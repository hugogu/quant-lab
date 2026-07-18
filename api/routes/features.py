"""Feature endpoints — read pre-computed factor values from feature_value.

Features are computed nightly by `collector.factor_runner` (scheduler entry
at 17:30 weekdays). These endpoints are read-only.

Endpoints:
  GET /features/{symbol}                  all features for one symbol, time series
  GET /features/{symbol}/{feature_name}   one feature for one symbol, time series
  GET /features/latest?date=YYYY-MM-DD    cross-section of latest features as of date
  GET /features/list                      metadata: names + versions + descriptions
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from collector.db import acquire
from factors import all_factors, list_names

log = logging.getLogger(__name__)
router = APIRouter(prefix="/features", tags=["features"])


@router.get("/list")
async def list_feature_metadata():
    """Return registered factor names + versions + descriptions.

    Reads from the in-process registry (populated at API boot via
    `from factors import builtin`). For full historical accuracy, the registry
    version must match the factor_version in feature_value rows.
    """
    return [
        {
            "name": f.name,
            "version": f.version,
            "description": f.description,
        }
        for f in all_factors().values()
    ]


@router.get("/latest")
async def latest_features(
    date_: date | None = Query(None, alias="date"),
    limit: int = Query(50, ge=1, le=500),
):
    """Cross-section of the latest feature_value rows for each (symbol, feature)
    pair as of `date_` (default: today).

    Returns one row per (symbol, feature) — most recent value on/before date_.
    Useful for the Streamlit panel's "截面分位" view.
    """
    as_of = date_ or date.today()
    sql = """
    WITH latest AS (
        SELECT DISTINCT ON (symbol, feature)
               symbol, feature, calc_date, value, factor_version
        FROM feature_value
        WHERE calc_date <= $1
        ORDER BY symbol, feature, calc_date DESC
    )
    SELECT * FROM latest
    ORDER BY feature, symbol
    LIMIT $2
    """
    async with acquire() as conn:
        rows = await conn.fetch(sql, as_of, limit * len(list_names()))
    return [dict(r) for r in rows]


@router.get("/{symbol}")
async def symbol_features(
    symbol: str,
    days: int = Query(252, ge=1, le=1000),
):
    """All features for `symbol`, last `days` rows, ordered by (feature, calc_date DESC)."""
    sql = """
    SELECT symbol, feature, calc_date, value, factor_version
    FROM feature_value
    WHERE symbol = $1
      AND calc_date >= $2
    ORDER BY feature, calc_date DESC
    """
    since = date.today() - timedelta(days=days)
    async with acquire() as conn:
        rows = await conn.fetch(sql, symbol, since)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no features for {symbol}")
    return [dict(r) for r in rows]


@router.get("/{symbol}/{feature_name}")
async def one_feature(
    symbol: str,
    feature_name: str,
    days: int = Query(252, ge=1, le=1000),
):
    """Time series for one (symbol, feature) pair."""
    if feature_name not in list_names():
        raise HTTPException(
            status_code=404,
            detail=f"unknown feature {feature_name!r}; available: {list_names()}",
        )
    sql = """
    SELECT calc_date, value, factor_version
    FROM feature_value
    WHERE symbol = $1
      AND feature = $2
      AND calc_date >= $3
    ORDER BY calc_date ASC
    """
    since = date.today() - timedelta(days=days)
    async with acquire() as conn:
        rows = await conn.fetch(sql, symbol, feature_name, since)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"no rows for {symbol}/{feature_name}",
        )
    return [dict(r) for r in rows]