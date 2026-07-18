-- 001_init.sql — TimescaleDB schema for quant-lab (Phase 1)
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pg_trgm;        -- Phase 4: text search
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ===== master: symbols =====
CREATE TABLE IF NOT EXISTS symbol (
    symbol        text PRIMARY KEY,           -- e.g. "000001.SZ" or "000001"
    name          text,
    market        text NOT NULL,             -- 'astock' | 'fund' | 'etf'
    exchange      text,                       -- 'SZ' | 'SH' | null
    category      text,                       -- 'stock' | 'index' | 'fund' | 'bond' | null
    list_date     date,
    status        text DEFAULT 'active',     -- 'active' | 'suspended' | 'delisted'
    extra         jsonb DEFAULT '{}'::jsonb,
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_symbol_market ON symbol(market);

-- ===== master: data sources (for watermark scheduler) =====
CREATE TABLE IF NOT EXISTS data_source (
    source        text PRIMARY KEY,
    kind          text NOT NULL,             -- 'akshare' | 'tushare' | 'eastmoney'
    config        jsonb DEFAULT '{}'::jsonb,
    last_run_at   timestamptz,
    last_success_at timestamptz,
    consecutive_errors int DEFAULT 0
);

-- ===== raw_payload: every API response goes here first =====
CREATE TABLE IF NOT EXISTS raw_payload (
    id            bigserial PRIMARY KEY,
    source        text NOT NULL,
    endpoint      text NOT NULL,
    params        jsonb NOT NULL,
    payload       jsonb NOT NULL,            -- raw response, exactly as API returned
    fetched_at    timestamptz DEFAULT now(),
    parsed_at     timestamptz,
    parse_status  text DEFAULT 'pending'    -- 'pending' | 'ok' | 'error'
);
CREATE INDEX IF NOT EXISTS idx_raw_source_endpoint ON raw_payload(source, endpoint, fetched_at DESC);

-- ===== ohlcv_daily: A股 + ETF 日线 (TimescaleDB hypertable) =====
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    symbol        text NOT NULL,
    trade_date    date NOT NULL,
    open          numeric,
    high          numeric,
    low           numeric,
    close         numeric,
    volume        bigint,
    amount        numeric,                 -- 成交额 (元)
    source        text NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('ohlcv_daily', 'trade_date', if_not_exists => TRUE, migrate_data => TRUE);

-- ===== fund_nav: 基金日净值 =====
CREATE TABLE IF NOT EXISTS fund_nav (
    fund_code     text NOT NULL,
    nav_date      date NOT NULL,
    nav           numeric,                  -- 单位净值
    accum_nav     numeric,                  -- 累计净值
    daily_growth  numeric,                  -- 日增长率 (%)
    source        text NOT NULL,
    PRIMARY KEY (fund_code, nav_date)
);
SELECT create_hypertable('fund_nav', 'nav_date', if_not_exists => TRUE, migrate_data => TRUE);

-- ===== job_run: scheduler + collector logs =====
CREATE TABLE IF NOT EXISTS job_run (
    id            bigserial PRIMARY KEY,
    job_name      text NOT NULL,
    started_at    timestamptz DEFAULT now(),
    finished_at   timestamptz,
    status        text DEFAULT 'running',    -- 'running' | 'ok' | 'error'
    rows_in       int,
    rows_upserted int,
    error_msg     text,
    extra         jsonb DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_job_run_name_started ON job_run(job_name, started_at DESC);

-- ===== Phase 2/3 placeholders (defined here so SQLAlchemy doesn't choke) =====
CREATE TABLE IF NOT EXISTS feature_value (
    symbol text NOT NULL,
    feature text NOT NULL,
    calc_date date NOT NULL,
    value double precision,
    factor_version text NOT NULL,
    PRIMARY KEY (symbol, feature, calc_date, factor_version)
);
SELECT create_hypertable('feature_value', 'calc_date', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS signal_vote (
    symbol text NOT NULL,
    strategy text NOT NULL,
    feature text NOT NULL,
    vote_date date NOT NULL,
    vote smallint,                              -- -1 / 0 / +1
    weight real,
    PRIMARY KEY (symbol, strategy, feature, vote_date)
);

CREATE TABLE IF NOT EXISTS paper_trade (
    id bigserial PRIMARY KEY,
    symbol text NOT NULL,
    side text NOT NULL,                          -- 'buy' | 'sell'
    price numeric,
    qty int,
    signal_date date,
    settled_date date,
    pnl numeric,
    extra jsonb DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_paper_trade_symbol ON paper_trade(symbol, signal_date DESC);

-- ===== seed initial data sources =====
INSERT INTO data_source(source, kind) VALUES
    ('akshare_astock', 'akshare'),
    ('akshare_fund',   'akshare'),
    ('tushare_astock', 'tushare')
ON CONFLICT (source) DO NOTHING;
