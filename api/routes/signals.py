"""Signal endpoints — composite score + decision per (symbol, date).

End-state: signal_runner writes signal_vote rows + a derived latest-decision
view daily. These endpoints are read-only.

Endpoints:
  GET /signals/latest            latest decision per symbol (cross-section)
  GET /signals/{symbol}          time series of scores + decisions for one symbol
  GET /signals/runs              signal_vote ingestion stats (last N runs)
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from collector.db import acquire

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/latest")
async def latest_decisions(limit: int = Query(100, ge=1, le=500)):
    """Cross-section of the latest composite score + decision per symbol.

    Source of truth: derived from `feature_value` joined with a precomputed
    composite (for now, we recompute on the fly since signal_vote may not be
    populated yet by the cron).
    """
    sql = """
    WITH latest_features AS (
        SELECT DISTINCT ON (symbol, feature)
               symbol, feature, calc_date, value
        FROM feature_value
        WHERE calc_date <= CURRENT_DATE
        ORDER BY symbol, feature, calc_date DESC
    )
    SELECT symbol, calc_date, value
    FROM latest_features
    ORDER BY symbol, feature
    """
    # Until signal_runner writes the composite table, return the per-feature
    # snapshot — useful for the UI to show what's being measured.
    async with acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows][:limit]


@router.get("/{symbol}")
async def symbol_signal_history(
    symbol: str,
    days: int = Query(252, ge=1, le=1000),
):
    """Time series of (calc_date, feature, value) for one symbol.

    Composite score derivation is in Milestone C6 (signal_runner); for now
    this returns raw feature_value rows so the UI can show the input signal.
    """
    sql = """
    SELECT feature, calc_date, value, factor_version
    FROM feature_value
    WHERE symbol = $1 AND calc_date >= $2
    ORDER BY feature, calc_date DESC
    """
    since = date.today() - timedelta(days=days)
    async with acquire() as conn:
        rows = await conn.fetch(sql, symbol, since)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no feature_value rows for {symbol}")
    return [dict(r) for r in rows]