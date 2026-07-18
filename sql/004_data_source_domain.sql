-- 004_data_source_domain.sql — Phase 2.0: separate domain (what data) from kind (what library).
-- The original `kind` column stores the upstream library name (akshare/baostock/tushare).
-- A new `domain` column captures what kind of data the source serves (astock/fund/etf/etc).
-- Idempotent.

ALTER TABLE data_source ADD COLUMN IF NOT EXISTS domain text;

-- Derive domain from source name suffix (default convention: <library>_<domain>)
UPDATE data_source SET domain = split_part(source, '_', 2) WHERE domain IS NULL;

-- Set explicit values for the seeded sources (in case naming convention diverges)
UPDATE data_source SET domain = 'astock' WHERE source IN ('akshare_astock', 'baostock_astock', 'tushare_astock');
UPDATE data_source SET domain = 'fund'   WHERE source = 'akshare_fund';

ALTER TABLE data_source ALTER COLUMN domain SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_data_source_domain ON data_source(domain);