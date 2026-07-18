"""BaoStock fetcher — subprocess-isolated; demoted to last-resort.

BaoStock uses its own TCP protocol (port 10030) instead of HTTP REST. Under
GFW, the long-lived TCP session gets silently dropped and the remote ends
up in CLOSE_WAIT. The baostock library's ``send_msg`` then busy-loops
writing to the half-closed socket without backoff — pegging one core
indefinitely. This was the root cause of the worker CPU spin bug.

Running the sync call in a subprocess lets the parent SIGKILL the spinning
child on timeout, ending the spin reliably (threads can't be killed;
subprocesses can).

BaoStock is also demoted to priority 99 in ``sql/006`` so it's only tried
after akshare + tushare both fail. Under GFW it's unreliable as a primary.

Reference: http://baostock.com/baostock/index.php/A%E8%82%A1K%E7%BA%BF%E6%95%B0%E6%8D%AE
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .base import Fetcher, SourceUnavailable
from .subprocess_runner import run_sync_in_subprocess

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


def _astock_sync(bs_code: str, start_date: str, end_date: str) -> list[list[str]]:
    """Sync baostock call. Runs in a child process so a stuck TCP session
    can be SIGKILLed by the parent. Returns list of row tuples
    (date, open, high, low, close, volume, amount) — same shape baostock's
    ``get_row_data()`` returns. Parse happens in-parent."""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",  # 1=不复权, 2=前复权, 3=后复权
        )
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"baostock query error: {rs.error_msg}")
        return rows
    finally:
        bs.logout()


class BaoStockAStockFetcher(Fetcher):
    source = "baostock_astock"

    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        bs_code = _to_bs_code(symbol)
        try:
            records = await run_sync_in_subprocess(
                "collector.sources.baostock", "_astock_sync",
                [bs_code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
                timeout=self.request_timeout,
            )
        except SourceUnavailable:
            raise
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
