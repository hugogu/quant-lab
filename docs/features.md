# Features — factor library reference

quant-lab ships a small, versioned factor library that runs nightly against
`ohlcv_daily`. Every factor is a pure polars function registered through a
decorator; algorithm changes bump `factors.version.FACTOR_VERSION` so historical
`feature_value` rows remain queryable.

## Built-in factors (v1.0)

| Factor | Category | Formula | Min lookback |
|---|---|---|---|
| `momentum_20d` | momentum | `close[t]/close[t-20] - 1` | 20 |
| `momentum_60d` | momentum | `close[t]/close[t-60] - 1` | 60 |
| `volatility_20d` | volatility | `20d rolling std of log returns, * sqrt(252)` | 20 |
| `ma_cross_5_20` | trend | `(MA5 - MA20) / MA20` | 20 |
| `rsi_14` | oscillator | Wilder's 14-day RSI (approx via simple rolling mean) | 15 |
| `volume_ratio_5_20` | volume | `5d avg volume / 20d avg volume` | 20 |

## How it flows

```
ohlcv_daily  ──┐
               ├──>  factor_runner (worker, daily 17:30 weekdays)
               │
               └──>  feature_value (hypertable by calc_date)
                              │
                              ├──>  GET /features/{symbol}/{feature}      (time series)
                              ├──>  GET /features/{symbol}                 (all features)
                              ├──>  GET /features/latest?date=YYYY-MM-DD   (cross-section)
                              └──>  GET /features/list                     (metadata)
```

## Adding a new factor

```python
# factors/my_new_factor.py
import polars as pl
from factors import register

@register("rsi_21", description="21-day RSI for slower-cycle signals")
def rsi_21(df: pl.DataFrame) -> pl.DataFrame:
    delta = pl.col("close").diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    rs = gain.rolling_mean(window_size=21) / loss.rolling_mean(window_size=21)
    return df.with_columns((100 - 100 / (1 + rs)).alias("value"))
```

Then in `api/main.py` and `collector/scheduler.py` (or `factors/__init__.py`'s
`from . import builtin` line), `import factors.my_new_factor` — registration
happens on import.

## Versioning policy

- **First implementation**: hardcode `"v1.0"` in `factors/version.py`.
- **Algorithm change** (e.g. switch RSI from simple rolling mean to Wilder RMA):
  bump to `"v1.1"`. Old rows under `v1.0` remain queryable via:
  ```sql
  SELECT * FROM feature_value WHERE factor_version = 'v1.0' AND feature = 'rsi_14';
  ```
- **Querying by latest version**: filter on `factor_version = (SELECT value
  FROM version_constants)` or pass the version from your app code.

## Data freshness caveat

Most factors need 14-60 trading days of OHLCV. If `ohlcv_daily` only has a
few days (e.g. fresh backfill, or upstream source is rate-limited), factor
output will be all-NaN and `upsert_feature_value` silently drops those rows.

To diagnose: `SELECT feature, COUNT(*) FROM feature_value GROUP BY feature;`
— empty result means the factors aren't producing values, not a bug.

## Performance

- 10 symbols × 6 factors × 252 days OHLCV: < 1 s on AMD 5825U (polars vectorized).
- DB writes: ~50 rows upserted per run (10 symbols × ~5 dates × 6 factors).

## Future work (Phase 2.2+)

- Add fundamentals-based factors (PE / ROE / 营收增速) once `daily_fundamentals`
  table is built (currently no source wired).
- Replace RSI approximation with true Wilder RMA — needs polars expression for
  recursive smoothing (not yet in polars 1.x core).
- YAML-driven factor definition (Kimi's "定义即代码" ideal — currently Python-only).
- Compute factor IC vs forward returns; use rolling IC as the weight in signal voting.