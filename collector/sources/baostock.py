"""BaoStock fetcher — stable fallback for A-share OHLCV.

BaoStock uses its own protocol (not HTTP REST) so it works through
some GFW paths that block eastmoney. Slower than akshare (1 req/sec
recommended) but more reliable when akshare's upstream is throttled.

Reference: http://baostock.com/baostock/index.php/A%E8%82%A1K%E7%BA%BF%E6%95%B0%E6%8D%AE
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from .base import Fetcher, SourceUnavailable

log = logging.getLogger(__name__)


# BaoStock uses 9-char codes with dot separator: "sh.600000" / "sz.000001"
def _to_bs_code(symbol: str) -> str:
    s = symbol.strip()
    s_lower = s.lower()
    # already in bs format (has dot after sh/sz)
    if len(s_lower) >= 4 and s_lower[2:3] == "." and s_lower[:2] in ("sh", "sz"):
        return s_lower
    # already sh/sz prefixed without dot — insert dot
    if s_lower.startswith(("sh", "sz")):
        return s_lower[:2] + "." + s_lower[2:]
    # Infer exchange from numeric prefix
    if s.startswith(("60", "68", "9")):
        return f"sh.{s}"
    if s.startswith(("00", "30")):
        return f"sz.{s}"
    raise ValueError(f"cannot infer exchange for symbol {symbol!r}")


class BaoStockAStockFetcher(Fetcher):
    source = "baostock_astock"

    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        bs_code = _to_bs_code(symbol)

        def _sync() -> list[dict]:
            import baostock as bs
            lg = bs.login()
            if lg.error_code != "0":
                raise SourceUnavailable(f"baostock login failed: {lg.error_msg}")
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency="d",
                    adjustflag="2",  # 1=不复权, 2=前复权, 3=后复权
                )
                rows = []
                while (rs.error_code == "0") and rs.next():
                    rows.append(rs.get_row_data())
                return rows
            finally:
                bs.logout()

        try:
            records = await asyncio.to_thread(_sync)
        except Exception as e:
            raise SourceUnavailable(f"baostock fetch failed for {symbol}: {e}") from e

        return {"symbol": symbol, "bs_code": bs_code, "start": str(start), "end": str(end), "records": records}

    def parse(self, raw: dict[str, Any], symbol: str) -> list[dict]:
        rows = []
        # baostock's get_row_data() returns a tuple (when fields are specified),
        # NOT a dict. Map by position to field names.
        fields = ["date", "open", "high", "low", "close", "volume", "amount"]
        for r in raw.get("records", []):
            try:
                if isinstance(r, (list, tuple)):
                    if len(r) < len(fields):
                        log.warning("baostock parse: short row for %s: %s", symbol, r)
                        continue
                    d = dict(zip(fields, r))
                elif isinstance(r, dict):
                    d = r
                else:
                    continue
                # BaoStock returns empty strings for missing fields
                rows.append({
                    "symbol": symbol,
                    "trade_date": d["date"].replace("-", ""),  # normalize to YYYYMMDD like akshare
                    "open": float(d["open"]) if d.get("open") else None,
                    "high": float(d["high"]) if d.get("high") else None,
                    "low": float(d["low"]) if d.get("low") else None,
                    "close": float(d["close"]) if d.get("close") else None,
                    "volume": int(float(d["volume"])) if d.get("volume") else None,
                    "amount": float(d["amount"]) if d.get("amount") else None,
                    "source": self.source,
                })
            except (KeyError, ValueError, TypeError) as e:
                log.warning("baostock parse: skipping malformed row for %s: %s", symbol, e)
        return rows