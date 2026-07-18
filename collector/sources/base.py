"""Fetcher interface + cross-cutting infrastructure.

Per Kimi's design spec:
- Raw 先行 (caller persists raw_payload before parsing)
- 限速 + 熔断 (rate limiter per source, circuit breaker after consecutive failures)
- 多源 + 兜底 (Fetcher impls are interchangeable; registry handles failover)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

log = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class SourceUnavailable(Exception):
    """Upstream source is unreachable / rate-limited / returned unusable data.
    Failover should try the next source in the chain."""


class SourceMisconfigured(Exception):
    """Source is configured in data_source but its required env / secrets
    are missing. Distinct from SourceUnavailable so the registry can mark
    it as 'permanently down' instead of incrementing consecutive_errors."""


class CircuitOpen(Exception):
    """Circuit breaker is open for this source — fail fast without calling
    the upstream. Recovers after cooldown window."""


# ============================================================
# Rate limiter (simple in-memory token bucket, per process)
# ============================================================

class RateLimiter:
    """Thread-safe-ish token bucket. Capacity = rate per second.
    Refills continuously. Block callers that exceed capacity.
    For Phase 2.0 single-process worker, in-memory is sufficient.
    """

    def __init__(self, rate_per_sec: float):
        self.rate = float(rate_per_sec)
        self.capacity = max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                # Sleep until enough tokens accrue
                deficit = n - self.tokens
                wait = deficit / self.rate
                await asyncio.sleep(wait)


# ============================================================
# Circuit breaker
# ============================================================

@dataclass
class CircuitBreaker:
    """After N consecutive failures, opens for cooldown_seconds.
    Half-open after cooldown: next call goes through; success → close, failure → reopen.

    State is in-process. For multi-worker deploys, persist via data_source
    table (consecutive_errors + last_success_at) and rehydrate at boot.
    """
    failure_threshold: int = 5
    cooldown_seconds: float = 300.0  # 5 min default
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            return "half-open"
        return "open"

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._opened_at is None:
            # Closed → may open
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                log.warning(
                    "circuit breaker opening after %d consecutive failures (cooldown=%.0fs)",
                    self._consecutive_failures, self.cooldown_seconds,
                )
        else:
            # Open or half-open: any new failure re-opens (resets cooldown)
            self._opened_at = time.monotonic()
            log.warning("circuit breaker re-opening (was %s)", self.state)

    def check(self) -> None:
        """Raise CircuitOpen if breaker is open. Passes through in closed/half-open."""
        if self.state == "open":
            raise CircuitOpen(
                f"circuit open; retry in {self.cooldown_seconds - (time.monotonic() - self._opened_at):.0f}s"
            )


# ============================================================
# Fetcher ABC
# ============================================================

class Fetcher(ABC):
    """Abstract data source. Each implementation declares:
    - source name (matches data_source.source)
    - rate_limit_per_sec (token-bucket)
    - max_retries (per-call retry with exponential backoff)
    - failure_threshold (circuit breaker trip point)

    Subclasses implement:
    - fetch_raw(symbol, start, end) -> opaque payload (the bytes the upstream returned)
    - parse(raw_payload, symbol) -> list[dict] (rows normalized for upsert)

    The two-step design lets the registry persist raw_payload before parsing
    (per Kimi "raw 先行"). On parse failure we can replay from raw.
    """

    source: str = ""  # must match data_source.source

    def __init__(self, rate_limit_per_sec: float = 2.0, max_retries: int = 3,
                 failure_threshold: int = 5, cooldown_seconds: float = 300.0):
        self.rate_limiter = RateLimiter(rate_limit_per_sec)
        self.breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )
        self.max_retries = max_retries

    @abstractmethod
    async def fetch_raw(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        """Fetch raw payload from upstream. Should raise SourceUnavailable on
        transient failure, SourceMisconfigured if token/env missing."""

    @abstractmethod
    def parse(self, raw: dict[str, Any], symbol: str) -> list[dict]:
        """Parse raw payload into row dicts compatible with upsert."""

    async def fetch(self, symbol: str, start: date, end: date) -> list[dict]:
        """Public entrypoint: rate-limit + circuit-check + retry around fetch_raw + parse.
        Returns normalized rows. Raises SourceUnavailable after exhausting retries."""
        self.breaker.check()
        await self.rate_limiter.acquire()

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                raw = await self.fetch_raw(symbol, start, end)
                rows = self.parse(raw, symbol)
                self.breaker.record_success()
                return rows
            except SourceMisconfigured:
                # Permanent: don't retry, don't increment breaker counter
                raise
            except CircuitOpen:
                raise
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                log.warning(
                    "%s fetch attempt %d/%d failed for %s: %s — retry in %ds",
                    self.source, attempt + 1, self.max_retries, symbol, e, wait,
                )
                await asyncio.sleep(wait)

        self.breaker.record_failure()
        raise SourceUnavailable(f"{self.source} fetch failed for {symbol}: {last_err}")


# ============================================================
# Decorator: with_retry (for non-Fetcher helper code)
# ============================================================

def with_retry(max_attempts: int = 3, base_delay: float = 1.0):
    """Retry decorator with exponential backoff. Catches Exception broadly
    — use only for code paths where any error is worth retrying."""
    def deco(fn):
        async def wrapper(*args, **kwargs):
            last = None
            for i in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    last = e
                    if i < max_attempts - 1:
                        await asyncio.sleep(base_delay * (2 ** i))
            raise last
        return wrapper
    return deco