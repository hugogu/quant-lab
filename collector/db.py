"""PostgreSQL/asyncpg helpers shared by all collectors and API."""
from __future__ import annotations
import os
import asyncpg
from contextlib import asynccontextmanager


def db_dsn() -> str:
    """Build DSN from env. Defaults match docker-compose service names."""
    host = os.getenv("POSTGRES_HOST", "timescaledb")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "quantlab")
    user = os.getenv("POSTGRES_USER", "quant")
    pw = os.getenv("POSTGRES_PASSWORD", "change_me_in_prod")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


_POOL: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(dsn=db_dsn(), min_size=1, max_size=4)
    return _POOL


@asynccontextmanager
async def acquire():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def upsert_ohlcv(rows: list[dict]) -> int:
    """rows: [{'symbol','trade_date','open','high','low','close','volume','amount','source'}, ...]"""
    if not rows:
        return 0
    sql = """
    INSERT INTO ohlcv_daily(symbol, trade_date, open, high, low, close, volume, amount, source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (symbol, trade_date) DO UPDATE SET
        open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
        close=EXCLUDED.close, volume=EXCLUDED.volume, amount=EXCLUDED.amount,
        source=EXCLUDED.source
    """
    async with acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, [
                (r["symbol"], r["trade_date"], r.get("open"), r.get("high"), r.get("low"),
                 r.get("close"), r.get("volume"), r.get("amount"), r["source"])
                for r in rows
            ])
    return len(rows)
