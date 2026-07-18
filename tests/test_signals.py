"""Unit tests for the signals layer."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from signals import (
    vote_dataframe_simple,
    DEFAULT_THRESHOLDS,
    compute_factor_ics,
    ics_to_weights,
    equal_weights,
    composite_score_df,
    decide,
    latest_decisions,
)
from signals.voter import _vote_for_value


# ============================================================
# Voter — scalar
# ============================================================

def test_vote_momentum_positive_is_plus_one():
    # 0.01 sits inside the inner neutral band [-0.02, 0.02] → +1
    assert _vote_for_value("momentum_20d", 0.01) == 1


def test_vote_momentum_negative_is_minus_one():
    assert _vote_for_value("momentum_20d", -0.15) == -1


def test_vote_momentum_at_zero_is_plus_one():
    # 0.0 is inside the inner neutral band → +1
    assert _vote_for_value("momentum_20d", 0.0) == 1


def test_vote_momentum_in_outer_band_is_zero():
    # 0.05 is in the outer neutral band (0.02, 0.10] → 0
    assert _vote_for_value("momentum_20d", 0.05) == 0


def test_vote_unknown_factor_is_zero():
    assert _vote_for_value("not_a_factor", 100.0) == 0


def test_vote_none_is_zero():
    assert _vote_for_value("momentum_20d", None) == 0


def test_vote_rsi_oversold_is_plus_one():
    # RSI 25 → oversold → inverted rule → +1
    assert _vote_for_value("rsi_14", 25.0) == 1


def test_vote_rsi_overbought_is_minus_one():
    assert _vote_for_value("rsi_14", 80.0) == -1


def test_vote_volatility_high_is_minus_one():
    # High vol is bad → inverted → -1
    assert _vote_for_value("volatility_20d", 0.50) == -1


def test_vote_volatility_low_is_plus_one():
    # Low vol is good → inverted → +1
    assert _vote_for_value("volatility_20d", 0.10) == 1


# ============================================================
# Voter — vectorized
# ============================================================

def test_vote_dataframe_simple_basic():
    df = pl.DataFrame({
        "symbol": ["000001", "000001", "000002"],
        "feature": ["momentum_20d", "rsi_14", "momentum_20d"],
        "calc_date": [date(2026, 1, 1)] * 3,
        "value": [0.01, 25.0, -0.20],
        "factor_version": ["v1.0"] * 3,
    })
    out = vote_dataframe_simple(df)
    assert "vote" in out.columns
    assert "weight" in out.columns
    rows = out.sort(["symbol", "feature"]).to_dicts()
    by_key = {(r["symbol"], r["feature"]): r["vote"] for r in rows}
    assert by_key[("000001", "momentum_20d")] == 1
    assert by_key[("000001", "rsi_14")] == 1
    assert by_key[("000002", "momentum_20d")] == -1


def test_vote_dataframe_simple_handles_nulls():
    df = pl.DataFrame({
        "symbol": ["000001"],
        "feature": ["momentum_20d"],
        "calc_date": [date(2026, 1, 1)],
        "value": [None],
        "factor_version": ["v1.0"],
    })
    out = vote_dataframe_simple(df)
    assert out.row(0)[out.columns.index("vote")] == 0


# ============================================================
# IC weights
# ============================================================

def test_ics_to_weights_normalizes_to_one():
    ics = {"momentum_20d": 0.10, "rsi_14": 0.05, "volatility_20d": -0.02}
    weights = ics_to_weights(ics)
    assert pytest.approx(sum(weights.values())) == 1.0
    assert weights["volatility_20d"] == 0.0  # negative → dropped


def test_ics_to_weights_eps_floor_keeps_weak_factors():
    ics = {"a": -0.01, "b": 0.10}
    weights = ics_to_weights(ics, eps=0.01)
    assert weights["a"] == pytest.approx(0.01 / 0.11)
    assert weights["b"] == pytest.approx(0.10 / 0.11)


def test_ics_to_weights_all_negative_falls_back_to_equal():
    ics = {"a": -0.1, "b": -0.2, "c": -0.3}
    weights = ics_to_weights(ics)
    assert all(w == pytest.approx(1/3) for w in weights.values())


def test_equal_weights():
    w = equal_weights(["a", "b", "c", "d"])
    assert len(w) == 4
    assert pytest.approx(sum(w.values())) == 1.0


def test_compute_factor_ics_basic():
    """Spearman IC on a clean monotonic relationship should be near 1."""
    np.random.seed(42)
    n = 100
    symbols = ["000001"] * n
    features = ["momentum_20d"] * n
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    # Construct factor values that strongly predict forward returns
    fv = np.random.randn(n)
    fwd = fv * 0.05 + np.random.randn(n) * 0.001  # correlated
    factor_df = pl.DataFrame({
        "symbol": symbols,
        "feature": features,
        "calc_date": dates,
        "value": fv,
        "factor_version": ["v1.0"] * n,
    })
    fwd_df = pl.DataFrame({
        "symbol": symbols,
        "calc_date": dates,
        "forward_return_20d": fwd,
    })
    ics = compute_factor_ics(factor_df, fwd_df)
    assert ics["momentum_20d"] > 0.5  # strong positive IC


def test_compute_factor_ics_handles_uncorrelated():
    np.random.seed(0)
    n = 50
    factor_df = pl.DataFrame({
        "symbol": ["000001"] * n,
        "feature": ["momentum_20d"] * n,
        "calc_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "value": np.random.randn(n),
        "factor_version": ["v1.0"] * n,
    })
    fwd_df = pl.DataFrame({
        "symbol": ["000001"] * n,
        "calc_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "forward_return_20d": np.random.randn(n) * 100,  # unrelated magnitude
    })
    ics = compute_factor_ics(factor_df, fwd_df)
    # Uncorrelated → IC near 0
    assert abs(ics["momentum_20d"]) < 0.3


# ============================================================
# Composite
# ============================================================

def test_decide_buy():
    assert decide(0.20) == "buy"


def test_decide_avoid():
    assert decide(-0.20) == "avoid"


def test_decide_hold():
    assert decide(0.0) == "hold"
    assert decide(0.05) == "hold"
    assert decide(-0.05) == "hold"


def test_decide_custom_thresholds():
    assert decide(0.5, buy=0.4, avoid=0.0) == "buy"
    assert decide(0.3, buy=0.4, avoid=0.0) == "hold"


def test_composite_score_df_sums_weighted_votes():
    df = pl.DataFrame({
        "symbol": ["000001", "000001"],
        "feature": ["momentum_20d", "rsi_14"],
        "calc_date": [date(2026, 1, 1)] * 2,
        "vote": [1, 1],
        "weight": [1.0, 1.0],  # initial placeholder, replaced by weights arg
    })
    weights = {"momentum_20d": 0.6, "rsi_14": 0.4}
    scores = composite_score_df(df, weights)
    assert scores.row(0)[scores.columns.index("score")] == pytest.approx(1.0)


def test_composite_score_df_zero_when_no_match():
    """Feature not in weights dict → 0 contribution."""
    df = pl.DataFrame({
        "symbol": ["000001"],
        "feature": ["unknown_factor"],
        "calc_date": [date(2026, 1, 1)],
        "vote": [1],
        "weight": [1.0],
    })
    scores = composite_score_df(df, {})
    assert scores.row(0)[scores.columns.index("score")] == 0.0


def test_latest_decisions():
    scores = pl.DataFrame({
        "symbol": ["000001", "000001", "000002", "000002"],
        "calc_date": [date(2026, 1, 1), date(2026, 1, 2)] * 2,
        "score": [0.05, 0.30, -0.20, -0.05],
    })
    # 000001 latest = 0.30 → BUY; 000002 latest = -0.05 → HOLD
    out = latest_decisions(scores)
    rows = out.sort("symbol").to_dicts()
    assert rows[0]["decision"] == "buy"
    assert rows[1]["decision"] == "hold"


def test_latest_decisions_avoid_band():
    scores = pl.DataFrame({
        "symbol": ["000001"],
        "calc_date": [date(2026, 1, 1)],
        "score": [-0.30],  # below -0.15 → AVOID
    })
    out = latest_decisions(scores)
    assert out.row(0)[out.columns.index("decision")] == "avoid"