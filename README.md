# quant-lab — 自托管股基分析平台

Self-hosted stock + fund quantitative analysis platform. Ingests A-share +
fund data via multi-source failover (akshare / baostock / tushare),
stores in TimescaleDB, computes versioned factors, votes them with IC-weighted
composite scoring, and exposes everything through a FastAPI + Streamlit + MCP
surface. Designed for **AMD 5825U + 16GB + Ubuntu + Docker** personal setups.

> Architecture discussion that drove this design:
> [Kimi conversation](https://www.kimi.com/share/19f73b7e-68a2-82cc-8000-000030e6dd29).

---

## Status

| Phase | Scope | State |
|-------|-------|-------|
| **1** | TimescaleDB schema + akshare A股 + FastAPI + Streamlit | ✅ shipped |
| **2.0** | Source abstraction (`Fetcher` ABC) + multi-source failover + circuit breaker + watermark + raw-first persistence | ✅ shipped |
| **2.1** | Factor registry + 6 builtin factors (polars) + nightly `factor_runner` cron | ✅ shipped |
| **2.2** | Rule voting + rolling Spearman IC weighting + composite score + paper-trade API | ✅ shipped |
| **2.3** | MCP server (fastapi-mcp) exposing every route to agents at `/mcp` | ✅ shipped |
| **3** | vectorbt backtest closed loop + auto-trade wiring | 📝 placeholder (`backtest/`) |
| **4** | Announcement/news full-text search (pg_trgm + zhparser) | 📝 placeholder (`search/`); `pg_trgm` ext already installed |

---

## Architecture

```
        akshare / baostock / tushare / eastmoney
                           │
                           ▼
            ┌──────────────────────────────┐
            │  collector.sources.registry  │  failover chain + circuit breaker
            │  fetch_with_failover()       │  + watermark + rate limit
            └──────────────┬───────────────┘
                           │  raw 先行
                           ▼
                raw_payload (JSONB, replay-safe)
                           │  parse + normalize
                           ▼
            ohlcv_daily (hypertable)  |  fund_nav (hypertable)
                           │
                           ▼  factor_runner (cron 17:30 weekdays, polars)
                feature_value (hypertable, factor_version tagged)
                           │
                           ▼  signal_runner (cron 17:45 weekdays)
            signal_vote (±1/0) → IC weights → composite score → buy/hold/avoid
                           │
                           ▼
                     paper_trade (decision ledger)
                           │
        ┌──────────────────┼───────────────────┐
        ▼                  ▼                   ▼
   FastAPI (8000)    Streamlit (8501)     MCP /mcp (streamable-http)
```

Five Docker services: `timescaledb`, `redis`, `api`, `web`, `worker`.

---

## Quick start (Docker, recommended)

### 1. Configure environment

```bash
git clone https://github.com/hugogu/quant-lab.git
cd quant-lab
cp .env.example .env
# review .env: DB password, API_TOKEN, optional TUSHARE_TOKEN
```

### 2. Bring up the stack

```bash
docker compose up -d
docker compose ps                # wait until timescaledb shows (healthy)
```

On first boot the TimescaleDB container runs `sql/*.sql` in order, which:
- creates extensions (`timescaledb`, `pg_trgm`, `uuid-ossp`),
- creates all 9 tables + hypertables,
- seeds 3 data-source rows + **10 starter A-share symbols** (平安银行, 贵州茅台, …).

So a fresh stack already has symbols to collect — no manual seed required.

### 3. Backfill history (required before factors produce values)

The worker fetches only the last ~7 days on startup. **Most factors need
≥20–60 days of OHLCV**, so run a one-shot backfill:

```bash
# backfill 252 trading days for all seeded symbols via the failover chain
docker compose exec api python -m bin.backfill_ohlcv --days 252
```

Alternatively via the API (same effect, JSON body):

```bash
curl -X POST http://localhost:8000/collect/astock \
     -H 'Content-Type: application/json' \
     -d '{"lookback_days": 252}'
```

### 4. Compute factors + signals

```bash
docker compose exec api python -m collector.factor_runner --all --days 252
docker compose exec api python -m collector.signal_runner  --days 252
```

After this the Streamlit panels (Factors / Signals / Paper Trades) have data.
The scheduler will keep them fresh automatically (cron on weekdays after
market close — see `docs/signals.md`).

### 5. Open the UI / hit the API

| URL | What |
|-----|------|
| http://localhost:8501 | Streamlit web UI (K-line / factors / signals / paper trades) |
| http://localhost:8000 | FastAPI root — lists all routes |
| http://localhost:8000/docs | Swagger UI (interactive) |
| http://localhost:8000/redoc | ReDoc (read-friendly) |
| http://localhost:8000/mcp | MCP endpoint (streamable HTTP, for agents) |

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/symbols?limit=5
curl http://localhost:8000/features/list
curl "http://localhost:8000/ohlcv/000001?limit=5"
```

---

## Host ports (intentionally offset to avoid local conflicts)

| Service | Host port | Container port |
|---------|-----------|----------------|
| TimescaleDB | **5433** | 5432 |
| Redis | **6380** | 6379 |
| FastAPI | 8000 | 8000 |
| Streamlit | 8501 | 8501 |

Connect to the DB from the host (e.g. with `psql` / DBeaver):

```bash
psql "postgresql://quant:change_me_in_prod@localhost:5433/quantlab"
```

---

## REST API reference

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | liveness + DB ping |
| GET | `/symbols?market=&limit=` | list tracked symbols |
| GET | `/ohlcv/{symbol}?start=&end=&limit=` | daily OHLCV |
| GET | `/fund/{code}?limit=` | fund NAV series |
| POST | `/collect/astock` | trigger A-share collection (body: `{"symbols":[…], "lookback_days":N}` or omit for all) |
| POST | `/collect/fund` | trigger fund NAV collection |
| GET | `/features/list` | registered factor metadata (name, version, description) |
| GET | `/features/latest?date=YYYY-MM-DD&limit=` | cross-section of latest factor values |
| GET | `/features/{symbol}?days=` | all factors for one symbol |
| GET | `/features/{symbol}/{feature_name}?days=` | single factor time series |
| GET | `/signals/latest?limit=` | latest composite snapshot |
| GET | `/signals/{symbol}?days=` | per-symbol signal history |
| GET | `/paper_trade/positions` | open positions + avg cost |
| GET | `/paper_trade/history?symbol=&limit=` | closed trades with realized PnL |
| GET | `/paper_trade/summary` | aggregate PnL / win rate / exposure |
| POST | `/paper_trade/buy` | `{symbol, price, qty, signal_date?, strategy?, note?}` |
| POST | `/paper_trade/sell` | `{symbol, price, qty, settled_date?}` (partial close OK) |

All routes are auto-exposed as MCP tools at `/mcp` — agents can call them
without a separate integration layer.

---

## CLI tools (run inside the `api` or `worker` container)

```bash
# data source ops
docker compose exec api python -m collector.cli list-sources          # show priority + status
docker compose exec api python -m collector.cli test-source akshare_astock 000001
docker compose exec api python -m collector.cli failover-test 000001  # walk the priority chain
docker compose exec api python -m collector.cli replay-raw <raw_id>   # re-parse a raw_payload row

# symbol management
docker compose exec api python -m collector.seed --symbols 600519,000858
docker compose exec api python -m collector.seed --from-sql /app/sql/002_seed_symbols.sql

# history + compute
docker compose exec api python -m bin.backfill_ohlcv --days 252            # all active symbols
docker compose exec api python -m bin.backfill_ohlcv --days 252 --symbols 000001,600519
docker compose exec api python -m collector.factor_runner --all --days 252
docker compose exec api python -m collector.factor_runner --symbols 000001 --days 60
docker compose exec api python -m collector.signal_runner --days 252
```

---

## Running the tests

```bash
docker compose exec api python -m pytest tests/ -v
```

Current suite: ~64 unit tests across `factors`, `signals`, `collector.sources`
(circuit breaker, rate limiter, failover, symbol-code converters, akshare/
baostock parse schemas), plus two MCP smoke tests that skip gracefully if the
`mcp` client package or the API container is unreachable.

CI (`.github/workflows/ci.yml`) runs `ruff check` (advisory) and a dependency
import smoke test on Python 3.12.

---

## Configuration (`.env`)

| Var | Default | Notes |
|-----|---------|-------|
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `quantlab` / `quant` / `change_me_in_prod` | change password in any non-toy setup |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `timescaledb` / `5432` | container-internal; use `localhost:5433` from host |
| `REDIS_URL` | `redis://redis:6379/0` | cache only |
| `API_TOKEN` | `local-dev-token` | reserved for future auth (Caddy front-end recommended) |
| `API_BASE_URL` | `http://api:8000` | used by Streamlit container |
| `TUSHARE_TOKEN` | _(empty)_ | set to enable `tushare_astock` in the failover chain |
| `SOURCE_ASTOCK_CHAIN` / `SOURCE_FUND_CHAIN` | akshare → baostock → tushare | comma-separated priority override |
| `SOURCE_FAILOVER_ENABLED` | `true` | |
| `SOURCE_LOOKBACK_BUFFER_DAYS` | `7` | re-fetch overlap to catch restated values |
| `COLLECT_CRON_ASTOCK` | `0 17 * * 1-5` | weekdays after A-share close |
| `COLLECT_CRON_FUND` | `0 22 * * *` | daily |
| `FACTOR_CRON` | `30 17 * * 1-5` | after OHLCV lands |
| `SIGNAL_CRON` | `45 17 * * 1-5` | after factors land |
| `FACTOR_LOOKBACK_DAYS` | `252` | window passed to `factor_runner` / `signal_runner` |

---

## Repo layout

```
quant-lab/
├── docker-compose.yml             5 services (host ports 5433/6380/8000/8501)
├── docker-compose.yml.build-bak   alternate compose that builds local images
├── .env.example
├── requirements.txt
├── sql/                           idempotent migrations, run on first boot
│   ├── 001_init.sql               extensions + 9 tables + hypertables + seed sources
│   ├── 002_seed_symbols.sql       10 starter A-share symbols
│   ├── 003_source_priority.sql    failover priorities + job_run extras
│   ├── 004_data_source_domain.sql kind vs domain split
│   └── 005_feature_indexes.sql    cross-sectional indexes on feature/signal
├── collector/
│   ├── db.py                      asyncpg pool + upsert_ohlcv + date coercion
│   ├── scheduler.py               APScheduler entrypoint (cron + startup jobs)
│   ├── seed.py                    CLI: seed symbols
│   ├── cli.py                     CLI: list-sources / test-source / failover-test / replay-raw
│   ├── factor_runner.py           nightly factor compute → feature_value
│   ├── signal_runner.py           nightly voting + IC + composite → signal_vote
│   └── sources/
│       ├── base.py                Fetcher ABC, RateLimiter, CircuitBreaker, with_retry
│       ├── akshare.py             AKShareAStockFetcher + AKShareFundFetcher
│       ├── baostock.py            BaoStockAStockFetcher (fallback)
│       ├── tushare.py             TushareAStockFetcher (token-gated)
│       └── registry.py            fetch_with_failover + raw_payload + watermark
├── factors/
│   ├── registry.py                @register decorator + Factor dataclass
│   ├── version.py                 FACTOR_VERSION = "v1.0" (bump on math change)
│   └── builtin.py                 momentum_20d/60d, volatility_20d, ma_cross_5_20, rsi_14, volume_ratio_5_20
├── signals/
│   ├── voter.py                   threshold-band voting (vectorized polars)
│   ├── ic_weight.py               rolling Spearman IC → weights (no scipy)
│   └── composite.py               weighted sum → buy/hold/avoid
├── api/
│   ├── main.py                    FastAPI app + MCP mount
│   ├── db.py                      query helpers
│   ├── Dockerfile
│   └── routes/{healthz,symbols,ohlcv,collect,features,signals,paper_trade}.py
├── web/
│   ├── app.py                     Streamlit UI (K-line / Factors / Signals / Paper Trades)
│   └── Dockerfile
├── bin/backfill_ohlcv.py          one-shot history backfill
├── tests/                         pytest; 64 pass + 2 MCP smoke (skip if offline)
├── docs/{architecture,features,signals}.md
└── {backtest,search,features,paper_trade,mcp}/  Phase 3/4 placeholders (README only)
```

---

## Design choices (rationale)

- **TimescaleDB over ClickHouse / InfluxDB** — PG ecosystem is rich, relations
  + time-series in one engine, `pg_trgm`/`zhparser` ready for Phase 4 text
  search. Fits 16GB; ClickHouse would need ≥4GB just for the JVM.
- **APScheduler over Celery** — <100 jobs/day is well within APScheduler.
  Saves a broker; Redis here is cache-only.
- **Raw-first** — every API response lands in `raw_payload` JSONB *before*
  parsing. Parser bug or upstream schema change? Replay from raw without
  re-hitting the source.
- **Idempotent writes** — `ON CONFLICT (symbol, date) DO UPDATE`. Any job is
  safe to re-run.
- **Multi-source failover** — akshare → baostock → tushare, with per-source
  rate limiter (token bucket), circuit breaker (opens after N failures,
  half-open after cooldown), and watermark-based incremental fetch.
- **Factor versioning** — `feature_value.factor_version` is reproducibility
  contract. Change the math → bump `factors/version.py` → old rows stay
  queryable for A/B.
- **IC weighting, not vote-counting** — composite score uses rolling Spearman
  IC against 20-day forward returns; factors that lose predictive power get
  zero weight. Falls back to equal weights when forward returns are
  unavailable (insufficient history).
- **MCP by default** — every FastAPI route is auto-exposed as an MCP tool at
  `/mcp`, so agents (Claude Code, OpenClaw, …) can query the platform with
  zero glue code.

---

## Troubleshooting (local testing)

- **`curl /ohlcv/000001` returns `[]`** — OHLCV not collected yet. Run the
  backfill step from Quick start §3.
- **Factors view empty after `factor_runner`** — most factors need 20–60
  days of OHLCV. Check: `SELECT feature, COUNT(*) FROM feature_value GROUP BY feature;`.
  Empty = insufficient history, not a bug. Use `--days 252`.
- **Signals show equal weights instead of IC weights** — IC path needs ≥20
  days of forward returns, i.e. ≥20 days of history beyond today. On a fresh
  DB it correctly falls back to equal weights; log line says
  `IC computation failed (...); using equal weights`.
- **`akshare_astock` showing `errs=1`** — akshare scrapes eastmoney which is
  frequently rate-limited / blocked. The failover chain will try baostock
  next; check with `python -m collector.cli failover-test 000001`.
- **`docker compose up` reuses an old volume** — to start completely fresh:
  `docker compose down -v && docker compose up -d`. This wipes all data.
- **Port 5432/6379 already in use on host** — that's why compose maps to
  5433/6380. Connect via those.
- **Adding tushare** — set `TUSHARE_TOKEN` in `.env`, then
  `docker compose restart api worker`. The source is already in the chain at
  priority 15 (between akshare=10 and baostock=20).

---

## Local dev (without Docker)

Requires Python 3.12 + a reachable Postgres with TimescaleDB extension.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export POSTGRES_HOST=localhost POSTGRES_PORT=5433   # point at compose's DB
psql "$POSTGRES_DSN" -f sql/001_init.sql            # one-time schema
psql "$POSTGRES_DSN" -f sql/002_seed_symbols.sql
# run any service in-process:
uvicorn api.main:app --reload --port 8000
streamlit run web/app.py
python -m collector.scheduler
python -m pytest tests/ -v
```

---

## License

MIT.
