"""Factor computation runner.

Reads OHLCV from `ohlcv_daily`, computes all registered factors via the
`factors` registry, and upserts results to `feature_value`.

Designed to run after `run_astock_job` completes (so today's OHLCV is in).
The scheduler wires it in at 17:30 weekdays.

Usage:
    # from scheduler (cron):
    await factor_runner.run_all_symbols()

    # from CLI / manual backfill:
    python -m collector.factor_runner --symbols 000001,600519 --days 252
    python -m collector.factor_runner --all --days 60   # quick smoke
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

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# ============================================================
# OHLCV read
# ============================================================

async def fetch_ohlcv_df(conn, symbol: str, lookback_days: int) -> pl.DataFrame:
    """Read last `lookback_days` of OHLCV for `symbol` into a polars DataFrame.

    Returns columns: trade_date, open, high, low, close, volume.
    Sorted by trade_date ASC (oldest first) so rolling/lookback works correctly.
    Numeric columns are coerced to float (asyncpg returns Decimal for NUMERIC,
    which polars can't always infer into Decimal128 — Float64 is fine for prices).
    """
    rows = await conn.fetch(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM ohlcv_daily
        WHERE symbol = $1
          AND trade_date >= $2
        ORDER BY trade_date ASC
        """,
        symbol,
        date.today() - timedelta(days=lookback_days),
    )
    schema = {
        "trade_date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
    }
    if not rows:
        return pl.DataFrame(schema=schema)

    coerced = [
        {
            "trade_date": r["trade_date"],
            "open":   float(r["open"])   if r["open"]   is not None else None,
            "high":   float(r["high"])   if r["high"]   is not None else None,
            "low":    float(r["low"])    if r["low"]    is not None else None,
            "close":  float(r["close"])  if r["close"]  is not None else None,
            "volume": int(r["volume"])   if r["volume"] is not None else None,
        }
        for r in rows
    ]
    return pl.DataFrame(coerced, schema=schema)


async def list_active_symbols(market: str = "astock") -> list[str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol FROM symbol WHERE market=$1 AND status='active' ORDER BY symbol",
            market,
        )
    return [r["symbol"] for r in rows]


# ============================================================
# Upsert
# ============================================================

async def upsert_feature_value(conn, rows: list[dict]) -> int:
    """rows: [{symbol, feature, calc_date, value, factor_version}, ...]
    ON CONFLICT (symbol, feature, calc_date, factor_version) DO UPDATE.
    Drops NULLs (don't overwrite a real value with NaN)."""
    if not rows:
        return 0
    # Filter out rows where value is None (NaN from insufficient lookback)
    rows = [r for r in rows if r.get("value") is not None]
    if not rows:
        return 0
    sql = """
    INSERT INTO feature_value(symbol, feature, calc_date, value, factor_version)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (symbol, feature, calc_date, factor_version) DO UPDATE SET
        value = EXCLUDED.value
    """
    async with conn.transaction():
        await conn.executemany(sql, [
            (r["symbol"], r["feature"], r["calc_date"], r["value"], r["factor_version"])
            for r in rows
        ])
    return len(rows)


# ============================================================
# Run for one symbol
# ============================================================

async def compute_for_symbol(conn, symbol: str, lookback_days: int = 252) -> dict[str, int]:
    """Compute all registered factors for one symbol. Returns per-factor row counts."""
    df = await fetch_ohlcv_df(conn, symbol, lookback_days)
    if df.is_empty():
        log.warning("%s: no OHLCV rows in last %d days, skipping", symbol, lookback_days)
        return {}

    counts: dict[str, int] = {}
    factors = all_factors()
    if not factors:
        log.warning("no factors registered — import factors.builtin")
        return {}

    for f_name, factor in factors.items():
        try:
            out_df = factor.fn(df)
        except Exception as e:
            log.exception("factor %s failed for %s: %s", f_name, symbol, e)
            counts[f_name] = 0
            continue
        materialized = materialize_for_storage(out_df, factor, symbol)
        rows = materialized.to_dicts()
        # polars Date → python date for asyncpg
        for r in rows:
            cd = r["calc_date"]
            if hasattr(cd, "isoformat"):
                r["calc_date"] = cd  # already a date
            elif isinstance(cd, str):
                from datetime import datetime
                r["calc_date"] = datetime.strptime(cd, "%Y-%m-%d").date()
        written = await upsert_feature_value(conn, rows)
        counts[f_name] = written

    return counts


# ============================================================
# Run for all symbols (cron entrypoint)
# ============================================================

async def run_all_symbols(lookback_days: int = 252) -> dict:
    """Compute factors for every active astock symbol. Returns summary stats."""
    async with acquire() as conn:
        symbols = await list_active_symbols("astock")
        log.info("factor_runner: %d symbols × %d factors, lookback=%dd",
                 len(symbols), len(all_factors()), lookback_days)
        total = 0
        per_symbol: dict[str, dict[str, int]] = {}
        for sym in symbols:
            counts = await compute_for_symbol(conn, sym, lookback_days)
            per_symbol[sym] = counts
            total += sum(counts.values())
        log.info("factor_runner done: %d total rows upserted", total)
        return {"symbols": len(symbols), "total_rows": total, "per_symbol": per_symbol}


# ============================================================
# CLI
# ============================================================

async def cmd_run(args):
    async with acquire() as conn:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        elif args.all:
            symbols = await list_active_symbols("astock")
        else:
            print("specify --symbols or --all", file=sys.stderr)
            sys.exit(1)

        total = 0
        for sym in symbols:
            counts = await compute_for_symbol(conn, sym, args.days)
            sub = sum(counts.values())
            total += sub
            print(f"{sym}: {sub} rows ({counts})")
        print(f"\nTotal: {total} rows upserted across {len(symbols)} symbols")


def main():
    p = argparse.ArgumentParser(description="quant-lab factor runner")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbols", help="comma-separated list of symbols")
    g.add_argument("--all", action="store_true", help="all active astock symbols")
    p.add_argument("--days", type=int, default=252, help="OHLCV lookback window")
    args = p.parse_args()
    asyncio.run(cmd_run(args))


if __name__ == "__main__":
    main()