"""Composite signal: S = Σ w_i · v_i, then threshold into buy / hold / avoid.

Per Kimi's signal flow:
  factor values → rule votes (±1/0) → weighted by rolling IC → sum → band.

The composite score for one (symbol, date) is the weighted sum of votes
across all features for that row. Bands:
    S ≥ buy_threshold      → BUY
    S ≤ avoid_threshold    → AVOID
    otherwise              → HOLD

For Phase 2.2 the bands are heuristic (±0.15 / ±0.05). Future Phase 2.3 will
calibrate them against historical hit rates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import polars as pl

log = logging.getLogger(__name__)

# Default bands on the weighted-sum scale (range roughly [-1, +1]).
DEFAULT_BUY_THRESHOLD = 0.15
DEFAULT_AVOID_THRESHOLD = -0.15


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    as_of_date: object  # pl.Date / datetime
    score: float
    decision: str  # "buy" / "hold" / "avoid"
    contributing: dict[str, tuple[int, float]]  # feature → (vote, weight)


def composite_score_df(
    votes_df: pl.DataFrame,
    weights: dict[str, float],
    *,
    vote_col: str = "vote",
    weight_col: str = "weight",
    feature_col: str = "feature",
    symbol_col: str = "symbol",
    calc_date_col: str = "calc_date",
) -> pl.DataFrame:
    """Compute weighted composite score per (symbol, calc_date).

    Input votes_df has columns [..., feature, vote, weight].
    Returns df with [symbol, calc_date, score] where score = Σ weight * vote.
    """
    # Override per-row weight with the rolling-IC weight for that feature
    df = votes_df.with_columns(
        pl.col(feature_col).replace_strict(weights, default=0.0).alias("_w"),
    ).with_columns(
        (pl.col(vote_col).cast(pl.Float64) * pl.col("_w")).alias("_contrib")
    )
    return (
        df.group_by([symbol_col, calc_date_col])
        .agg(pl.col("_contrib").sum().alias("score"))
        .sort([symbol_col, calc_date_col])
    )


def decide(score: float, *, buy: float = DEFAULT_BUY_THRESHOLD, avoid: float = DEFAULT_AVOID_THRESHOLD) -> str:
    """Map a composite score to a decision."""
    if score >= buy:
        return "buy"
    if score <= avoid:
        return "avoid"
    return "hold"


def latest_decisions(
    scores_df: pl.DataFrame,
    *,
    symbol_col: str = "symbol",
    date_col: str = "calc_date",
    score_col: str = "score",
    buy: float = DEFAULT_BUY_THRESHOLD,
    avoid: float = DEFAULT_AVOID_THRESHOLD,
) -> pl.DataFrame:
    """Return the most-recent decision per symbol with its score + decision."""
    latest = (
        scores_df.sort(date_col, descending=True)
        .group_by(symbol_col, maintain_order=True)
        .first()
        .with_columns(
            pl.when(pl.col(score_col) >= buy).then(pl.lit("buy"))
            .when(pl.col(score_col) <= avoid).then(pl.lit("avoid"))
            .otherwise(pl.lit("hold"))
            .alias("decision")
        )
    )
    return latest