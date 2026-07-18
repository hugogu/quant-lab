"""Signal computation runner — daily, after factor_runner.

Pipeline:
  feature_value → voter (±1/0) → IC weights → composite score
                → upsert signal_vote (long form)
                → latest decisions cached in memory for /signals/latest

Wiring: scheduler at 17:45 weekdays (after factor_runner at 17:30).

For Phase 2.2 baseline, IC weights default to equal-weighting if forward
returns aren't available (only ~5 days of OHLCV currently). When tushare is
wired in or history accumulates, the IC path activates automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

import polars as pl

from .db import acquire
from factors import all_factors, materialize_for_storage
from signals import (
    vote_dataframe_simple,
    compute_factor_ics,
    ics_to_weights,
    equal_weights,
    composite_score_df,
    latest_decisions,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# ============================================================
# Reads
# ============================================================

async def fetch_features(conn, since: date) -> pl.DataFrame:
    """Read feature_value rows into a polars DataFrame.
    Returns [symbol, feature, calc_date, value, factor_version] sorted by calc_date DESC."""
    rows = await conn.fetch(
        """
        SELECT symbol, feature, calc_date, value, factor_version
        FROM feature_value
        WHERE calc_date >= $1
        ORDER BY calc_date DESC
        """,
        since,
    )
    if not rows:
        return pl.DataFrame(schema={
            "symbol": pl.Utf8, "feature": pl.Utf8,
            "calc_date": pl.Date, "value": pl.Float64,
            "factor_version": pl.Utf8,
        })
    return pl.DataFrame([dict(r) for r in rows])


async def fetch_forward_returns(conn, lookback_days: int, n_days: int = 20) -> pl.DataFrame:
    """Compute N-day forward returns from ohlcv_daily.
    For each (symbol, calc_date), forward_return_Nd = close[calc_date + N] / close[calc_date] - 1.
    Returns [symbol, calc_date, forward_return_Nd].
    """
    sql = """
    WITH windowed AS (
        SELECT symbol, trade_date, close,
               LEAD(close, $2) OVER (PARTITION BY symbol ORDER BY trade_date) AS future_close
        FROM ohlcv_daily
        WHERE trade_date >= CURRENT_DATE - ($1::int || ' days')::interval
    )
    SELECT symbol, trade_date AS calc_date,
           (future_close / NULLIF(close, 0) - 1) AS forward_return_20d
    FROM windowed
    WHERE future_close IS NOT NULL
    """
    rows = await conn.fetch(sql, lookback_days, n_days)
    if not rows:
        return pl.DataFrame(schema={
            "symbol": pl.Utf8, "calc_date": pl.Date,
            "forward_return_20d": pl.Float64,
        })
    return pl.DataFrame([dict(r) for r in rows])


# ============================================================
# Writes
# ============================================================

async def upsert_signal_vote(conn, votes_df: pl.DataFrame) -> int:
    """rows: [symbol, feature, calc_date, vote, weight, factor_version-as-strategy]
    ON CONFLICT (symbol, strategy, feature, vote_date) DO UPDATE."""
    if votes_df.is_empty():
        return 0
    rows = votes_df.with_columns(
        pl.lit("v1.0").alias("strategy")  # placeholder; Phase 2.3 will allow multi-strategy
    ).to_dicts()
    coerced = []
    for r in rows:
        cd = r["calc_date"]
        if hasattr(cd, "isoformat"):
            cd = cd
        elif isinstance(cd, str):
            from datetime import datetime
            cd = datetime.strptime(cd, "%Y-%m-%d").date()
        coerced.append((
            r["symbol"], r["strategy"], r["feature"], cd,
            int(r["vote"]), float(r["weight"]),
        ))
    sql = """
    INSERT INTO signal_vote(symbol, strategy, feature, vote_date, vote, weight)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (symbol, strategy, feature, vote_date) DO UPDATE SET
        vote = EXCLUDED.vote, weight = EXCLUDED.weight
    """
    async with conn.transaction():
        await conn.executemany(sql, coerced)
    return len(coerced)


# ============================================================
# Main runner
# ============================================================

async def run(lookback_days: int = 252) -> dict:
    """Compute signals for the most recent feature_value rows."""
    since = date.today() - timedelta(days=lookback_days)
    async with acquire() as conn:
        factor_df = await fetch_features(conn, since)
        if factor_df.is_empty():
            log.warning("signal_runner: no feature_value rows in last %d days", lookback_days)
            return {"votes_written": 0, "scores_computed": 0}

        # IC weights — fall back to equal if forward returns unavailable
        try:
            fwd_df = await fetch_forward_returns(conn, lookback_days, n_days=20)
            if fwd_df.is_empty():
                raise ValueError("no forward returns")
            ics = compute_factor_ics(factor_df, fwd_df)
            weights = ics_to_weights(ics)
            log.info("IC weights (Spearman on last %dd): %s", lookback_days,
                     {k: round(v, 4) for k, v in weights.items()})
        except Exception as e:
            log.warning("IC computation failed (%s); using equal weights", e)
            weights = equal_weights(list(factor_df["feature"].unique()))

        # Vote
        votes_df = vote_dataframe_simple(factor_df)
        votes_written = await upsert_signal_vote(conn, votes_df)

        # Composite (in memory; not persisted as a table yet — Phase 2.3 will
        # add a `composite_signal` table to avoid recomputing per request)
        scores = composite_score_df(votes_df, weights)

    return {
        "votes_written": votes_written,
        "scores_computed": len(scores),
        "weights": weights,
        "latest_top": _format_latest(scores),
    }


def _format_latest(scores: pl.DataFrame) -> list[dict]:
    """Format the top-N latest decisions for a quick log line."""
    latest = latest_decisions(scores)
    return [
        {"symbol": r["symbol"], "score": round(r["score"], 4), "decision": r["decision"]}
        for r in latest.head(10).to_dicts()
    ]


# ============================================================
# CLI
# ============================================================

async def cmd_run(args):
    result = await run(args.days)
    print(f"votes_written: {result['votes_written']}")
    print(f"scores_computed: {result['scores_computed']}")
    if result.get("weights"):
        print(f"weights: {result['weights']}")
    if result.get("latest_top"):
        print("\nLatest decisions (top 10):")
        for d in result["latest_top"]:
            print(f"  {d['symbol']}: score={d['score']:+.4f} → {d['decision']}")


def main():
    p = argparse.ArgumentParser(description="quant-lab signal runner")
    p.add_argument("--days", type=int, default=252, help="feature_value lookback")
    args = p.parse_args()
    asyncio.run(cmd_run(args))


if __name__ == "__main__":
    main()