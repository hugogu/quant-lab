#!/usr/bin/env python3
"""One-off backfill script — fetches 252 days of OHLCV for all active astock
symbols via the failover registry and upserts to ohlcv_daily.

Run inside the worker container:
    python -m bin.backfill_ohlcv [--days 252] [--symbols 000001,600519]

Used during Phase 2.1 to populate enough history for factor_runner to produce
non-null values (most factors need >=14-20 days of OHLCV).
"""
import argparse
import asyncio
import sys
from datetime import date, timedelta

from collector.db import acquire, upsert_ohlcv
from collector.sources import fetch_with_failover, SourceUnavailable


async def main(days: int, symbols: list[str] | None) -> int:
    end = date.today()
    start = end - timedelta(days=days)
    async with acquire() as conn:
        if symbols:
            syms = symbols
        else:
            rows = await conn.fetch(
                "SELECT symbol FROM symbol WHERE market='astock' AND status='active' ORDER BY symbol"
            )
            syms = [r["symbol"] for r in rows]

    print(f"Backfilling {len(syms)} symbols × {days} days ({start} → {end})…")
    total = 0
    by_source: dict[str, int] = {}
    errors: list[str] = []
    for sym in syms:
        try:
            r = await fetch_with_failover(
                domain="astock", symbol=sym, start=start, end=end,
                persist_raw=False,
            )
            if r.rows:
                n = await upsert_ohlcv(r.rows)
                total += n
                by_source[r.source] = by_source.get(r.source, 0) + n
                print(f"  {sym}: {n} rows via {r.source}")
            else:
                print(f"  {sym}: 0 rows (no data)")
        except SourceUnavailable as e:
            errors.append(f"{sym}: {e}")
            print(f"  {sym}: FAIL {e}")
        except Exception as e:
            errors.append(f"{sym}: {e}")
            print(f"  {sym}: ERROR {type(e).__name__}: {e}")

    print(f"\nTotal: {total} rows upserted")
    print(f"Sources: {by_source}")
    if errors:
        print(f"\n{len(errors)} errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=252)
    p.add_argument("--symbols", default=None, help="comma-separated; default = all active astock")
    args = p.parse_args()
    syms = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    sys.exit(asyncio.run(main(args.days, syms)))