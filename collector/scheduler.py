"""APScheduler-based watermark scheduler (Phase 2.0: source failover aware).

Reads `data_source` table → runs collector jobs that use SourceRegistry
to fetch with failover. Each successful fetch:
  1. Persists raw_payload (Kimi "raw 先行")
  2. Parses rows and upserts to ohlcv_daily / fund_nav
  3. Updates data_source.last_success_at + consecutive_errors

Run: python -m collector.scheduler (in worker container)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import acquire, upsert_ohlcv
from .sources import fetch_with_failover, SourceUnavailable
from . import factor_runner

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# ============================================================
# job_run helpers
# ============================================================

async def record_job_start(job_name: str) -> int:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO job_run(job_name, started_at, status) VALUES ($1, now(), 'running') RETURNING id",
            job_name)
    return row["id"]


async def record_job_end(job_id: int, status: str, rows_in: int, rows_upserted: int,
                          error: str = None, source_used: str = None, fail_reason: str = None):
    async with acquire() as conn:
        await conn.execute(
            """UPDATE job_run
               SET finished_at=now(), status=$2, rows_in=$3, rows_upserted=$4,
                   error_msg=$5, source_used=$6, fail_reason=$7
               WHERE id=$1""",
            job_id, status, rows_in, rows_upserted, error, source_used, fail_reason)


async def list_active_symbols(market: str = "astock") -> list[str]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol FROM symbol WHERE market=$1 AND status='active' ORDER BY symbol",
            market)
    return [r["symbol"] for r in rows]


# ============================================================
# A股 daily collection (Phase 2.0: failover-aware)
# ============================================================

async def collect_symbol_one(symbol: str, lookback_days: int = 7):
    """Fetch one symbol via failover chain. Returns (source_used, rows)."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)
    result = await fetch_with_failover(domain="astock", symbol=symbol, start=start, end=end)
    return result.source, result.rows


async def run_astock_job():
    job_id = await record_job_start("astock_daily")
    symbols = await list_active_symbols("astock")
    rows_upserted = 0
    last_source: str | None = None
    fail_reason: str | None = None
    sources_used: set[str] = set()
    try:
        for sym in symbols:
            try:
                source, rows = await collect_symbol_one(sym, lookback_days=7)
                last_source = source
                sources_used.add(source)
                if rows:
                    await upsert_ohlcv(rows)
                    rows_upserted += len(rows)
            except SourceUnavailable as e:
                fail_reason = f"{sym}: {e}"
                log.error("astock_daily: %s — skipping", fail_reason)
                continue

        status = "ok"
        log.info(
            "astock_daily: %d symbols, %d rows upserted, sources=%s",
            len(symbols), rows_upserted, sorted(sources_used) or "none",
        )
    except Exception as e:
        status = "error"
        fail_reason = fail_reason or str(e)
        log.exception("astock_daily failed: %s", e)

    await record_job_end(
        job_id, status, len(symbols), rows_upserted,
        error=fail_reason if status == "error" else None,
        source_used=last_source,
        fail_reason=fail_reason,
    )


# ============================================================
# Fund NAV daily (Phase 2.0: failover-aware for fund too)
# ============================================================

async def collect_fund_one(fund_code: str, lookback_days: int = 3):
    """Fetch one fund via failover chain. Returns (source_used, rows)."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)
    result = await fetch_with_failover(domain="fund", symbol=fund_code, start=start, end=end)
    return result.source, result.rows


async def upsert_fund_nav(rows: list[dict]) -> int:
    """Fund-specific upsert (kept here so this module is self-contained).
    nav_date may be str or python date; coerced for asyncpg."""
    from .db import _coerce_date
    if not rows:
        return 0
    sql = """
    INSERT INTO fund_nav(fund_code, nav_date, nav, accum_nav, daily_growth, source)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (fund_code, nav_date) DO UPDATE SET
        nav=EXCLUDED.nav, accum_nav=EXCLUDED.accum_nav,
        daily_growth=EXCLUDED.daily_growth, source=EXCLUDED.source
    """
    coerced = [
        (r["fund_code"], _coerce_date(r["nav_date"]),
         r.get("nav"), r.get("accum_nav"), r.get("daily_growth"), r["source"])
        for r in rows
    ]
    async with acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, coerced)
    return len(rows)


async def run_fund_job():
    job_id = await record_job_start("fund_nav_daily")
    funds = await list_active_symbols("fund")
    rows_upserted = 0
    last_source: str | None = None
    fail_reason: str | None = None
    sources_used: set[str] = set()
    try:
        for f in funds:
            try:
                source, rows = await collect_fund_one(f, lookback_days=3)
                last_source = source
                sources_used.add(source)
                if rows:
                    await upsert_fund_nav(rows)
                    rows_upserted += len(rows)
            except SourceUnavailable as e:
                fail_reason = f"{f}: {e}"
                log.error("fund_nav_daily: %s — skipping", fail_reason)
                continue

        status = "ok"
        log.info(
            "fund_nav_daily: %d funds, %d rows upserted, sources=%s",
            len(funds), rows_upserted, sorted(sources_used) or "none",
        )
    except Exception as e:
        status = "error"
        fail_reason = fail_reason or str(e)
        log.exception("fund_nav_daily failed: %s", e)

    await record_job_end(
        job_id, status, len(funds), rows_upserted,
        error=fail_reason if status == "error" else None,
        source_used=last_source,
        fail_reason=fail_reason,
    )


# ============================================================
# Scheduler entrypoint
# ============================================================

async def main_async():
    sched = AsyncIOScheduler(timezone="Asia/Shanghai")

    # A股: 工作日 17:00 (after market close)
    sched.add_job(run_astock_job, CronTrigger.from_crontab(os.getenv("COLLECT_CRON_ASTOCK", "0 17 * * 1-5")))
    # 基金: 每天 22:00
    sched.add_job(run_fund_job, CronTrigger.from_crontab(os.getenv("COLLECT_CRON_FUND", "0 22 * * *")))
    # 因子: 工作日 17:30 (after OHLCV is in)
    sched.add_job(
        lambda: factor_runner.run_all_symbols(lookback_days=int(os.getenv("FACTOR_LOOKBACK_DAYS", "252"))),
        CronTrigger.from_crontab(os.getenv("FACTOR_CRON", "30 17 * * 1-5")),
        id="factor_runner",
    )

    # Run once on startup to populate initial data
    sched.add_job(run_astock_job, "date", id="astock_startup")
    sched.add_job(run_fund_job, "date", id="fund_startup")

    log.info("scheduler starting")
    sched.start()
    # Keep event loop alive — APScheduler 3.x needs a running loop
    while True:
        await asyncio.sleep(3600)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()