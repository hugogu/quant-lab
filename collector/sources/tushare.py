"""Tushare fetcher — token-gated, higher rate limit, requires TUSHARE_TOKEN env.

Tushare Pro has a points-based system; daily() / pro_bar() needs enough points
for the user. Falls back from akshare when token is set and primary fails.

Reference: https://tushare.pro/document/2
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from typing import Any

from .base import Fetcher, SourceMisconfigured, SourceUnavailable

log = logging.getLogger(__name__)


class TushareAStockFetcher(Fetcher):
    source = "tushare_astock"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        token = os.getenv("TUSHARE_TOKEN")
        if not token:
            raise SourceMisconfigured(
                "TUSHARE_TOKEN env not set; tushare source will be skipped at runtime"
            )
        # Lazy import — only if token present
        import tushare as ts
        ts.set_token(token)
        self._pro = ts.pro_api()

    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        # Tushare expects YYYYMMDD strings; exchange suffix required for pro_bar
        ts_code = _to_ts_code(symbol)

        def _sync() -> list[dict]:
            df = self._pro.daily(
                ts_code=ts_code,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            if df is None or df.empty:
                return []
            return df.to_dict(orient="records")

        try:
            records = await asyncio.to_thread(_sync)
        except Exception as e:
            raise SourceUnavailable(f"tushare fetch failed for {symbol}: {e}") from e

        return {"symbol": symbol, "ts_code": ts_code, "start": str(start), "end": str(end), "records": records}

    def parse(self, raw: dict[str, Any], symbol: str) -> list[dict]:
        rows = []
        for r in raw.get("records", []):
            try:
                rows.append({
                    "symbol": symbol,
                    "trade_date": r["trade_date"],
                    "open": float(r["open"]) if r.get("open") is not None else None,
                    "high": float(r["high"]) if r.get("high") is not None else None,
                    "low": float(r["low"]) if r.get("low") is not None else None,
                    "close": float(r["close"]) if r.get("close") is not None else None,
                    "volume": int(float(r["vol"])) if r.get("vol") is not None else None,  # tushare: vol in 手 (lots)
                    "amount": float(r["amount"]) if r.get("amount") is not None else None,
                    "source": self.source,
                })
            except (KeyError, ValueError, TypeError) as e:
                log.warning("tushare parse: skipping malformed row for %s: %s", symbol, e)
        return rows


def _to_ts_code(symbol: str) -> str:
    """Convert local symbol (e.g. '000001') to tushare format ('000001.SZ')."""
    s = symbol.strip().lower()
    if "." in s:
        return s.upper()  # already in ts format
    if s.startswith(("60", "68", "9")):
        return f"{s}.SH"
    if s.startswith(("00", "30")):
        return f"{s}.SZ"
    if s.startswith(("8", "4")):
        return f"{s}.BJ"  # 北交所
    raise ValueError(f"cannot infer exchange for symbol {symbol!r}")