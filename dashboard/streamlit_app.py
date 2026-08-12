from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Central Strategy Monitor",
    page_icon=":material/monitoring:",
    layout="wide",
)

HEARTBEAT_STALE_AFTER = timedelta(minutes=2)

# ---------------------------------------------------------------------------
# Backend API base URL — the FastAPI service is hosted separately (e.g. on
# Vercel), NOT inside this Streamlit app. Streamlit Cloud's edge gates every
# request behind a browser-only session handshake, so machine clients (EC2
# workers posting heartbeats) can never reach an API mounted in this process.
# Configure via Streamlit secrets (API_BASE_URL) or the API_BASE_URL env var.
# ---------------------------------------------------------------------------
API_BASE_URL = st.secrets.get("API_BASE_URL", os.environ.get("API_BASE_URL", ""))
if not API_BASE_URL:
    st.error(
        ":material/error: `API_BASE_URL` is not configured. Set it in this app's "
        "Streamlit secrets to the deployed backend URL (e.g. `https://your-app.vercel.app`)."
    )
    st.stop()
API_BASE_URL = API_BASE_URL.rstrip("/")

# ---------------------------------------------------------------------------
# Auto-refresh every 30 s
# ---------------------------------------------------------------------------
st_autorefresh(interval=30_000, key="strategy_dashboard_refresh")


# ---------------------------------------------------------------------------
# Data loading — calls the FastAPI backend mounted at /api on the same host
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30)
def load_strategy_data() -> pd.DataFrame:
    """Fetch strategy heartbeats from the FastAPI backend."""
    try:
        resp = requests.get(f"{API_BASE_URL}/strategies", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except requests.exceptions.ConnectionError:
        st.error(":material/wifi_off: Cannot reach the backend API.")
        return pd.DataFrame()
    except requests.exceptions.HTTPError as e:
        st.error(f":material/error: Backend returned an error: {e}")
        return pd.DataFrame()
    except Exception as e:  # noqa: BLE001
        st.error(f":material/bug_report: Unexpected error: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
STATUS_COLOR = {
    "RUNNING": "#16a34a",
    "STOPPED": "#dc2626",
    "ERROR": "#dc2626",
}
STATUS_ICON = {
    "RUNNING": "🟢",
    "STOPPED": "🟡",
    "ERROR": "🔴",
}


def status_badge(status: str, is_stale: bool) -> str:
    icon = "⚠️" if is_stale else STATUS_ICON.get(status, "❓")
    return f"{icon} {status}"


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title(":material/monitoring: Central Strategy Monitor")
st.caption("Live view of all strategy heartbeats · Auto-refreshes every 30 s")

# ---------------------------------------------------------------------------
# Config check
# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
df = load_strategy_data()


if df.empty:
    st.info(
        ":material/hourglass_empty: No strategy heartbeats yet. "
        "Start sending `POST /update_strategy` updates from your EC2 workers.",
        icon=":material/info:",
    )
    st.stop()

# ---------------------------------------------------------------------------
# Data prep
# ---------------------------------------------------------------------------
now_utc = datetime.now(timezone.utc)
df["last_update_time"] = pd.to_datetime(df["last_update_time"], errors="coerce", utc=True)
df["received_at"] = pd.to_datetime(df["received_at"], errors="coerce", utc=True)
df["is_stale"] = (now_utc - df["last_update_time"]) > HEARTBEAT_STALE_AFTER
df["is_stale"] = df["is_stale"].fillna(True)

# ---------------------------------------------------------------------------
# KPI row
# ---------------------------------------------------------------------------
running_count = int((df["status"] == "RUNNING").sum())
stopped_or_error = int(df["status"].isin(["STOPPED", "ERROR"]).sum())
stale_count = int(df["is_stale"].sum())
total_day_pnl = float(df["day_pnl"].sum())
total_mtm = float(df["current_mtm"].sum())
total_trades = int(df["number_of_trades"].sum())

with st.container(horizontal=True):
    st.metric(":material/play_circle: Running", running_count, border=True)
    st.metric(":material/stop_circle: Stopped / Error", stopped_or_error, border=True)
    st.metric(":material/warning: Stale Heartbeats", stale_count, border=True)
    st.metric(":material/trending_up: Total Day P&L", f"₹{total_day_pnl:,.2f}", border=True)
    st.metric(":material/account_balance: Total MTM", f"₹{total_mtm:,.2f}", border=True)
    st.metric(":material/swap_horiz: Total Trades", total_trades, border=True)

st.divider()

# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
col1, col2 = st.columns(2)

PNL_SIGN_COLORS = {"Profit": "#16a34a", "Loss": "#dc2626"}

with col1:
    with st.container(border=True):
        st.subheader("Strategy-wise Day P&L")
        pnl_df = df.copy()
        pnl_df["P&L"] = pnl_df["day_pnl"].apply(lambda v: "Loss" if v < 0 else "Profit")
        pnl_fig = px.bar(
            pnl_df,
            x="strategy_name",
            y="day_pnl",
            color="P&L",
            labels={"strategy_name": "Strategy", "day_pnl": "Day P&L"},
            color_discrete_map=PNL_SIGN_COLORS,
        )
        pnl_fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(pnl_fig)

with col2:
    with st.container(border=True):
        st.subheader("Strategy-wise MTM")
        mtm_df = df.copy()
        mtm_df["P&L"] = mtm_df["current_mtm"].apply(lambda v: "Loss" if v < 0 else "Profit")
        mtm_fig = px.bar(
            mtm_df,
            x="strategy_name",
            y="current_mtm",
            color="P&L",
            labels={"strategy_name": "Strategy", "current_mtm": "MTM"},
            color_discrete_map=PNL_SIGN_COLORS,
        )
        mtm_fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        st.plotly_chart(mtm_fig)

st.divider()

# ---------------------------------------------------------------------------
# Strategy table
# ---------------------------------------------------------------------------
with st.container(border=True):
    st.subheader(":material/table_chart: Strategy status table")

    table_df = df.copy()
    table_df["Status"] = table_df.apply(
        lambda r: status_badge(r["status"], r["is_stale"]), axis=1
    )
    table_df["Last Heartbeat"] = (
        table_df["last_update_time"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d %H:%M:%S IST")
    )
    table_df["Received At"] = (
        table_df["received_at"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d %H:%M:%S IST")
    )

    display_df = table_df.rename(columns={
        "strategy_name": "Strategy",
        "server_name": "Server",
        "day_pnl": "Day P&L (₹)",
        "current_mtm": "MTM (₹)",
        "number_of_trades": "Trades",
    })[["Strategy", "Server", "Status", "Day P&L (₹)", "MTM (₹)", "Trades", "Last Heartbeat", "Received At"]]

    def _negative_red(val: float) -> str:
        return "color: #dc2626" if val < 0 else ""

    styled_df = display_df.style.map(
        _negative_red, subset=["Day P&L (₹)", "MTM (₹)"]
    ).format({"Day P&L (₹)": "₹{:,.2f}", "MTM (₹)": "₹{:,.2f}"})

    st.dataframe(styled_df, hide_index=True)
