"""POST /collect — manually trigger a collector run (mostly for debugging)."""
from __future__ import annotations
import asyncio
from fastapi import APIRouter
from collector import astock, fund
from datetime import date, timedelta

router = APIRouter()


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
        symbols = await astock.list_active_symbols()
    result = await astock.collect_symbols(symbols, lookback_days=lookback_days)
    return {"symbols_processed": len(result), "rows_per_symbol": result}


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
        from ..db import acquire
        async with acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM symbol WHERE market='fund' AND status='active'")
        funds = [r["symbol"] for r in rows]
    end = date.today()
    start = end - timedelta(days=lookback_days)
    rows_in, total = 0, 0
    for f in funds:
        rows = await fund.fetch_fund_nav(f, start, end)
        if rows:
            await fund.upsert_fund_nav(rows)
        rows_in += 1
        total += len(rows)
    return {"funds_processed": rows_in, "rows_total": total}
