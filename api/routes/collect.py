"""POST /collect — manually trigger a collector run (mostly for debugging)."""
from __future__ import annotations
import asyncio
from fastapi import APIRouter
from collector import astock, fund
from datetime import date, timedelta

router = APIRouter()


@router.post("/collect/astock")
async def collect_astock(symbols: list[str] | None = None, lookback_days: int = 7):
    if not symbols:
        symbols = await astock.list_active_symbols()
    result = await astock.collect_symbols(symbols, lookback_days=lookback_days)
    return {"symbols_processed": len(result), "rows_per_symbol": result}


@router.post("/collect/fund")
async def collect_fund(funds: list[str] | None = None, lookback_days: int = 3):
    if not funds:
        # Fallback to symbol table
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
