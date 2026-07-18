"""Streamlit entrypoint — K-line chart + symbol browser + features panel."""
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
    view = st.radio("view", ["K-line / 净值", "Factors", "Signals", "Paper Trades"])

# ===================== Main: K-line / NAV =====================
if view == "K-line / 净值":
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
            st.info("no data — run `python -m bin.backfill_ohlcv` or hit POST /collect/astock")
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

# ===================== Main: Features =====================
elif view == "Factors":
    st.subheader(f"Factors — {target}")
    try:
        meta = requests.get(f"{API_BASE}/features/list", timeout=5).json()
    except Exception as e:
        st.error(f"failed to load feature list: {e}")
        meta = []
    if not meta:
        st.info("no factors registered")
    else:
        feature_names = [m["name"] for m in meta]
        feature = st.selectbox("feature", feature_names)
        try:
            rows = requests.get(
                f"{API_BASE}/features/{target}/{feature}",
                params={"days": lookback},
                timeout=10,
            ).json()
        except Exception as e:
            st.error(f"failed to load feature: {e}")
            rows = []
        if rows:
            df = pd.DataFrame(rows).sort_values("calc_date")
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            fig = go.Figure(data=[go.Scatter(
                x=df["calc_date"], y=df["value"], mode="lines+markers",
                name=feature,
            )])
            fig.update_layout(height=400, yaxis_title=feature, xaxis_title="calc_date")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(df, use_container_width=True)
        else:
            st.info(
                f"no feature rows for {target}/{feature} — "
                f"run factor_runner (need ≥14-20 days of OHLCV for most factors)"
            )

        # Cross-section table (latest values for all symbols, this feature)
        with st.expander(f"Cross-section: latest {feature} across all symbols"):
            try:
                latest = requests.get(f"{API_BASE}/features/latest", timeout=10).json()
            except Exception as e:
                st.error(f"failed: {e}")
                latest = []
            if latest:
                df = pd.DataFrame(latest)
                df_wide = df.pivot_table(
                    index="symbol", columns="feature", values="value", aggfunc="first"
                ).reset_index()
                st.dataframe(df_wide, use_container_width=True)
            else:
                st.info("no cross-section data yet")

# ===================== Main: Signals =====================
elif view == "Signals":
    st.subheader("Signals — latest composite scores")
    st.caption("Source: signal_runner cron (17:45 weekdays). Until features land, this view shows the raw feature snapshot.")
    try:
        latest = requests.get(f"{API_BASE}/signals/latest", timeout=10).json()
    except Exception as e:
        st.error(f"failed: {e}")
        latest = []
    if latest:
        df = pd.DataFrame(latest)
        st.dataframe(df, use_container_width=True)
    else:
        st.info("no signal rows yet — need feature_value data (currently 0 rows due to baostock's 5-day history limit)")

    st.divider()
    st.subheader(f"Signal history — {target}")
    try:
        rows = requests.get(f"{API_BASE}/signals/{target}", params={"days": lookback}, timeout=10).json()
    except Exception as e:
        st.error(f"failed: {e}")
        rows = []
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)
    else:
        st.info(f"no signal rows for {target}")

# ===================== Main: Paper Trades =====================
elif view == "Paper Trades":
    st.subheader("Paper Trades — simulated positions")
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Open positions**")
        try:
            positions = requests.get(f"{API_BASE}/paper_trade/positions", timeout=10).json()
        except Exception as e:
            st.error(f"failed: {e}")
            positions = []
        if positions:
            st.dataframe(pd.DataFrame(positions), use_container_width=True)
        else:
            st.info("no open positions")

    with col2:
        st.markdown("**Summary**")
        try:
            summary = requests.get(f"{API_BASE}/paper_trade/summary", timeout=10).json()
        except Exception as e:
            st.error(f"failed: {e}")
            summary = {}
        if summary:
            st.metric("Realized PnL", f"{summary.get('realized_pnl', 0):.2f}")
            wr = summary.get("win_rate")
            st.metric("Win rate", f"{wr:.1%}" if wr is not None else "—")
            st.metric("Total trades", summary.get("total_trades", 0))
            st.metric("Open exposure", f"{summary.get('open_exposure', 0):.2f}")

    st.divider()
    st.markdown("**Buy (open a position)**")
    with st.form("buy_form"):
        b_col1, b_col2, b_col3 = st.columns(3)
        b_sym = b_col1.text_input("symbol", value=target)
        b_price = b_col2.number_input("price", min_value=0.0, value=10.0, step=0.1)
        b_qty = b_col3.number_input("qty", min_value=1, value=100, step=100)
        submitted = st.form_submit_button("Buy")
        if submitted:
            try:
                r = requests.post(
                    f"{API_BASE}/paper_trade/buy",
                    json={"symbol": b_sym, "price": b_price, "qty": int(b_qty)},
                    timeout=10,
                )
                if r.ok:
                    st.success(f"bought {b_qty} {b_sym} @ {b_price}")
                    st.rerun()
                else:
                    st.error(f"failed: {r.json().get('detail', r.text)}")
            except Exception as e:
                st.error(f"failed: {e}")

    st.markdown("**Sell (close a position)**")
    with st.form("sell_form"):
        s_col1, s_col2, s_col3 = st.columns(3)
        s_sym = s_col1.text_input("symbol ", value=target)
        s_price = s_col2.number_input("sell price", min_value=0.0, value=10.0, step=0.1)
        s_qty = s_col3.number_input("sell qty", min_value=1, value=100, step=100)
        submitted = st.form_submit_button("Sell")
        if submitted:
            try:
                r = requests.post(
                    f"{API_BASE}/paper_trade/sell",
                    json={"symbol": s_sym, "price": s_price, "qty": int(s_qty)},
                    timeout=10,
                )
                if r.ok:
                    body = r.json()
                    st.success(f"sold {body['qty']} {s_sym} @ {s_price} — pnl {body['pnl']:.2f}")
                    st.rerun()
                else:
                    st.error(f"failed: {r.json().get('detail', r.text)}")
            except Exception as e:
                st.error(f"failed: {e}")

    st.divider()
    st.markdown("**History (closed)**")
    try:
        hist = requests.get(f"{API_BASE}/paper_trade/history", params={"limit": 50}, timeout=10).json()
    except Exception as e:
        st.error(f"failed: {e}")
        hist = []
    if hist:
        st.dataframe(pd.DataFrame(hist), use_container_width=True)
    else:
        st.info("no closed trades yet")