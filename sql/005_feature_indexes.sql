-- 005_feature_indexes.sql — Phase 2.1: index feature_value for time-series queries.
-- The hypertable already partitions by calc_date; this adds a (symbol, feature) index
-- for cross-sectional queries ("give me momentum_20d for all symbols as of date X").

CREATE INDEX IF NOT EXISTS idx_feature_value_symbol_feature_date
    ON feature_value (symbol, feature, calc_date DESC);

CREATE INDEX IF NOT EXISTS idx_feature_value_feature_date
    ON feature_value (feature, calc_date DESC);

-- signal_vote: same pattern for cross-sectional signal queries
CREATE INDEX IF NOT EXISTS idx_signal_vote_strategy_date
    ON signal_vote (strategy, vote_date DESC);

CREATE INDEX IF NOT EXISTS idx_signal_vote_symbol_date
    ON signal_vote (symbol, vote_date DESC);