"""GET /ohlcv/{symbol} — historical OHLCV. GET /fund/{code} — fund NAV."""
from __future__ import annotations
from fastapi import APIRouter, Query
from datetime import date
from ..db import fetch_ohlcv, fetch_fund_nav

router = APIRouter()


@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol: str,
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(500, le=5000),
):
    return await fetch_ohlcv(symbol, start=str(start) if start else None,
                             end=str(end) if end else None, limit=limit)


@router.get("/fund/{code}")
async def get_fund(code: str, limit: int = Query(500, le=5000)):
    return await fetch_fund_nav(code, limit=limit)
