"""Factor registry — decorator-based registration + lookup.

A Factor is a pure function: OHLCV DataFrame in, DataFrame with added `value`
column out. NaN where lookback isn't satisfied (e.g. first 20 days).

Usage:
    @register("momentum_20d")
    def momentum_20d(df):
        return df.with_columns(
            (pl.col("close") / pl.col("close").shift(20) - 1).alias("value")
        )

The factor version is attached at registration time (FACTOR_VERSION) so the
caller doesn't need to pass it on every invocation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import polars as pl

from .version import FACTOR_VERSION

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Factor:
    name: str
    fn: Callable[[pl.DataFrame], pl.DataFrame]
    version: str = FACTOR_VERSION
    description: str = ""


_REGISTRY: dict[str, Factor] = {}


def register(name: str, *, version: str = FACTOR_VERSION, description: str = ""):
    """Decorator: register a function as a factor under `name`."""
    def deco(fn):
        if name in _REGISTRY:
            raise ValueError(f"factor {name!r} already registered")
        _REGISTRY[name] = Factor(name=name, fn=fn, version=version, description=description)
        log.info("registered factor %s (v=%s)", name, version)
        return fn
    return deco


def get(name: str) -> Factor:
    if name not in _REGISTRY:
        raise KeyError(f"unknown factor {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def all_factors() -> dict[str, Factor]:
    """Return a copy of the registry (for iteration without mutation)."""
    return dict(_REGISTRY)


def list_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def materialize_for_storage(df: pl.DataFrame, factor: Factor, symbol: str) -> pl.DataFrame:
    """Take a factor's output (df with `value` column) and add storage columns:
    symbol, feature, calc_date, factor_version. Drops OHLCV columns.

    Input: df with [trade_date, open, high, low, close, volume, value]
    Output: df with [symbol, feature, calc_date, value, factor_version]
    """
    return df.select([
        pl.lit(symbol).alias("symbol"),
        pl.lit(factor.name).alias("feature"),
        pl.col("trade_date").alias("calc_date"),
        pl.col("value").alias("value"),
        pl.lit(factor.version).alias("factor_version"),
    ])