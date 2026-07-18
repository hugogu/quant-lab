"""SourceRegistry — failover chain, watermark-based incremental fetch, raw_payload persistence.

This is the orchestration layer. Callers (scheduler, API, CLI) don't
instantiate Fetchers directly; they go through `fetch_with_failover(symbol, start, end)`
which:

1. Loads active sources for the kind (astock/fund) from `data_source` table,
   ordered by `config->>'priority'` ASC (lower = tried first).
2. For each source in priority order:
   a. Check circuit breaker (skip if open).
   b. Check required env (e.g. TUSHARE_TOKEN) — skip cleanly if missing.
   c. Compute start = max(watermark, requested_start).
   d. fetch via Fetcher; on SourceUnavailable → mark failure, try next.
3. Persist raw_payload before parsing (Kimi "raw 先行").
4. Return (source_used, rows) on first success.
5. If all sources fail, raise SourceUnavailable with aggregate error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .base import (
    Fetcher,
    SourceMisconfigured,
    SourceUnavailable,
    CircuitOpen,
)

# Lazy import removed — needed at module top so tests can monkeypatch it.
# (No circular dep: db.py does not import from registry.)
from ..db import acquire as _db_acquire

log = logging.getLogger(__name__)


# ============================================================
# Lazy Fetcher registry — instantiated on first use, cached per process
# ============================================================

_FETCHERS: dict[str, Fetcher] = {}


def _get_fetcher(source: str, db_row: dict) -> Fetcher:
    """Instantiate (or reuse) the Fetcher for a given source name."""
    if source in _FETCHERS:
        return _FETCHERS[source]

    # asyncpg returns JSONB as a JSON string by default; coerce to dict.
    raw_config = db_row.get("config")
    if isinstance(raw_config, str):
        config = json.loads(raw_config) if raw_config else {}
    elif isinstance(raw_config, dict):
        config = raw_config
    else:
        config = {}
    rate = float(config.get("rate_limit_per_sec", 2.0))
    retries = int(config.get("max_retries", 3))
    threshold = int(config.get("failure_threshold", 5))
    cooldown = float(config.get("cooldown_seconds", 300))
    # Per-fetch hard timeout (seconds). The sync call runs in a subprocess;
    # on expiry the parent SIGKILLs the child. Bounds CPU usage when an
    # upstream library misbehaves (e.g. baostock busy-looping on CLOSE_WAIT).
    request_timeout = float(config.get("request_timeout_seconds", 60.0))

    fetcher: Fetcher
    if source == "akshare_astock":
        from .akshare import AKShareAStockFetcher
        fetcher = AKShareAStockFetcher(rate, retries, threshold, cooldown, request_timeout)
    elif source == "akshare_fund":
        from .akshare import AKShareFundFetcher
        fetcher = AKShareFundFetcher(rate, retries, threshold, cooldown, request_timeout)
    elif source == "baostock_astock":
        from .baostock import BaoStockAStockFetcher
        fetcher = BaoStockAStockFetcher(rate, retries, threshold, cooldown, request_timeout)
    elif source == "tushare_astock":
        from .tushare import TushareAStockFetcher
        try:
            fetcher = TushareAStockFetcher(rate, retries, threshold, cooldown, request_timeout)
        except SourceMisconfigured as e:
            log.info("tushare_astock skipped: %s", e)
            raise
    else:
        raise ValueError(f"unknown source: {source}")

    _FETCHERS[source] = fetcher
    return fetcher


# ============================================================
# data_source table helpers
# ============================================================

async def list_active_sources(conn, domain: str) -> list[dict]:
    """Read data_source rows for `domain`, ordered by priority ASC.
    Excludes rows where priority IS NULL (unconfigured).
    Returns list of {source, kind, domain, config (as dict), last_success_at, consecutive_errors}."""
    rows = await conn.fetch(
        """
        SELECT source, kind, domain,
               (config)::text AS config_text,
               last_run_at, last_success_at, consecutive_errors
        FROM data_source
        WHERE domain = $1
          AND (config->>'priority') IS NOT NULL
        ORDER BY (config->>'priority')::int ASC
        """,
        domain,
    )
    out = []
    for r in rows:
        d = dict(r)
        # decode JSONB → dict here, once, for all downstream callers
        cfg_text = d.pop("config_text", None)
        d["config"] = json.loads(cfg_text) if cfg_text else {}
        out.append(d)
    return out


async def mark_source_success(conn, source: str):
    await conn.execute(
        """
        UPDATE data_source
        SET last_run_at = now(),
            last_success_at = now(),
            consecutive_errors = 0
        WHERE source = $1
        """,
        source,
    )


async def mark_source_failure(conn, source: str):
    await conn.execute(
        """
        UPDATE data_source
        SET last_run_at = now(),
            consecutive_errors = consecutive_errors + 1
        WHERE source = $1
        """,
        source,
    )


async def update_job_run_source(conn, job_id: int, source_used: str | None, fail_reason: str | None = None):
    await conn.execute(
        "UPDATE job_run SET source_used = $2, fail_reason = $3 WHERE id = $1",
        job_id, source_used, fail_reason,
    )


# ============================================================
# raw_payload persistence (Kimi "raw 先行")
# ============================================================

async def save_raw_payload(conn, *, source: str, endpoint: str, params: dict, payload: Any) -> int:
    """Persist raw API response to raw_payload table. Returns the row id.
    Sets parse_status = 'pending'; caller updates to 'ok' or 'error' after parsing."""
    row = await conn.fetchrow(
        """
        INSERT INTO raw_payload(source, endpoint, params, payload)
        VALUES ($1, $2, $3::jsonb, $4::jsonb)
        RETURNING id
        """,
        source, endpoint, json.dumps(params), json.dumps(payload, default=str),
    )
    return row["id"]


async def mark_raw_parsed(conn, raw_id: int, ok: bool, error: str | None = None):
    status = "ok" if ok else "error"
    await conn.execute(
        "UPDATE raw_payload SET parsed_at = now(), parse_status = $2 WHERE id = $1",
        raw_id, status,
    )
    if not ok:
        await conn.execute(
            "UPDATE raw_payload SET payload = payload || $2::jsonb WHERE id = $1",
            raw_id, json.dumps({"_parse_error": error}),
        )


# ============================================================
# Watermark-based incremental fetch
# ============================================================

def _watermark_start(conn, source: str, kind: str, requested_start: date) -> date:
    """Compute effective start date: max(last_success_at, requested_start).
    For first run (last_success_at NULL), use requested_start.
    Conservative default: subtract 7 days as a safety buffer for late-published bars."""
    # Caller fetches the row separately; we just compute the date here
    # given the inputs.
    return requested_start  # caller passes in last_success_at; this is a pure function


async def compute_effective_start(conn, source: str, requested_start: date, buffer_days: int = 7) -> date:
    """For watermark-based incremental fetch: start = max(last_success_at - buffer_days, requested_start).
    buffer_days accounts for late corrections / restated values."""
    from datetime import timedelta
    row = await conn.fetchrow("SELECT last_success_at FROM data_source WHERE source = $1", source)
    if row is None or row["last_success_at"] is None:
        return requested_start
    last = row["last_success_at"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    last_date = last.date() - timedelta(days=buffer_days)
    return max(last_date, requested_start)


# ============================================================
# Main entrypoint: fetch_with_failover
# ============================================================

@dataclass
class FetchResult:
    source: str          # which source actually served the data
    rows: list[dict]
    raw_id: int | None   # raw_payload row id (for audit / replay)


async def fetch_with_failover(
    *,
    domain: str,
    symbol: str,
    start: date,
    end: date,
    persist_raw: bool = True,
) -> FetchResult:
    """Try each active source in priority order. Return on first success.

    On every successful fetch, persist raw_payload (if persist_raw=True) and
    parse rows in the same flow. Source chain is read fresh from DB each call
    so config changes take effect without restart.
    """
    errors: list[str] = []
    async with _db_acquire() as conn:
        sources = await list_active_sources(conn, domain)
        if not sources:
            raise SourceUnavailable(f"no active sources configured for domain={domain!r}")

        for src_row in sources:
            source = src_row["source"]
            try:
                fetcher = _get_fetcher(source, src_row)
            except SourceMisconfigured as e:
                log.info("skip %s: %s", source, e)
                continue

            # Watermark-based start
            effective_start = await compute_effective_start(conn, source, start)

            try:
                # Check circuit breaker before doing work
                fetcher.breaker.check()
            except CircuitOpen as e:
                log.info("skip %s: %s", source, e)
                continue

            try:
                raw = await fetcher.fetch_raw(symbol, effective_start, end)

                # raw 先行: persist before parsing
                raw_id = None
                if persist_raw:
                    raw_id = await save_raw_payload(
                        conn,
                        source=source,
                        endpoint=fetcher.__class__.__name__,
                        params={"symbol": symbol, "start": str(effective_start), "end": str(end)},
                        payload=raw,
                    )

                # Parse
                try:
                    rows = fetcher.parse(raw, symbol)
                except Exception as e:
                    if raw_id is not None:
                        await mark_raw_parsed(conn, raw_id, ok=False, error=str(e))
                    log.warning("parse failed for %s via %s: %s", symbol, source, e)
                    continue  # try next source

                if raw_id is not None:
                    await mark_raw_parsed(conn, raw_id, ok=True)

                # Mark success
                await mark_source_success(conn, source)

                if not rows:
                    log.info("%s returned 0 rows for %s", source, symbol)
                    # Don't fail over on empty data — that's valid (e.g. no trades today)
                    return FetchResult(source=source, rows=[], raw_id=raw_id)

                return FetchResult(source=source, rows=rows, raw_id=raw_id)

            except (SourceUnavailable, CircuitOpen) as e:
                await mark_source_failure(conn, source)
                errors.append(f"{source}: {e}")
                log.warning("source %s failed for %s: %s", source, symbol, e)
                continue
            except Exception as e:
                await mark_source_failure(conn, source)
                errors.append(f"{source}: {e}")
                log.exception("unexpected error from %s for %s: %s", source, symbol, e)
                continue

        # All sources exhausted
        raise SourceUnavailable(
            f"all sources failed for {symbol} ({domain}): {'; '.join(errors) or 'no eligible sources'}"
        )