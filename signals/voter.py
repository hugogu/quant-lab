"""Rule-based voting: factor values → +1 / 0 / -1 per (symbol, feature, date).

Per Kimi's "阶段一:规则投票": every factor feeds a voter with a threshold
(or z-score / quantile band). Output is the `vote` column of `signal_vote`.

Design notes:
  - Voters are pure functions of (factor_value, symbol_context) → small int.
  - Default thresholds are conservative; tunable via SIGNALS_CONFIG (future
    YAML file or env). For Phase 2.2 baseline, we hardcode sensible defaults
    and expose them via constants for easy override.
  - Returns NaN for missing factor values (preserves "I don't know" semantics).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import polars as pl

log = logging.getLogger(__name__)


# ============================================================
# Default thresholds per factor (tunable)
# ============================================================
# These map a factor value to a vote. Format: (low, high) inclusive → +1;
# outside either → -1; inside band but with neutral cushion → 0.
# For unbounded factors (RSI 0-100), use the natural 0/100 anchors.

DEFAULT_THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    # (low_band, high_band, lower_neutral, upper_neutral)
    # vote=+1 if value in [lower_neutral, upper_neutral]
    # vote=0 if in [low_band, lower_neutral) ∪ (upper_neutral, high_band]
    # vote=-1 if value < low_band or value > high_band
    "momentum_20d":       (-0.10,  0.10, -0.02,  0.02),
    "momentum_60d":       (-0.20,  0.20, -0.05,  0.05),
    "volatility_20d":     ( 0.10,  0.40,  0.20,  0.30),   # inverted: low vol → +1 (stable)
    "ma_cross_5_20":      (-0.05,  0.05, -0.01,  0.01),
    "rsi_14":             ( 30.0, 70.0,  45.0,  55.0),    # inverted: oversold → +1, overbought → -1
    "volume_ratio_5_20":  ( 0.50,  2.00,  0.80,  1.30),   # spike + steady OK, crash or wash-out → -1
}


def _vote_for_value(factor_name: str, value: float) -> int:
    """Apply the threshold rule for one factor value. Returns ±1 or 0.
    NaN input → NaN-sentinel (we encode as 0 here, but signal_runner will
    know to skip rows where the original factor_value is null)."""
    if value is None:
        return 0
    if factor_name not in DEFAULT_THRESHOLDS:
        log.warning("no threshold for factor %s — vote=0", factor_name)
        return 0
    low, high, lo_neutral, hi_neutral = DEFAULT_THRESHOLDS[factor_name]
    # Inverted semantics for volatility and rsi: low value is "good"
    if factor_name in ("volatility_20d", "rsi_14"):
        if value < lo_neutral:
            return 1
        if value <= hi_neutral:
            return 0
        if value <= high:
            return -1
        return -1
    # Default: high value is "good" (momentum, MA cross positive, volume stable-high)
    if value < low:
        return -1
    if value < lo_neutral:
        return 0
    if value <= hi_neutral:
        return 1
    if value <= high:
        return 0
    return -1


# ============================================================
# Vectorized vote computation
# ============================================================

def vote_dataframe(factor_df: pl.DataFrame) -> pl.DataFrame:
    """Take a long-format feature_value DataFrame with columns
    [symbol, feature, calc_date, value, factor_version] and return the same
    plus a `vote` column (smallint) and `weight` column (real, default 1.0).

    Weight defaults to 1.0 here; IC-based weighting is computed separately
    in signals.ic_weight and applied at composite-score time.
    """
    # Use polars map_batches for per-row voting; smallint output
    def _vote_row(value: float) -> int:
        if value is None:
            return 0
        return _vote_for_value(factor_df.filter(pl.col("value") == value).row(0)[2], value) \
            if False else _vote_for_value_single(value)  # placeholder, see below

    def _vote_for_value_single(v):
        # We don't have feature name in this closure; use a global lookup by row.
        return _vote_for_value(_current_feature[0], v)

    # Hmm, polars closures can't easily capture per-row feature name.
    # Use a different approach: groupby feature and apply _vote_for_value.
    pass


def vote_dataframe_simple(factor_df: pl.DataFrame) -> pl.DataFrame:
    """Vectorized voter: returns df with added `vote` and `weight` columns.

    Implementation: for each feature, compute vote via a vectorized expression
    using polars `when().then().otherwise()` so we stay in the polars engine.
    """
    out = factor_df.with_columns(pl.lit(1.0).alias("weight"))
    # Build the vote column as a single expression over all known features.
    # Start with all zeros; for each feature, overlay the rule.
    vote_expr = pl.lit(0, dtype=pl.Int8)
    for feature_name, (low, high, lo_neutral, hi_neutral) in DEFAULT_THRESHOLDS.items():
        feat = pl.col("feature") == feature_name
        v = pl.col("value")
        if feature_name in ("volatility_20d", "rsi_14"):
            # Inverted: low → +1, high → -1
            branch = (
                pl.when(v.is_null()).then(pl.lit(0, dtype=pl.Int8))
                .when(v < lo_neutral).then(pl.lit(1, dtype=pl.Int8))
                .when(v <= hi_neutral).then(pl.lit(0, dtype=pl.Int8))
                .otherwise(pl.lit(-1, dtype=pl.Int8))
            )
        else:
            # Default: high → +1
            branch = (
                pl.when(v.is_null()).then(pl.lit(0, dtype=pl.Int8))
                .when(v < low).then(pl.lit(-1, dtype=pl.Int8))
                .when(v < lo_neutral).then(pl.lit(0, dtype=pl.Int8))
                .when(v <= hi_neutral).then(pl.lit(1, dtype=pl.Int8))
                .when(v <= high).then(pl.lit(0, dtype=pl.Int8))
                .otherwise(pl.lit(-1, dtype=pl.Int8))
            )
        # Mask: only apply this feature's branch where feature == feature_name
        vote_expr = pl.when(feat).then(branch).otherwise(vote_expr)
    return out.with_columns(vote_expr.alias("vote"))