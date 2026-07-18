"""Rolling IC weight — information coefficient per factor over a window.

Per Kimi: "权重 w_i = 该因子过去 252 日的滚动 IC(IC 衰减自动降权)".

IC(factor, forward_return) = Spearman rank correlation between today's factor
value and the N-day forward return. Computed per (feature) on a rolling
window so weights decay as factors lose predictive power.

Output: a per-factor weight vector (default length = number of features),
one weight per factor. Used by composite.py to weight votes.

For Phase 2.2, the IC is computed against `forward_return_N` columns that
the caller is expected to materialize on the OHLCV df (forward_return_5d,
forward_return_20d). If no forward returns are available, all factors get
equal weight (1.0 / N).

Implementation note: we compute Spearman via numpy rank + Pearson on the
ranks — equivalent to scipy.stats.spearmanr, but avoids a scipy dependency.
"""
from __future__ import annotations

import logging

import numpy as np
import polars as pl

log = logging.getLogger(__name__)

DEFAULT_WINDOW = 252  # trading days, per Kimi
DEFAULT_FORWARD_DAYS = 20  # N-day forward return target


def _rank(x: np.ndarray) -> np.ndarray:
    """Average-rank tie-breaking, NaN-safe (NaNs go to the end and get filtered)."""
    # Mask out NaNs; scipy.rankdata would be cleaner but again, no scipy.
    mask = ~np.isnan(x)
    out = np.empty_like(x, dtype=float)
    out[~mask] = np.nan
    valid = x[mask]
    if valid.size == 0:
        return out
    # Order gives ascending sort indices; average ranks via searchsorted.
    order = np.argsort(valid, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    sorted_vals = valid[order]
    # Find run boundaries to assign average ranks to ties.
    starts = np.searchsorted(sorted_vals, sorted_vals, side="left")
    ends = np.searchsorted(sorted_vals, sorted_vals, side="right")
    ranks[order] = (starts + ends + 1) / 2.0
    out[mask] = ranks
    return out


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation; NaN-safe; returns 0.0 on insufficient data."""
    if len(x) < 5:
        return 0.0
    rx = _rank(x.astype(float))
    ry = _rank(y.astype(float))
    # Drop pairs where either rank is NaN
    mask = ~(np.isnan(rx) | np.isnan(ry))
    if mask.sum() < 5:
        return 0.0
    rx, ry = rx[mask], ry[mask]
    if np.std(rx) == 0 or np.std(ry) == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def compute_factor_ics(
    factor_df: pl.DataFrame,
    forward_returns_df: pl.DataFrame,
    *,
    feature_col: str = "feature",
    symbol_col: str = "symbol",
    calc_date_col: str = "calc_date",
    value_col: str = "value",
    fwd_col: str = "forward_return_20d",
    window: int = DEFAULT_WINDOW,
) -> dict[str, float]:
    """Compute rolling IC per feature.

    Inputs:
      factor_df: long format with [symbol, feature, calc_date, value]
      forward_returns_df: wide-ish format with [symbol, calc_date, fwd_col]
                         where fwd_col is the forward return on calc_date.

    Returns: dict {feature_name: ic_value} — the latest IC over the last
             `window` trading days. Range [-1, 1].
    """
    # Join factor values with forward returns
    joined = factor_df.join(
        forward_returns_df.select([symbol_col, calc_date_col, fwd_col]),
        on=[symbol_col, calc_date_col],
        how="inner",
    ).filter(pl.col(value_col).is_not_null() & pl.col(fwd_col).is_not_null())

    ics: dict[str, float] = {}
    features = joined[feature_col].unique().to_list()
    for feat in features:
        sub = joined.filter(pl.col(feature_col) == feat).sort(calc_date_col).tail(window)
        if len(sub) < 5:
            ics[feat] = 0.0
            continue
        x = sub[value_col].to_numpy()
        y = sub[fwd_col].to_numpy()
        ics[feat] = _spearman(x, y)
    return ics


def ics_to_weights(ics: dict[str, float], *, eps: float = 0.0) -> dict[str, float]:
    """Convert raw IC values to non-negative weights. Negative IC → 0 (or eps).
    Sum of weights is preserved (=1) so the composite score is on a known scale.

    eps > 0 lets you keep weakly-negative-IC factors in the model with a small
    floor weight, which smooths transitions when a factor flips sign.
    """
    if not ics:
        return {}
    pos = {k: max(v, eps) for k, v in ics.items()}
    total = sum(pos.values())
    if total == 0:
        # All ICs ≤ 0: fall back to equal weighting
        n = len(ics)
        return {k: 1.0 / n for k in ics}
    return {k: v / total for k, v in pos.items()}


def equal_weights(features: list[str]) -> dict[str, float]:
    """Fallback when IC computation isn't possible (no forward returns yet)."""
    if not features:
        return {}
    w = 1.0 / len(features)
    return {f: w for f in features}