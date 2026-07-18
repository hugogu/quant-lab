"""A-share daily OHLCV collector via akshare (free, no key)."""
from __future__ import annotations
import asyncio
import logging
from datetime import date, timedelta
from typing import Iterable
from .db import upsert_ohlcv, acquire

log = logging.getLogger(__name__)


async def fetch_one(symbol: str, start: date, end: date, source: str = "akshare_astock") -> list[dict]:
    """Fetch OHLCV rows for one symbol. Returns list of dicts ready for upsert_ohlcv.

    Akshare is sync + blocking — run in a thread pool to not stall the event loop.
    """
    def _sync_fetch():
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",  # forward-adjusted
            )
        except Exception as e:
            log.warning("akshare fetch failed for %s: %s", symbol, e)
            return []
        if df is None or df.empty:
            return []
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "symbol": symbol,
                "trade_date": r["日期"],
                "open": float(r["开盘"]),
                "high": float(r["最高"]),
                "low": float(r["最低"]),
                "close": float(r["收盘"]),
                "volume": int(r["成交量"]),
                "amount": float(r["成交额"]),
                "source": source,
            })
        return rows

    return await asyncio.to_thread(_sync_fetch)


async def collect_symbols(symbols: Iterable[str], lookback_days: int = 7) -> dict[str, int]:
    """Collect last `lookback_days` for each symbol. Returns {symbol: rows_upserted}."""
    end = date.today()
    start = end - timedelta(days=lookback_days)
    result: dict[str, int] = {}
    for sym in symbols:
        rows = await fetch_one(sym, start, end)
        if rows:
            await upsert_ohlcv(rows)
        result[sym] = len(rows)
    return result


async def list_active_symbols() -> list[str]:
    async with acquire() as conn:
        rows = await conn.fetch("SELECT symbol FROM symbol WHERE market='astock' AND status='active' ORDER BY symbol")
    return [r["symbol"] for r in rows]
