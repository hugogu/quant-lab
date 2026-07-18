"""Unit tests for the data source layer.

Run: docker compose exec api pytest tests/test_sources.py -v
or:   python -m pytest tests/test_sources.py -v (if pytest installed locally)
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collector.sources.base import (
    CircuitBreaker,
    CircuitOpen,
    Fetcher,
    RateLimiter,
    SourceMisconfigured,
    SourceUnavailable,
    with_retry,
)
from collector.sources.akshare import AKShareAStockFetcher
from collector.sources.baostock import BaoStockAStockFetcher, _to_bs_code
from collector.sources.tushare import TushareAStockFetcher, _to_ts_code


# ============================================================
# Helpers
# ============================================================

class FakeFetcher(Fetcher):
    """Fetcher that returns canned raw payloads, for testing base behavior."""
    source = "fake"
    _payload: dict = {"records": []}
    _exception: Exception | None = None

    async def fetch_raw(self, symbol, start, end):
        if self._exception:
            raise self._exception
        return self._payload

    def parse(self, raw, symbol):
        return raw.get("records", [])


# ============================================================
# CircuitBreaker
# ============================================================

def test_circuit_breaker_starts_closed():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
    assert cb.state == "closed"


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"


def test_circuit_breaker_check_raises_when_open():
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10)
    cb.record_failure()
    with pytest.raises(CircuitOpen):
        cb.check()


def test_circuit_breaker_recovers_after_cooldown():
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
    cb.record_failure()
    assert cb.state == "open"
    import time; time.sleep(0.1)
    assert cb.state == "half-open"
    cb.check()  # half-open allows the call
    cb.record_success()
    assert cb.state == "closed"


def test_circuit_breaker_reopens_on_half_open_failure():
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
    cb.record_failure()
    import time; time.sleep(0.1)
    cb.check()
    cb.record_failure()
    assert cb.state == "open"


# ============================================================
# RateLimiter
# ============================================================

@pytest.mark.asyncio
async def test_rate_limiter_blocks_when_exhausted():
    rl = RateLimiter(rate_per_sec=10)  # 10/sec capacity
    # Drain
    for _ in range(10):
        await rl.acquire()
    # Next one should still complete (waits for refill)
    import time
    start = time.monotonic()
    await rl.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05, f"expected wait, got {elapsed:.3f}s"


# ============================================================
# Fetcher retry behavior
# ============================================================

@pytest.mark.asyncio
async def test_fetcher_returns_rows_on_success():
    f = FakeFetcher(rate_limit_per_sec=1000, max_retries=2, failure_threshold=3)
    f._payload = {"records": [{"a": 1}, {"a": 2}]}
    rows = await f.fetch("000001", date(2026, 1, 1), date(2026, 1, 7))
    assert rows == [{"a": 1}, {"a": 2}]


@pytest.mark.asyncio
async def test_fetcher_retries_on_source_unavailable():
    f = FakeFetcher(rate_limit_per_sec=1000, max_retries=3, failure_threshold=10)
    f._exception = SourceUnavailable("rate limited")
    with pytest.raises(SourceUnavailable):
        await f.fetch("000001", date(2026, 1, 1), date(2026, 1, 7))
    # Circuit breaker should NOT have tripped because failure_threshold=10 and we only did 3 retries
    assert f.breaker.state == "closed"


@pytest.mark.asyncio
async def test_fetcher_does_not_retry_misconfigured():
    f = FakeFetcher(rate_limit_per_sec=1000, max_retries=3)
    f._exception = SourceMisconfigured("token missing")
    with pytest.raises(SourceMisconfigured):
        await f.fetch("000001", date(2026, 1, 1), date(2026, 1, 7))


@pytest.mark.asyncio
async def test_fetcher_trips_breaker_after_repeated_failures():
    """Three separate fetches, each max_retries=1 → 3 consecutive failures → breaker opens."""
    f = FakeFetcher(rate_limit_per_sec=1000, max_retries=1, failure_threshold=3, cooldown_seconds=300)
    f._exception = SourceUnavailable("nope")
    for _ in range(3):
        with pytest.raises(SourceUnavailable):
            await f.fetch("000001", date(2026, 1, 1), date(2026, 1, 7))
    assert f.breaker.state == "open"


# ============================================================
# Symbol-format converters
# ============================================================

def test_baostock_code_inference():
    assert _to_bs_code("000001") == "sz.000001"
    assert _to_bs_code("600519") == "sh.600519"
    assert _to_bs_code("688981") == "sh.688981"
    assert _to_bs_code("300750") == "sz.300750"
    assert _to_bs_code("sz000001") == "sz.000001"
    assert _to_bs_code("sh600519") == "sh.600519"
    assert _to_bs_code("SH600519") == "sh.600519"
    with pytest.raises(ValueError):
        _to_bs_code("xxxxx")


def test_tushare_code_inference():
    assert _to_ts_code("000001") == "000001.SZ"
    assert _to_ts_code("600519") == "600519.SH"
    assert _to_ts_code("688981") == "688981.SH"
    assert _to_ts_code("300750") == "300750.SZ"
    assert _to_ts_code("830799") == "830799.BJ"
    assert _to_ts_code("000001.SZ") == "000001.SZ"  # already in ts format


# ============================================================
# AkShare row schema
# ============================================================

def test_akshare_astock_parse():
    f = AKShareAStockFetcher(rate_limit_per_sec=1000, max_retries=1)
    raw = {
        "symbol": "000001",
        "records": [
            {"日期": "20260101", "开盘": 10.0, "最高": 11.0, "最低": 9.5, "收盘": 10.5,
             "成交量": 1000000, "成交额": 10500000.0}
        ]
    }
    rows = f.parse(raw, "000001")
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "000001"
    assert r["trade_date"] == "20260101"
    assert r["open"] == 10.0
    assert r["close"] == 10.5
    assert r["volume"] == 1000000
    assert r["source"] == "akshare_astock"


def test_akshare_astock_parse_skips_malformed():
    f = AKShareAStockFetcher(rate_limit_per_sec=1000, max_retries=1)
    raw = {
        "symbol": "000001",
        "records": [
            {"日期": "20260101", "开盘": 10.0, "最高": 11.0, "最低": 9.5, "收盘": 10.5,
             "成交量": 1000000, "成交额": 10500000.0},
            {"日期": "20260102", "开盘": "bad"},  # malformed
            {"日期": "20260103", "开盘": 11.0, "最高": 12.0, "最低": 10.5, "收盘": 11.5,
             "成交量": 1100000, "成交额": 11500000.0},
        ]
    }
    rows = f.parse(raw, "000001")
    assert len(rows) == 2


# ============================================================
# BaoStock row schema
# ============================================================

def test_baostock_parse():
    f = BaoStockAStockFetcher(rate_limit_per_sec=1000, max_retries=1)
    # baostock returns tuples from get_row_data() when fields are specified
    raw = {
        "symbol": "000001", "bs_code": "sz.000001",
        "records": [
            ("2026-01-01", "10.0", "11.0", "9.5", "10.5", "1000000", "10500000")
        ]
    }
    rows = f.parse(raw, "000001")
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_date"] == "20260101"  # normalized from YYYY-MM-DD
    assert r["open"] == 10.0
    assert r["close"] == 10.5
    assert r["source"] == "baostock_astock"


def test_baostock_parse_handles_empty_fields():
    """BaoStock returns empty strings for some fields; parser should produce None."""
    f = BaoStockAStockFetcher(rate_limit_per_sec=1000, max_retries=1)
    raw = {
        "symbol": "000001", "records": [
            ("2026-01-01", "", "", "", "10.5", "", "")
        ]
    }
    rows = f.parse(raw, "000001")
    assert rows[0]["open"] is None
    assert rows[0]["close"] == 10.5
    assert rows[0]["volume"] is None


# ============================================================
# Registry / failover (mocked)
# ============================================================

@pytest.mark.asyncio
async def test_registry_failover_to_next_source(monkeypatch):
    """When akshare fails SourceUnavailable, registry should try baostock."""
    from collector.sources import registry as reg_mod

    # Mock DB connection
    fake_conn = AsyncMock()
    fake_conn.fetch = AsyncMock(return_value=[
        {"source": "akshare_astock", "kind": "astock",
         "config": {"priority": 10, "rate_limit_per_sec": 1000, "max_retries": 1, "failure_threshold": 5},
         "last_run_at": None, "last_success_at": None, "consecutive_errors": 0},
        {"source": "baostock_astock", "kind": "astock",
         "config": {"priority": 20, "rate_limit_per_sec": 1000, "max_retries": 1, "failure_threshold": 5},
         "last_run_at": None, "last_success_at": None, "consecutive_errors": 0},
    ])
    fake_conn.execute = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value={"last_success_at": None})

    fake_pool_acquire = MagicMock()
    fake_pool_acquire.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool_acquire.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(reg_mod, "_db_acquire", lambda: fake_pool_acquire)
    reg_mod._FETCHERS.clear()

    # Akshare fails, baostock succeeds
    akshare_fetcher = reg_mod._get_fetcher("akshare_astock", fake_conn.fetch.return_value[0])
    async def akshare_fail(*a, **kw): raise SourceUnavailable("rate limited")
    akshare_fetcher.fetch_raw = akshare_fail

    baostock_fetcher = reg_mod._get_fetcher("baostock_astock", fake_conn.fetch.return_value[1])
    async def baostock_ok(symbol, start, end):
        return {"symbol": symbol, "records": [{"date": "2026-01-01", "open": "10.0"}]}
    baostock_fetcher.fetch_raw = baostock_ok

    result = await reg_mod.fetch_with_failover(
        domain="astock", symbol="000001",
        start=date(2026, 1, 1), end=date(2026, 1, 7),
        persist_raw=False,
    )
    assert result.source == "baostock_astock"
    assert len(result.rows) == 1


@pytest.mark.asyncio
async def test_registry_skips_source_with_open_circuit(monkeypatch):
    """If akshare's breaker is already open, registry should skip without calling."""
    from collector.sources import registry as reg_mod

    fake_conn = AsyncMock()
    fake_conn.fetch = AsyncMock(return_value=[
        {"source": "akshare_astock", "kind": "astock",
         "config": {"priority": 10, "rate_limit_per_sec": 1000, "max_retries": 1, "failure_threshold": 5},
         "last_run_at": None, "last_success_at": None, "consecutive_errors": 0},
        {"source": "baostock_astock", "kind": "astock",
         "config": {"priority": 20, "rate_limit_per_sec": 1000, "max_retries": 1, "failure_threshold": 5},
         "last_run_at": None, "last_success_at": None, "consecutive_errors": 0},
    ])
    fake_conn.execute = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value={"last_success_at": None})

    fake_pool_acquire = MagicMock()
    fake_pool_acquire.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool_acquire.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(reg_mod, "_db_acquire", lambda: fake_pool_acquire)
    reg_mod._FETCHERS.clear()

    # Trip akshare's breaker
    akshare_fetcher = reg_mod._get_fetcher("akshare_astock", fake_conn.fetch.return_value[0])
    for _ in range(5):
        akshare_fetcher.breaker.record_failure()

    baostock_fetcher = reg_mod._get_fetcher("baostock_astock", fake_conn.fetch.return_value[1])
    async def baostock_ok(symbol, start, end):
        return {"symbol": symbol, "records": []}
    baostock_fetcher.fetch_raw = baostock_ok

    result = await reg_mod.fetch_with_failover(
        domain="astock", symbol="000001",
        start=date(2026, 1, 1), end=date(2026, 1, 7),
        persist_raw=False,
    )
    assert result.source == "baostock_astock"


@pytest.mark.asyncio
async def test_registry_raises_when_all_sources_fail(monkeypatch):
    """If every source fails SourceUnavailable, registry should raise SourceUnavailable."""
    from collector.sources import registry as reg_mod

    fake_conn = AsyncMock()
    fake_conn.fetch = AsyncMock(return_value=[
        {"source": "akshare_astock", "kind": "astock",
         "config": {"priority": 10, "rate_limit_per_sec": 1000, "max_retries": 1, "failure_threshold": 5},
         "last_run_at": None, "last_success_at": None, "consecutive_errors": 0},
    ])
    fake_conn.execute = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value={"last_success_at": None})

    fake_pool_acquire = MagicMock()
    fake_pool_acquire.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool_acquire.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(reg_mod, "_db_acquire", lambda: fake_pool_acquire)
    reg_mod._FETCHERS.clear()

    fetcher = reg_mod._get_fetcher("akshare_astock", fake_conn.fetch.return_value[0])
    async def fail(*a, **kw): raise SourceUnavailable("blocked")
    fetcher.fetch_raw = fail

    with pytest.raises(SourceUnavailable) as exc:
        await reg_mod.fetch_with_failover(
            domain="astock", symbol="000001",
            start=date(2026, 1, 1), end=date(2026, 1, 7),
            persist_raw=False,
        )
    assert "akshare_astock" in str(exc.value)


# ============================================================
# with_retry decorator
# ============================================================

@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient_failure():
    calls = []

    @with_retry(max_attempts=3, base_delay=0.01)
    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("transient")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_with_retry_gives_up_after_max_attempts():
    calls = []

    @with_retry(max_attempts=3, base_delay=0.01)
    async def always_fail():
        calls.append(1)
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError):
        await always_fail()
    assert len(calls) == 3