"""AKShare fetchers — refactored from legacy collector/astock.py + collector/fund.py.

Implements the Fetcher interface so it can plug into the registry's
failover chain. Logic is preserved (3-retry with exponential backoff,
RemoteDisconnected detection) — this is a structural refactor, not
a behavior change.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any

from .base import Fetcher, SourceUnavailable, with_retry

log = logging.getLogger(__name__)


# ============================================================
# A-share OHLCV
# ============================================================

class AKShareAStockFetcher(Fetcher):
    source = "akshare_astock"

    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        """Call akshare.stock_zh_a_hist and return the dataframe as a JSON-serializable
        dict. Wraps the sync call in asyncio.to_thread to avoid blocking the event loop."""
        def _sync() -> list[dict]:
            import akshare as ak
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",
            )
            if df is None or df.empty:
                return []
            return df.to_dict(orient="records")

        try:
            records = await asyncio.to_thread(_sync)
        except Exception as e:
            # Network errors / rate limits / remote disconnects → SourceUnavailable
            # triggers failover in registry.
            raise SourceUnavailable(f"akshare astock fetch failed for {symbol}: {e}") from e

        return {"symbol": symbol, "start": str(start), "end": str(end), "records": records}

    def parse(self, raw: dict[str, Any], symbol: str) -> list[dict]:
        rows = []
        for r in raw.get("records", []):
            try:
                rows.append({
                    "symbol": symbol,
                    "trade_date": r["日期"],
                    "open": float(r["开盘"]),
                    "high": float(r["最高"]),
                    "low": float(r["最低"]),
                    "close": float(r["收盘"]),
                    "volume": int(r["成交量"]),
                    "amount": float(r["成交额"]),
                    "source": self.source,
                })
            except (KeyError, ValueError, TypeError) as e:
                log.warning("akshare parse: skipping malformed row for %s: %s", symbol, e)
        return rows


# ============================================================
# Fund NAV
# ============================================================

class AKShareFundFetcher(Fetcher):
    source = "akshare_fund"

    async def fetch_raw(self, fund_code: str, start: date, end: date) -> dict[str, Any]:
        def _sync() -> list[dict]:
            import akshare as ak
            df = ak.fund_open_fund_info_em(fund=fund_code, indicator="单位净值走势")
            if df is None or df.empty:
                return []
            return df.to_dict(orient="records")

        try:
            records = await asyncio.to_thread(_sync)
        except Exception as e:
            raise SourceUnavailable(f"akshare fund fetch failed for {fund_code}: {e}") from e

        return {"fund_code": fund_code, "start": str(start), "end": str(end), "records": records}

    def parse(self, raw: dict[str, Any], fund_code: str) -> list[dict]:
        rows = []
        for r in raw.get("records", []):
            try:
                rows.append({
                    "fund_code": fund_code,
                    "nav_date": r["净值日期"],
                    "nav": float(r["单位净值"]) if r.get("单位净值") is not None else None,
                    "accum_nav": float(r["累计净值"]) if r.get("累计净值") is not None else None,
                    "daily_growth": float(r["日增长率"]) if r.get("日增长率") is not None else None,
                    "source": self.source,
                })
            except (KeyError, ValueError, TypeError) as e:
                log.warning("akshare fund parse: skipping malformed row for %s: %s", fund_code, e)
        return rows