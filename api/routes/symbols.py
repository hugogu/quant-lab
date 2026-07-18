"""GET /symbols — list tracked symbols."""
from __future__ import annotations
from fastapi import APIRouter, Query
from ..db import fetch_symbols

router = APIRouter()


@router.get("/symbols")
async def list_symbols(market: str | None = Query(None), limit: int = Query(200, le=2000)):
    return await fetch_symbols(market=market, limit=limit)
