"""FastAPI entrypoint."""
from __future__ import annotations
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routes import healthz, symbols, ohlcv, collect, features, paper_trade, signals
# Importing factors.builtin registers all factors with the registry at boot.
import factors  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

app = FastAPI(
    title="quant-lab API",
    version="0.2.0",
    description="Self-hosted stock + fund quantitative analysis platform. Phase 2.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # local single-user setup
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(healthz.router)
app.include_router(symbols.router)
app.include_router(ohlcv.router)
app.include_router(collect.router)
app.include_router(features.router)
app.include_router(paper_trade.router)
app.include_router(signals.router)


# ============================================================
# MCP server — expose the same API to agents (Kimi's design).
# Auto-generates MCP tools from every FastAPI route.
# Mounted at /mcp via streamable HTTP transport.
# ============================================================
try:
    from fastapi_mcp import FastApiMCP
    mcp_server = FastApiMCP(
        app,
        name="quant-lab",
        description="Self-hosted quantitative platform: collect, analyze, manage positions.",
    )
    mcp_server.mount_http()  # default path: /mcp
    logging.getLogger(__name__).info("MCP server mounted at /mcp (streamable HTTP)")
except ImportError:
    logging.getLogger(__name__).warning("fastapi-mcp not installed; MCP endpoint unavailable")


@app.get("/")
async def root():
    return {
        "service": "quant-lab",
        "phase": 2,
        "endpoints": [
            "/healthz", "/symbols",
            "/ohlcv/{symbol}", "/fund/{code}",
            "/collect/astock", "/collect/fund",
            "/features/list", "/features/latest", "/features/{symbol}", "/features/{symbol}/{feature_name}",
            "/paper_trade/positions", "/paper_trade/history", "/paper_trade/summary",
            "/paper_trade/buy", "/paper_trade/sell",
            "/signals/latest", "/signals/{symbol}",
            "/mcp",   # MCP endpoint (streamable HTTP)
        ],
        "mcp_transport": "streamable-http at /mcp",
    }
