"""Built-in factors — 6 baseline factors covering the standard
momentum / volatility / trend / oscillator / volume categories.

Each factor takes a polars DataFrame with columns
[trade_date, open, high, low, close, volume]
and returns the same DataFrame with an added `value` column.

NaN where the lookback window isn't satisfied (e.g. day 1-19 of a
20-day momentum will be null).
"""
from __future__ import annotations

import polars as pl

from .registry import register


# ============================================================
# Momentum
# ============================================================

@register(
    "momentum_20d",
    description="20-day price momentum: close[t]/close[t-20] - 1",
)
def momentum_20d(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        (pl.col("close") / pl.col("close").shift(20) - 1).alias("value")
    )


@register(
    "momentum_60d",
    description="60-day price momentum: close[t]/close[t-60] - 1",
)
def momentum_60d(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        (pl.col("close") / pl.col("close").shift(60) - 1).alias("value")
    )


# ============================================================
# Volatility
# ============================================================

@register(
    "volatility_20d",
    description="20-day rolling std of daily log-returns (annualized: * sqrt(252))",
)
def volatility_20d(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        (
            pl.col("close").log().diff().rolling_std(window_size=20) * (252 ** 0.5)
        ).alias("value")
    )


# ============================================================
# Trend
# ============================================================

@register(
    "ma_cross_5_20",
    description="5-day MA minus 20-day MA, normalized by 20-day MA",
)
def ma_cross_5_20(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        (
            (pl.col("close").rolling_mean(window_size=5)
             - pl.col("close").rolling_mean(window_size=20))
            / pl.col("close").rolling_mean(window_size=20)
        ).alias("value")
    )


# ============================================================
# Oscillator
# ============================================================

@register(
    "rsi_14",
    description="14-day Relative Strength Index (0-100)",
)
def rsi_14(df: pl.DataFrame) -> pl.DataFrame:
    """Wilder's RSI: separate rolling mean of gains and losses over 14 days."""
    delta = pl.col("close").diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    avg_gain = gain.rolling_mean(window_size=14)
    avg_loss = loss.rolling_mean(window_size=14)
    # Wilder smoothing approximation; true Wilder uses RMA which polars doesn't expose directly.
    # For Phase 2.1 baseline, simple rolling mean is close enough.
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return df.with_columns(rsi.alias("value"))


# ============================================================
# Volume
# ============================================================

@register(
    "volume_ratio_5_20",
    description="5-day average volume / 20-day average volume",
)
def volume_ratio_5_20(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        (
            pl.col("volume").rolling_mean(window_size=5)
            / pl.col("volume").rolling_mean(window_size=20)
        ).alias("value")
    )