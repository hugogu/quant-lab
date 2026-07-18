"""Debug / ops CLI for the data source layer.

Subcommands:
  list-sources            show all sources with priority + recent status
  test-source <name>      smoke test one source for one symbol
  failover-test <symbol>  walk the priority chain; show which source served
  replay-raw <raw_id>     re-parse a raw_payload row (useful after parse logic changes)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta

from .db import acquire, upsert_ohlcv
from .sources import (
    SourceUnavailable,
    fetch_with_failover,
    list_active_sources,
    mark_raw_parsed,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# ============================================================
# list-sources
# ============================================================

async def cmd_list_sources(args):
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, kind,
                   (config->>'priority')::int AS priority,
                   config->>'rate_limit_per_sec' AS rate_limit,
                   config->>'max_retries' AS max_retries,
                   last_run_at, last_success_at, consecutive_errors
            FROM data_source
            ORDER BY kind, priority NULLS LAST, source
            """
        )
    if not rows:
        print("(no sources configured — run sql/003_source_priority.sql)")
        return
    print(f"{'source':<22} {'kind':<10} {'pri':>4} {'rps':>5} {'retries':>7}  "
          f"{'last_run':<20} {'last_success':<20} {'errs':>4}")
    print("-" * 100)
    for r in rows:
        last_run = r["last_run_at"].isoformat() if r["last_run_at"] else "—"
        last_succ = r["last_success_at"].isoformat() if r["last_success_at"] else "—"
        pri = r["priority"] if r["priority"] is not None else "—"
        print(f"{r['source']:<22} {r['kind']:<10} {str(pri):>4} "
              f"{r['rate_limit'] or '-':>5} {r['max_retries'] or '-':>7}  "
              f"{last_run:<20} {last_succ:<20} {r['consecutive_errors']:>4}")


# ============================================================
# test-source
# ============================================================

async def cmd_test_source(args):
    """Smoke test a single source for one symbol. Bypasses registry; instantiates the fetcher directly."""
    from .sources.registry import _get_fetcher, list_active_sources
    from .sources.base import SourceMisconfigured

    async with acquire() as conn:
        sources = await list_active_sources(conn, args.domain)
        row = next((s for s in sources if s["source"] == args.source), None)
        if row is None:
            print(f"ERROR: source {args.source!r} not configured for domain={args.domain!r}", file=sys.stderr)
            print("Active sources:", [s["source"] for s in sources], file=sys.stderr)
            sys.exit(1)
        try:
            fetcher = _get_fetcher(args.source, row)
        except SourceMisconfigured as e:
            print(f"SKIP: {e}", file=sys.stderr)
            sys.exit(2)

    end = date.today()
    start = end - timedelta(days=args.lookback)
    print(f"Testing {args.source} for {args.symbol} ({start} → {end})…")
    try:
        raw = await fetcher.fetch_raw(args.symbol, start, end)
        rows = fetcher.parse(raw, args.symbol)
        print(f"✅ {len(rows)} rows")
        if rows:
            print("First row:", json.dumps(rows[0], default=str, ensure_ascii=False))
            print("Last row:", json.dumps(rows[-1], default=str, ensure_ascii=False))
    except SourceUnavailable as e:
        print(f"❌ SourceUnavailable: {e}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(4)


# ============================================================
# failover-test
# ============================================================

async def cmd_failover_test(args):
    """Walk the priority chain explicitly. Logs which sources were attempted and which succeeded."""
    end = date.today()
    start = end - timedelta(days=args.lookback)
    print(f"Failover test for {args.symbol} ({start} → {end})…")
    try:
        result = await fetch_with_failover(
            domain=args.domain, symbol=args.symbol, start=start, end=end,
            persist_raw=args.persist_raw,
        )
        print(f"✅ served by {result.source}: {len(result.rows)} rows (raw_id={result.raw_id})")
    except SourceUnavailable as e:
        print(f"❌ all sources failed: {e}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# replay-raw
# ============================================================

async def cmd_replay_raw(args):
    """Re-parse a raw_payload row. Useful after parser logic changes."""
    from .sources.registry import _get_fetcher, list_active_sources
    from .sources.base import SourceMisconfigured

    async with acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM raw_payload WHERE id = $1", args.raw_id)
        if row is None:
            print(f"ERROR: raw_id {args.raw_id} not found", file=sys.stderr)
            sys.exit(1)
        # Derive domain from source name suffix if not provided
        domain = args.domain or row["source"].rsplit("_", 1)[-1]
        sources = await list_active_sources(conn, domain)
        src_row = next((s for s in sources if s["source"] == row["source"]), None)
        if src_row is None:
            print(f"ERROR: source {row['source']} no longer configured for domain={domain!r}", file=sys.stderr)
            sys.exit(2)
        try:
            fetcher = _get_fetcher(row["source"], src_row)
        except SourceMisconfigured as e:
            print(f"SKIP: {e}", file=sys.stderr)
            sys.exit(3)

        # Parse and upsert
        symbol = row["params"].get("symbol") if isinstance(row["params"], dict) else None
        if not symbol:
            print("ERROR: params.symbol missing from raw_payload row", file=sys.stderr)
            sys.exit(4)
        try:
            rows = fetcher.parse(row["payload"], symbol)
        except Exception as e:
            await mark_raw_parsed(conn, row["id"], ok=False, error=str(e))
            print(f"❌ parse failed: {e}", file=sys.stderr)
            sys.exit(5)
        await mark_raw_parsed(conn, row["id"], ok=True)
        if rows:
            await upsert_ohlcv(rows)
        print(f"✅ replayed {args.raw_id}: {len(rows)} rows upserted")


# ============================================================
# entrypoint
# ============================================================

def main():
    p = argparse.ArgumentParser(description="quant-lab data source CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("list-sources", help="show all data_source rows")
    s1.set_defaults(func=cmd_list_sources)

    s2 = sub.add_parser("test-source", help="smoke test one source/symbol")
    s2.add_argument("source", help="source name, e.g. akshare_astock")
    s2.add_argument("symbol", help="symbol code, e.g. 000001")
    s2.add_argument("--domain", default="astock", help="data domain (astock/fund)")
    s2.add_argument("--lookback", type=int, default=7)
    s2.set_defaults(func=cmd_test_source)

    s3 = sub.add_parser("failover-test", help="walk the failover chain explicitly")
    s3.add_argument("symbol", help="symbol code")
    s3.add_argument("--domain", default="astock")
    s3.add_argument("--lookback", type=int, default=7)
    s3.add_argument("--no-persist-raw", dest="persist_raw", action="store_false")
    s3.set_defaults(func=cmd_failover_test, persist_raw=True)

    s4 = sub.add_parser("replay-raw", help="re-parse a raw_payload row")
    s4.add_argument("raw_id", type=int)
    s4.add_argument("--domain", default=None)
    s4.set_defaults(func=cmd_replay_raw)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()