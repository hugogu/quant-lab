# quant-lab — 自托管股基分析平台

Self-hosted stock + fund quantitative analysis platform.
Designed for **AMD 5825U + 16GB + Ubuntu + Docker** personal setups.
Phased rollout; this repo lands **Phase 1 (data foundation)** fully and
scaffolds Phase 2-4.

## Architecture

```
┌──────────────┐  ┌────────────────┐  ┌──────────────┐
│ akshare/baostock│ │    FastAPI      │  │   Streamlit  │
│  + tushare    │  │   (read API)   │  │  (web UI)    │
└──────┬───────┘  └────────┬───────┘  └──────┬───────┘
       │                   │                 │
       ▼                   ▼                 ▼
┌──────────────────────────────────────────────────────┐
│           PostgreSQL + TimescaleDB                    │
│  raw_payload (JSONB) | ohlcv_daily (hypertable)        │
│  feature_value | signal_vote | paper_trade | job_run  │
└──────────────────────────────────────────────────────┘
       ▲                   ▲
       │                   │
┌──────┴───────┐  ┌────────┴───────┐
│ APScheduler │  │     Redis     │
│  (worker)   │  │   (cache)     │
└─────────────┘  └────────────────┘
```

## Phased roadmap

| Phase | Goal | Status |
|-------|------|--------|
| **1** | TimescaleDB + akshare A股 + 基金净值 + FastAPI 只读 + Streamlit K 线 | ✅ in this repo |
| 2 | Feature 表 + 5-8 基础因子 + 规则投票 + 每日信号推送 | 📝 scaffolded (`features/` empty) |
| 3 | vectorbt 回测闭环 + 滚动 IC 自动调权 + 纸面交易台账 | 📝 scaffolded (`backtest/`, `paper_trade/` empty) |
| 4 | 公告文本检索（pg_trgm + zhparser）+ MCP 接口给 Agent | 📝 scaffolded (`search/`, `mcp/`) |

## Quick start

```bash
git clone https://github.com/hugogu/quant-lab.git
cd quant-lab
cp .env.example .env       # edit DB password if needed
docker compose up -d
# 1) wait ~30s for timescaledb ready
# 2) seed with 10 stocks
docker compose exec api python -m collector.seed --symbols 000001,600519,000858,002594,300750,601318,600036,601398,601288,000333
# 3) open streamlit
open http://localhost:8501
# 4) hit API
curl http://localhost:8000/ohlcv/000001 | head
```

## Repo layout

```
quant-lab/
├── docker-compose.yml        # 5 services: timescaledb, redis, api, web, worker
├── .env.example
├── sql/
│   ├── 001_init.sql          # extensions + tables + hypertables
│   └── 002_seed_symbols.sql  # optional static symbol list
├── collector/                # APScheduler + akshare/tushare
│   ├── db.py
│   ├── astock.py
│   ├── fund.py
│   ├── announcement.py
│   ├── scheduler.py
│   └── seed.py               # CLI: bootstrap initial symbol list
├── api/                      # FastAPI
│   ├── main.py
│   ├── db.py
│   └── routes/{symbols,ohlcv,collect,healthz}.py
├── web/                      # Streamlit
│   └── app.py
├── features/                 # Phase 2 placeholder
├── backtest/                 # Phase 3 placeholder
├── paper_trade/              # Phase 3 placeholder
├── search/                   # Phase 4 placeholder
├── mcp/                      # Phase 4 placeholder
└── docs/
    └── architecture.md
```

## Design choices (rationale)

- **TimescaleDB over ClickHouse**: PG ecosystem is rich, relations + time-series in one engine. ClickHouse is overkill for personal scale.
- **APScheduler over Celery**: <100 jobs/day is well within APScheduler. Saves a broker (Redis here is cache-only).
- **Raw-first**: every API response → `raw_payload` JSONB before parsing. Replay-safe on interface changes.
- **Idempotent writes**: `ON CONFLICT (symbol, date) DO UPDATE`. Re-running a job is safe.
- **Watermark scheduler**: per-source watermark in `data_source` table → incremental fetch only.
- **Factor versioning**: each `feature_value` row tagged with `factor_version` from Git. Reproducible + A/B-able.

## License

MIT.
