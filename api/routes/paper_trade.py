"""Paper-trade endpoints — simulated positions, no real money.

Endpoints:
  GET  /paper_trade/positions         open positions (unsettled)
  GET  /paper_trade/history           closed positions with realized PnL
  GET  /paper_trade/summary           aggregate PnL, win rate, exposure
  POST /paper_trade/buy               open a position {symbol, price, qty, signal_date?}
  POST /paper_trade/sell              close a position {symbol, price, qty, settled_date?}

Schema (see sql/001_init.sql paper_trade):
  id, symbol, side, price, qty, signal_date, settled_date, pnl, extra
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from collector.db import acquire

router = APIRouter(prefix="/paper_trade", tags=["paper_trade"])


# ============================================================
# Request/Response models
# ============================================================

class BuyRequest(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    qty: int = Field(gt=0)
    signal_date: Optional[date] = None  # defaults to today
    strategy: str = "manual"  # future: tie to signal_vote.strategy
    note: str | None = None


class SellRequest(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    qty: int = Field(gt=0)  # partial close OK; ignored if > held qty (then close all)
    settled_date: Optional[date] = None  # defaults to today


# ============================================================
# Internal helpers
# ============================================================

async def _open_position_qty(conn, symbol: str) -> int:
    """Net open quantity for `symbol`: buys minus sells (signed)."""
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(CASE WHEN side='buy'  THEN qty ELSE 0 END), 0)
          - COALESCE(SUM(CASE WHEN side='sell' THEN qty ELSE 0 END), 0) AS net
        FROM paper_trade
        WHERE symbol = $1 AND settled_date IS NULL
        """,
        symbol,
    )
    return int(row["net"] or 0)


async def _avg_open_price(conn, symbol: str) -> float | None:
    """Volume-weighted average buy price across open positions for `symbol`."""
    row = await conn.fetchrow(
        """
        SELECT SUM(price * qty)::float / NULLIF(SUM(qty), 0) AS avg
        FROM paper_trade
        WHERE symbol = $1 AND side = 'buy' AND settled_date IS NULL
        """,
        symbol,
    )
    return float(row["avg"]) if row and row["avg"] is not None else None


# ============================================================
# Endpoints
# ============================================================

@router.get("/positions")
async def positions():
    """Open (unsettled) positions grouped by symbol with avg cost basis."""
    sql = """
    SELECT symbol,
           SUM(CASE WHEN side='buy'  THEN qty ELSE 0 END)
         - SUM(CASE WHEN side='sell' THEN qty ELSE 0 END) AS qty,
           SUM(CASE WHEN side='buy'  THEN price * qty ELSE 0 END)::float
         / NULLIF(SUM(CASE WHEN side='buy' THEN qty ELSE 0 END), 0) AS avg_price,
           MIN(signal_date) AS opened_at
    FROM paper_trade
    WHERE settled_date IS NULL
    GROUP BY symbol
    HAVING SUM(CASE WHEN side='buy'  THEN qty ELSE 0 END)
         - SUM(CASE WHEN side='sell' THEN qty ELSE 0 END) > 0
    ORDER BY symbol
    """
    async with acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


@router.get("/history")
async def history(symbol: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)):
    """Closed positions (settled_date IS NOT NULL) ordered by settled_date DESC."""
    sql = """
    SELECT id, symbol, side, price, qty, signal_date, settled_date, pnl, extra
    FROM paper_trade
    WHERE settled_date IS NOT NULL
      AND ($1::text IS NULL OR symbol = $1)
    ORDER BY settled_date DESC
    LIMIT $2
    """
    async with acquire() as conn:
        rows = await conn.fetch(sql, symbol, limit)
    return [dict(r) for r in rows]


@router.get("/summary")
async def summary():
    """Aggregate PnL, win rate, exposure."""
    sql = """
    WITH closed AS (
        SELECT pnl FROM paper_trade WHERE settled_date IS NOT NULL
    ),
    open_pos AS (
        SELECT symbol,
               SUM(CASE WHEN side='buy'  THEN qty ELSE 0 END)
             - SUM(CASE WHEN side='sell' THEN qty ELSE 0 END) AS qty,
               SUM(CASE WHEN side='buy' THEN price * qty ELSE 0 END)::float AS cost
        FROM paper_trade WHERE settled_date IS NULL
        GROUP BY symbol
    )
    SELECT
        (SELECT COALESCE(SUM(pnl), 0) FROM closed)                              AS realized_pnl,
        (SELECT COUNT(*) FILTER (WHERE pnl > 0) FROM closed)                   AS wins,
        (SELECT COUNT(*) FILTER (WHERE pnl < 0) FROM closed)                   AS losses,
        (SELECT COUNT(*) FROM closed)                                          AS total_trades,
        (SELECT COALESCE(SUM(qty * (cost / NULLIF(qty, 0))), 0) FROM open_pos) AS open_exposure
    """
    async with acquire() as conn:
        row = await conn.fetchrow(sql)
    d = dict(row)
    wins = int(d["wins"] or 0)
    total = int(d["total_trades"] or 0)
    d["win_rate"] = (wins / total) if total > 0 else None
    return d


@router.post("/buy")
async def buy(req: BuyRequest):
    """Open a buy position. Idempotent on (symbol, signal_date): if a buy row
    already exists for that pair, return 409."""
    sig_date = req.signal_date or date.today()
    async with acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM paper_trade WHERE symbol=$1 AND side='buy' AND signal_date=$2",
            req.symbol, sig_date,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"buy already exists for {req.symbol} on {sig_date} (id={existing['id']})",
            )
        row = await conn.fetchrow(
            """
            INSERT INTO paper_trade(symbol, side, price, qty, signal_date, extra)
            VALUES ($1, 'buy', $2, $3, $4, $5::jsonb)
            RETURNING id, symbol, side, price, qty, signal_date
            """,
            req.symbol, req.price, req.qty, sig_date,
            _json({"strategy": req.strategy, "note": req.note}),
        )
    return dict(row)


@router.post("/sell")
async def sell(req: SellRequest):
    """Close (or partially close) a position. Computes realized PnL using the
    volume-weighted avg open price, then writes a sell row with pnl set.
    If qty > held, sells everything (no leverage in paper-trade).
    """
    settled = req.settled_date or date.today()
    async with acquire() as conn:
        held = await _open_position_qty(conn, req.symbol)
        if held <= 0:
            raise HTTPException(status_code=400, detail=f"no open position for {req.symbol}")
        sell_qty = min(req.qty, held)
        avg_cost = await _avg_open_price(conn, req.symbol)
        if avg_cost is None:
            raise HTTPException(status_code=500, detail="avg open price missing")
        pnl = (req.price - avg_cost) * sell_qty

        sell_row = await conn.fetchrow(
            """
            INSERT INTO paper_trade(symbol, side, price, qty, signal_date, settled_date, pnl, extra)
            VALUES ($1, 'sell', $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id, symbol, side, price, qty, settled_date, pnl
            """,
            req.symbol, req.price, sell_qty, settled, settled, pnl,
            _json({"avg_cost": avg_cost, "requested_qty": req.qty, "held_before": held}),
        )
        # If fully closed, mark the buys as settled too (one-shot: settled_date
        # on the buy rows means "this leg is closed"). For partial closes we
        # leave the buy rows open (a future sell can settle them).
        if sell_qty >= held:
            await conn.execute(
                "UPDATE paper_trade SET settled_date = $2 WHERE symbol = $1 AND side = 'buy' AND settled_date IS NULL",
                req.symbol, settled,
            )
    return dict(sell_row)


def _json(d: dict) -> str:
    import json
    return json.dumps({k: v for k, v in d.items() if v is not None}, default=str)