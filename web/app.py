"""Streamlit entrypoint — K-line chart + symbol browser."""
from __future__ import annotations
import os
import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")

st.set_page_config(page_title="quant-lab", layout="wide")
st.title("quant-lab — 自托管股基分析")

# Sidebar: symbol selector
with st.sidebar:
    st.header("Query")
    try:
        symbols_resp = requests.get(f"{API_BASE}/symbols", timeout=5).json()
        symbols = [s["symbol"] for s in symbols_resp]
    except Exception as e:
        st.error(f"failed to load symbols: {e}")
        symbols = []

    kind = st.radio("type", ["astock", "fund"])
    if kind == "astock":
        target = st.selectbox("symbol", symbols or ["000001"])
    else:
        target = st.text_input("fund code", "000001")
    lookback = st.slider("lookback days", 30, 1000, 250)

# Main: fetch + plot
if kind == "astock":
    st.subheader(f"A股 K线 — {target}")
    try:
        rows = requests.get(f"{API_BASE}/ohlcv/{target}", params={"limit": lookback}, timeout=10).json()
    except Exception as e:
        st.error(f"failed to load OHLCV: {e}")
        rows = []
    if rows:
        df = pd.DataFrame(rows).sort_values("trade_date")
        fig = go.Figure(data=[go.Candlestick(
            x=df["trade_date"],
            open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="red", decreasing_line_color="green",
        )])
        fig.update_layout(xaxis_rangeslider_visible=False, height=500)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(df[["trade_date", "open", "high", "low", "close", "volume"]], use_container_width=True)
    else:
        st.info("no data — run `python -m collector.seed` and let APScheduler run, or hit POST /collect/astock")
else:
    st.subheader(f"基金净值 — {target}")
    try:
        rows = requests.get(f"{API_BASE}/fund/{target}", params={"limit": lookback}, timeout=10).json()
    except Exception as e:
        st.error(f"failed to load NAV: {e}")
        rows = []
    if rows:
        df = pd.DataFrame(rows).sort_values("nav_date")
        fig = go.Figure(data=[go.Scatter(x=df["nav_date"], y=df["nav"], mode="lines+markers")])
        fig.update_layout(height=400, yaxis_title="单位净值")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(df, use_container_width=True)
    else:
        st.info("no data — seed fund codes via SQL then hit POST /collect/fund")
