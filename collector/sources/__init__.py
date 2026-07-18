"""Data source abstractions for quant-lab (Phase 2.0).

Each Fetcher implements a uniform interface so the registry can do
failover + watermark-based incremental fetch without caring which
upstream library is used.

Layers:
  base.py        — Fetcher ABC + exceptions + retry/rate-limit/circuit-breaker
  akshare.py     — AKShareAStockFetcher, AKShareFundFetcher (refactor of legacy)
  baostock.py    — BaoStockAStockFetcher
  tushare.py     — TushareAStockFetcher (token-gated)
  registry.py    — failover chain + watermark + raw_payload persistence
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
    "fetch_with_failover",
    "save_raw_payload",
    "mark_raw_parsed",
    "list_active_sources",
    "mark_source_success",
    "mark_source_failure",
    "compute_effective_start",
]