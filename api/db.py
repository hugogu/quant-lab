"""DB helpers for API (uses collector.db for connection pool)."""
from __future__ import annotations
from collector.db import acquire, get_pool  # noqa: re-export


async def fetch_symbols(market: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT symbol, name, market, exchange, status FROM symbol"
    args = []
    if market:
        sql += " WHERE market = $1"
        args.append(market)
    sql += " ORDER BY symbol LIMIT $" + str(len(args) + 1)
    args.append(limit)
    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def fetch_ohlcv(symbol: str, start: str | None = None, end: str | None = None, limit: int = 500) -> list[dict]:
    sql = "SELECT symbol, trade_date, open, high, low, close, volume, amount, source FROM ohlcv_daily WHERE symbol = $1"
    args = [symbol]
    if start:
        sql += f" AND trade_date >= ${len(args)+1}"
        args.append(start)
    if end:
        sql += f" AND trade_date <= ${len(args)+1}"
        args.append(end)
    sql += f" ORDER BY trade_date DESC LIMIT ${len(args)+1}"
    args.append(limit)
    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def fetch_fund_nav(fund_code: str, limit: int = 500) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT fund_code, nav_date, nav, accum_nav, daily_growth, source FROM fund_nav WHERE fund_code = $1 ORDER BY nav_date DESC LIMIT $2",
            fund_code, limit)
    return [dict(r) for r in rows]
