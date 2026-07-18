"""CLI: seed initial symbol list. Usage:
    docker compose exec api python -m collector.seed --symbols 000001,600519
    docker compose exec api python -m collector.seed --from-sql /path/to/symbols.sql
"""
from __future__ import annotations
import argparse
import asyncio
import logging
from .db import acquire, db_dsn

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


async def seed_symbols(symbols: list[str]):
    rows = [(s, s, "astock", "SZ" if s.startswith(("0", "3")) else "SH", None) for s in symbols]
    async with acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """INSERT INTO symbol(symbol, name, market, exchange, list_date)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (symbol) DO NOTHING""",
                rows,
            )
    log.info("seeded %d symbols: %s", len(rows), symbols)


async def seed_from_sql(path: str):
    with open(path) as f:
        sql = f.read()
    async with acquire() as conn:
        await conn.execute(sql)
    log.info("executed %s", path)


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbols", help="comma-separated list of A-share codes, e.g. 000001,600519")
    g.add_argument("--from-sql", help="path to a .sql file with INSERT statements")
    args = p.parse_args()
    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        asyncio.run(seed_symbols(syms))
    else:
        asyncio.run(seed_from_sql(args.from_sql))


if __name__ == "__main__":
    main()
