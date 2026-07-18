"""AKShare fetchers — subprocess-isolated for CPU-spin containment.

Each sync call runs in a fresh subprocess (see ``subprocess_runner``) so
that if akshare's underlying HTTP/SSL layer hangs or busy-loops, the
parent can SIGKILL it after ``request_timeout`` seconds. This replaces
the previous ``asyncio.to_thread`` dispatch which could not cancel a
stuck thread.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .base import Fetcher, SourceUnavailable
from .subprocess_runner import run_sync_in_subprocess

log = logging.getLogger(__name__)


# ============================================================
# Module-level sync functions — callable from a subprocess.
# Must NOT close over instance state (subprocess can't pickle closures).
# ============================================================

def _astock_sync(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Sync akshare call. Runs in a child process; returns list of raw dicts
    (Chinese-keyed, exactly what akshare returns). Parse is done in-parent."""
    import akshare as ak
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def _fund_sync(fund_code: str) -> list[dict]:
    import akshare as ak
    df = ak.fund_open_fund_info_em(fund=fund_code, indicator="单位净值走势")
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


# ============================================================
# Fetchers — fetch_raw spawns a subprocess; parse() unchanged.
# ============================================================

class AKShareAStockFetcher(Fetcher):
    source = "akshare_astock"

    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        try:
            records = await run_sync_in_subprocess(
                "collector.sources.akshare", "_astock_sync",
                [symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")],
                timeout=self.request_timeout,
            )
        except SourceUnavailable:
            raise
        except Exception as e:
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


class AKShareFundFetcher(Fetcher):
    source = "akshare_fund"

    async def fetch_raw(self, fund_code: str, start: date, end: date) -> dict[str, Any]:
        try:
            records = await run_sync_in_subprocess(
                "collector.sources.akshare", "_fund_sync",
                [fund_code],
                timeout=self.request_timeout,
            )
        except SourceUnavailable:
            raise
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
