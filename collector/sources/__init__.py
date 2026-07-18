"""Data source abstractions for quant-lab (Phase 2.0).

Each Fetcher implements a uniform interface so the registry can do
failover + watermark-based incremental fetch without caring which
upstream library is used.

Layers:
  base.py             — Fetcher ABC + exceptions + retry/rate-limit/circuit-breaker
  subprocess_runner.py — run sync calls in a SIGKILLable subprocess (CPU-spin guard)
  _runner.py          — standalone entrypoint invoked by subprocess_runner
  akshare.py          — AKShareAStockFetcher, AKShareFundFetcher
  baostock.py         — BaoStockAStockFetcher (demoted to last-resort)
  tushare.py          — TushareAStockFetcher (token-gated)
  registry.py         — failover chain + watermark + raw_payload persistence
"""
from .base import (
    Fetcher,
    SourceUnavailable,
    SourceMisconfigured,
    CircuitOpen,
    RateLimiter,
    CircuitBreaker,
    with_retry,
)
from .subprocess_runner import run_sync_in_subprocess
from .registry import (
    fetch_with_failover,
    save_raw_payload,
    mark_raw_parsed,
    list_active_sources,
    mark_source_success,
    mark_source_failure,
    compute_effective_start,
)

__all__ = [
    "Fetcher",
    "SourceUnavailable",
    "SourceMisconfigured",
    "CircuitOpen",
    "RateLimiter",
    "CircuitBreaker",
    "with_retry",
    "run_sync_in_subprocess",
    "fetch_with_failover",
    "save_raw_payload",
    "mark_raw_parsed",
    "list_active_sources",
    "mark_source_success",
    "mark_source_failure",
    "compute_effective_start",
]