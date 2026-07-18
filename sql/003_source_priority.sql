-- 003_source_priority.sql — Phase 2.0: data source priority + failover chain
-- Idempotent. Safe to re-run.

-- ===== priority config: each source row gets a numeric priority in config JSONB =====
-- Lower priority value = tried first. Source with NULL priority is excluded from
-- the failover chain until configured.

-- akshare: primary for astock (fast when not blocked)
UPDATE data_source
SET config = config || '{"priority": 10, "rate_limit_per_sec": 2, "max_retries": 3}'::jsonb
WHERE source = 'akshare_astock' AND (config->>'priority') IS NULL;

UPDATE data_source
SET config = config || '{"priority": 10, "rate_limit_per_sec": 2, "max_retries": 3}'::jsonb
WHERE source = 'akshare_fund' AND (config->>'priority') IS NULL;

-- baostock: stable fallback for astock (slower but reliable)
INSERT INTO data_source(source, kind, config)
VALUES ('baostock_astock', 'baostock', '{"priority": 20, "rate_limit_per_sec": 1, "max_retries": 2}'::jsonb)
ON CONFLICT (source) DO UPDATE
SET config = data_source.config || EXCLUDED.config
WHERE (data_source.config->>'priority') IS NULL;

-- tushare: token-based; will only be used if TUSHARE_TOKEN env is set at runtime
UPDATE data_source
SET config = config || '{"priority": 15, "rate_limit_per_sec": 5, "max_retries": 3, "requires_env": "TUSHARE_TOKEN"}'::jsonb
WHERE source = 'tushare_astock' AND (config->>'priority') IS NULL;

-- ===== raw_payload: add status index for failed-parse replay =====
CREATE INDEX IF NOT EXISTS idx_raw_payload_pending ON raw_payload(parse_status, fetched_at DESC)
WHERE parse_status = 'pending';

-- ===== job_run: add source_used column to track which source served each run =====
ALTER TABLE job_run ADD COLUMN IF NOT EXISTS source_used text;
ALTER TABLE job_run ADD COLUMN IF NOT EXISTS fail_reason text;