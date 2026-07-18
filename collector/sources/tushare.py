"""Tushare fetcher — token-gated, subprocess-isolated.

Token check still happens in-parent (raises ``SourceMisconfigured`` if
``TUSHARE_TOKEN`` is unset, so the registry skips this source cleanly
rather than spinning up a subprocess that will just fail). The token is
passed to the child via env (not argv) to avoid leaking it via
``/proc/<pid>/cmdline``.

Tushare Pro has a points-based system; daily() / pro_bar() needs enough
points for the user. Falls back from akshare when token is set and primary
fails.

Reference: https://tushare.pro/document/2
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from typing import Any

from .base import Fetcher, SourceMisconfigured, SourceUnavailable
from .subprocess_runner import run_sync_in_subprocess

log = logging.getLogger(__name__)


def _astock_sync(ts_code: str, start_date: str, end_date: str) -> list[dict]:
    """Sync tushare call. Reads TUSHARE_TOKEN from env (passed by parent).
    Returns list of dicts (tushare's native column names: vol/amount/etc.)."""
    import tushare as ts
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN not set in subprocess env")
    ts.set_token(token)
    pro = ts.pro_api()
    df = pro.daily(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


class TushareAStockFetcher(Fetcher):
    source = "tushare_astock"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        token = os.getenv("TUSHARE_TOKEN")
        if not token:
            raise SourceMisconfigured(
                "TUSHARE_TOKEN env not set; tushare source will be skipped at runtime"
            )
        # No pro_api() in parent anymore — happens in subprocess.

    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        ts_code = _to_ts_code(symbol)
        try:
            records = await run_sync_in_subprocess(
                "collector.sources.tushare", "_astock_sync",
                [ts_code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")],
                timeout=self.request_timeout,
                env_extra={"TUSHARE_TOKEN": os.environ["TUSHARE_TOKEN"]},
            )
        except SourceUnavailable:
            raise
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
