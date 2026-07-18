"""Health check — also pings DB."""
from __future__ import annotations
from fastapi import APIRouter
from ..db import acquire

router = APIRouter()


@router.get("/healthz")
async def healthz():
    try:
        async with acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "up"}
    except Exception as e:
        return {"status": "degraded", "db": "down", "error": str(e)}
