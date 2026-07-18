"""Signals — rule-based voting, IC weighting, composite scoring.

Public API:
    signals.voter     factor values → ±1/0 votes
    signals.ic_weight rolling Spearman IC → weights
    signals.composite weighted votes → composite score → buy/hold/avoid
"""
from .voter import vote_dataframe_simple, DEFAULT_THRESHOLDS
from .ic_weight import compute_factor_ics, ics_to_weights, equal_weights
from .composite import composite_score_df, decide, latest_decisions, SignalDecision

__all__ = [
    "vote_dataframe_simple",
    "DEFAULT_THRESHOLDS",
    "compute_factor_ics",
    "ics_to_weights",
    "equal_weights",
    "composite_score_df",
    "decide",
    "latest_decisions",
    "SignalDecision",
]