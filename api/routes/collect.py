"""POST /collect — manually trigger a collector run (mostly for debugging).

Phase 2.0: uses the failover-aware registry (fetch_with_failover) instead of
the legacy collector.astock / collector.fund modules (deleted in commit 60ce2ba).
Each per-symbol call returns the source that served the data and how many
rows landed.
"""
from __future__ import annotations
from datetime import date, timedelta
from fastapi import APIRouter

from collector.sources import fetch_with_failover, SourceUnavailable
from collector.db import acquire, upsert_ohlcv

router = APIRouter()


async def _list_symbols(market: str) -> list[str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol FROM symbol WHERE market=$1 AND status='active' ORDER BY symbol",
            market,
        )
    return [r["symbol"] for r in rows]


@router.post("/collect/astock")
async def collect_astock(payload: dict | list | None = None, lookback_days: int = 7):
    """Trigger A-share collection.

    Accepts:
      - raw JSON list: ["000001", "600519"]
      - dict: {"symbols": ["000001", "600519"], "lookback_days": 7}
      - nothing: collect all active astock symbols
    """
    if isinstance(payload, list):
        symbols = payload
    elif isinstance(payload, dict):
        symbols = payload.get("symbols")
        if "lookback_days" in payload:
            lookback_days = int(payload["lookback_days"])
    else:
        symbols = None
    if not symbols:
        symbols = await _list_symbols("astock")

    end = date.today()
    start = end - timedelta(days=lookback_days)
    out: list[dict] = []
    for sym in symbols:
        try:
            r = await fetch_with_failover(
                domain="astock", symbol=sym, start=start, end=end, persist_raw=True,
            )
            if r.rows:
                await upsert_ohlcv(r.rows)
            out.append({
                "symbol": sym,
                "source": r.source,
                "rows": len(r.rows),
                "raw_id": r.raw_id,
            })
        except SourceUnavailable as e:
            out.append({"symbol": sym, "error": str(e)})
    return {"symbols_processed": len(out), "results": out}


@router.post("/collect/fund")
async def collect_fund(payload: dict | list | None = None, lookback_days: int = 3):
    """Trigger fund NAV collection. Accepts list, dict, or nothing."""
    if isinstance(payload, list):
        funds = payload
    elif isinstance(payload, dict):
        funds = payload.get("funds")
        if "lookback_days" in payload:
            lookback_days = int(payload["lookback_days"])
    else:
        funds = None
    if not funds:
        funds = await _list_symbols("fund")

    end = date.today()
    start = end - timedelta(days=lookback_days)
    out: list[dict] = []
    # Reuse the fund-specific upsert (it lives in scheduler to keep this module
    # independent of fund-specific row shape knowledge).
    from collector.scheduler import upsert_fund_nav

    for f in funds:
        try:
            r = await fetch_with_failover(
                domain="fund", symbol=f, start=start, end=end, persist_raw=True,
            )
            if r.rows:
                await upsert_fund_nav(r.rows)
            out.append({
                "symbol": f,
                "source": r.source,
                "rows": len(r.rows),
                "raw_id": r.raw_id,
            })
        except SourceUnavailable as e:
            out.append({"symbol": f, "error": str(e)})
    return {"funds_processed": len(out), "results": out}