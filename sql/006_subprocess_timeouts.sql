-- 006_subprocess_timeouts.sql — Phase 2.4: per-source request timeouts + baostock demotion.
-- Idempotent.
--
-- Root cause context: baostock's send_msg busy-loops on a CLOSE_WAIT socket
-- when its TCP server (port 10030) gets silently dropped by GFW. Combined with
-- our previous asyncio.to_thread dispatch, this pegged one core indefinitely.
-- Sync fetches now run in a SIGKILLable subprocess (see
-- collector/sources/subprocess_runner.py); request_timeout_seconds bounds it.
--
-- This migration also demotes baostock to priority 99 (last resort) so the
-- chain tries akshare → tushare first. Baostock is kept (not removed) because
-- it's occasionally useful when eastmoney's HTTP API is the one being blocked.

-- ===== akshare: 60s default (HTTP, usually fast or fails quickly) =====
UPDATE data_source
SET config = config || '{"request_timeout_seconds": 60}'::jsonb
WHERE source IN ('akshare_astock', 'akshare_fund');

-- ===== tushare: 60s (token-gated REST API) =====
UPDATE data_source
SET config = config || '{"request_timeout_seconds": 60}'::jsonb
WHERE source = 'tushare_astock';

-- ===== baostock: demote to last-resort + tighter 20s timeout =====
-- priority 99 = tried only after akshare(10) and tushare(15) both fail.
-- The 20s cap reflects baostock's fragile TCP protocol under GFW: if it
-- hasn't returned by then, it's almost certainly stuck in the spin loop
-- and the subprocess will be SIGKILLed.
UPDATE data_source
SET config = config || '{"priority": 99, "request_timeout_seconds": 20}'::jsonb
WHERE source = 'baostock_astock';

-- ===== index for the new config key (cross-source admin queries) =====
CREATE INDEX IF NOT EXISTS idx_data_source_priority
    ON data_source (((config ->> 'priority')::int))
    WHERE (config ->> 'priority') IS NOT NULL;
