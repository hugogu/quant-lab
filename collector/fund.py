"""Fund NAV collector via akshare (free, daily NAV)."""
from __future__ import annotations
import asyncio
import logging
from datetime import date, timedelta
from .db import acquire

log = logging.getLogger(__name__)


async def fetch_fund_nav(fund_code: str, start: date, end: date) -> list[dict]:
    def _sync():
        try:
            import akshare as ak
            df = ak.fund_open_fund_info_em(fund=fund_code, indicator="单位净值走势")
        except Exception as e:
            log.warning("akshare fund fetch failed for %s: %s", fund_code, e)
            return []
        if df is None or df.empty:
            return []
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "fund_code": fund_code,
                "nav_date": r["净值日期"],
                "nav": float(r["单位净值"]) if r.get("单位净值") is not None else None,
                "accum_nav": float(r["累计净值"]) if r.get("累计净值") is not None else None,
                "daily_growth": float(r["日增长率"]) if r.get("日增长率") is not None else None,
                "source": "akshare_fund",
            })
        return rows

    return await asyncio.to_thread(_sync)


async def upsert_fund_nav(rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
    INSERT INTO fund_nav(fund_code, nav_date, nav, accum_nav, daily_growth, source)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (fund_code, nav_date) DO UPDATE SET
        nav=EXCLUDED.nav, accum_nav=EXCLUDED.accum_nav,
        daily_growth=EXCLUDED.daily_growth, source=EXCLUDED.source
    """
    async with acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, [
                (r["fund_code"], r["nav_date"], r.get("nav"), r.get("accum_nav"),
                 r.get("daily_growth"), r["source"]) for r in rows
            ])
    return len(rows)
