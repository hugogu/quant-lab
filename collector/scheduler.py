"""APScheduler-based watermark scheduler.

Reads `data_source` table → runs collector jobs with per-source watermark
(incremental fetch since last successful run). Logs to `job_run` table.

Phase 1: jobs for A股日线 + 基金净值. Phase 4: add 公告新闻.

Run: python -m collector.scheduler (in worker container)
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import acquire
from . import astock, fund

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


async def record_job_start(job_name: str) -> int:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO job_run(job_name, started_at, status) VALUES ($1, now(), 'running') RETURNING id",
            job_name)
    return row["id"]


async def record_job_end(job_id: int, status: str, rows_in: int, rows_upserted: int, error: str = None):
    async with acquire() as conn:
        await conn.execute(
            "UPDATE job_run SET finished_at=now(), status=$2, rows_in=$3, rows_upserted=$4, error_msg=$5 WHERE id=$1",
            job_id, status, rows_in, rows_upserted, error)


async def update_source_success(source: str, ok: bool):
    async with acquire() as conn:
        if ok:
            await conn.execute(
                "UPDATE data_source SET last_run_at=now(), last_success_at=now(), consecutive_errors=0 WHERE source=$1",
                source)
        else:
            await conn.execute(
                "UPDATE data_source SET last_run_at=now(), consecutive_errors=consecutive_errors+1 WHERE source=$1",
                source)


async def run_astock_job():
    job_id = await record_job_start("astock_daily")
    try:
        symbols = await astock.list_active_symbols()
        result = await astock.collect_symbols(symbols, lookback_days=7)
        total = sum(result.values())
        await record_job_end(job_id, "ok", len(symbols), total)
        await update_source_success("akshare_astock", True)
        log.info("astock_daily: %d symbols, %d rows upserted", len(symbols), total)
    except Exception as e:
        await record_job_end(job_id, "error", 0, 0, str(e))
        await update_source_success("akshare_astock", False)
        log.exception("astock_daily failed: %s", e)


async def run_fund_job():
    job_id = await record_job_start("fund_nav_daily")
    try:
        # Read all fund symbols from symbol table
        async with acquire() as conn:
            funds = await conn.fetch("SELECT symbol FROM symbol WHERE market='fund' AND status='active'")
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=3)
        rows_in, total = 0, 0
        for f in funds:
            rows = await fund.fetch_fund_nav(f["symbol"], start, end)
            if rows:
                await fund.upsert_fund_nav(rows)
            rows_in += 1
            total += len(rows)
        await record_job_end(job_id, "ok", rows_in, total)
        await update_source_success("akshare_fund", True)
        log.info("fund_nav_daily: %d funds, %d rows", rows_in, total)
    except Exception as e:
        await record_job_end(job_id, "error", 0, 0, str(e))
        await update_source_success("akshare_fund", False)
        log.exception("fund_nav_daily failed: %s", e)


async def main_async():
    sched = AsyncIOScheduler(timezone="Asia/Shanghai")

    # A股: 工作日 17:00 (after market close, before 22:00 fund run)
    sched.add_job(run_astock_job, CronTrigger.from_crontab(os.getenv("COLLECT_CRON_ASTOCK", "0 17 * * 1-5")))
    # 基金: 每天 22:00
    sched.add_job(run_fund_job, CronTrigger.from_crontab(os.getenv("COLLECT_CRON_FUND", "0 22 * * *")))

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
