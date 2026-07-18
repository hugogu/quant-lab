# Architecture

## Data flow

```
akshare / tushare / baostock / eastmoney
        │
        ▼
   raw_payload (JSONB, all responses, replay-safe)
        │
        ▼  parse + transform
   ohlcv_daily (hypertable) | fund_nav (hypertable)
        │
        ▼  feature engineering (Phase 2)
   feature_value (factor_version tagged)
        │
        ▼  rule voting + IC-weighted score (Phase 2)
   signal_vote (+1 / 0 / -1 per (symbol, strategy, feature, date))
        │
        ▼  meta-model stacking OR direct signal (Phase 3)
   paper_trade (decision ledger)
        │
        ▼  announcement/news text + zhparser (Phase 4)
   search index
        │
        ▼  MCP server exposes (Phase 4)
   Agent queries features / signals / decisions
```

## Service layout

- **timescaledb** — single Postgres + TimescaleDB extension. Hypertable for
  time-series, regular tables for relations (symbol, data_source, job_run).
- **redis** — cache layer (recent OHLCV lookups, hot symbols).
- **api** — FastAPI read API + manual collect trigger.
- **web** — Streamlit K-line / NAV browser.
- **worker** — APScheduler runs collectors on cron; logs to `job_run`.

## Watermark + idempotency

Each `data_source` row tracks `last_success_at`. Collectors query
`symbol` table → fetch since `last_success_at - 1day` (small overlap) →
upsert via `ON CONFLICT DO UPDATE`. Re-running a job is always safe.

## Why TimescaleDB (not ClickHouse / InfluxDB)

- PG ecosystem: pg_trgm + zhparser for Phase 4 text search
- Relations + time-series in one engine (no ETL)
- Continuous aggregates for weekly/monthly bars (Phase 2)
- Compression policy for cold data (>30 days)
- Fits 16GB box — ClickHouse would need 4GB just for JVM

## Capacity (rough, for 5825U + 14GB RAM)

- A股日线: 5000 stocks × 20 years × 250 days = 25M rows ≈ 2 GB raw + 0.5 GB compressed
- 基金净值: 10000 funds × 10 years × 365 days = 36M rows ≈ 1 GB
- 全文检索 (Phase 4): depends on announcement corpus size, probably < 5 GB
- All comfortably fits 14GB with margin
