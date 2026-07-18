"""FastAPI entrypoint."""
from __future__ import annotations
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routes import healthz, symbols, ohlcv, collect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

app = FastAPI(
    title="quant-lab API",
    version="0.1.0",
    description="Self-hosted stock + fund quantitative analysis platform. Phase 1.",
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


@app.get("/")
async def root():
    return {"service": "quant-lab", "phase": 1, "endpoints": ["/healthz", "/symbols", "/ohlcv/{symbol}", "/fund/{code}", "/collect/astock", "/collect/fund"]}
