# Signals & Paper Trading

quant-lab implements Kimi's two-stage signal pipeline:

  factor values → rule votes (±1/0) → IC-weighted composite → buy/hold/avoid → paper trade

## Layer 1 — Voting (signals.voter)

Each feature has a default threshold band defined in `DEFAULT_THRESHOLDS`:

| Factor | Inner (+1) | Outer neutral | -1 outside |
|---|---|---|---|
| `momentum_20d` | [-0.02, 0.02] | (-0.10,-0.02) ∪ (0.02,0.10] | < -0.10 or > 0.10 |
| `momentum_60d` | [-0.05, 0.05] | (-0.20,-0.05) ∪ (0.05,0.20] | outside |
| `volatility_20d` (inverted) | < 0.20 | [0.20, 0.30] | > 0.30 |
| `ma_cross_5_20` | [-0.01, 0.01] | (-0.05,-0.01) ∪ (0.01,0.05] | outside |
| `rsi_14` (inverted) | < 45 | [45, 55] | > 55 |
| `volume_ratio_5_20` | [0.80, 1.30] | (0.50,0.80) ∪ (1.30,2.00] | < 0.50 or > 2.00 |

Tune `signals/voter.py:DEFAULT_THRESHOLDS` to match your risk tolerance.

## Layer 2 — IC weighting (signals.ic_weight)

Spearman rank correlation between factor values and 20-day forward returns,
computed per-feature on a rolling 252-day window. Negative IC factors get
zero weight (or an eps floor if `ics_to_weights(..., eps=0.01)` is set).

Falls back to **equal weighting** when forward returns are unavailable
(insufficient OHLCV history for the lead window).

Spearman is implemented via numpy rank + Pearson on ranks — no scipy
dependency.

## Layer 3 — Composite + decision (signals.composite)

```
S(symbol, date) = Σ_features  weight[feature] · vote[symbol, feature, date]
```

Mapped to a discrete decision by `decide(score, buy=0.15, avoid=-0.15)`:

| Score | Decision |
|---|---|
| ≥ +0.15 | buy |
| -0.15 < S < +0.15 | hold |
| ≤ -0.15 | avoid |

Tune via `decide(score, buy=..., avoid=...)`.

## Cron schedule

| Time | Job |
|---|---|
| 17:00 weekdays | `run_astock_job` — fetch OHLCV via failover |
| 17:30 weekdays | `factor_runner` — compute factors from OHLCV |
| 17:45 weekdays | `signal_runner` — vote + IC + composite → `signal_vote` table |

## Paper trading (api/routes/paper_trade.py)

Manually-driven for now (no auto-trade). The bot side stays simple so you
keep the decision boundary clear:

  POST /paper_trade/buy   {symbol, price, qty}            open
  POST /paper_trade/sell  {symbol, price, qty}            close (partial OK)
  GET  /paper_trade/positions                             open positions + avg cost
  GET  /paper_trade/history                               closed trades + realized pnl
  GET  /paper_trade/summary                               PnL, win rate, exposure

PnL is realized using volume-weighted avg buy cost across open lots. Fully
closing a position marks the buy rows `settled_date` so the position drops
out of `/paper_trade/positions`.

## Future work (Phase 2.3+)

- **Auto-trade on signal**: when `signals/latest` returns "buy", open a
  paper position automatically; "avoid" → close. Needs careful safeguards
  (max position size, sector concentration cap, dry-run flag).
- **Multi-strategy**: today every vote is under strategy `"v1.0"`. Add
  per-strategy factor blends (e.g., momentum-only vs. value-tilt).
- **Calibrate bands from history**: optimize buy/avoid thresholds on
  rolling hit rates once enough closed trades accumulate.
- **Market regime filter**: scale signal strength by index-level trend /
  volatility (Kimi's "regime factor").
- **vectorbt backtest integration** (Phase 3): replay history through the
  same voter + IC logic to measure hit rate before going live (paper or otherwise).