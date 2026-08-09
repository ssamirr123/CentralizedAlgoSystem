from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "tracker.db"
HEARTBEAT_STALE_AFTER = timedelta(minutes=2)

st.set_page_config(page_title="Central Strategy Monitoring", layout="wide")
st_autorefresh(interval=30_000, key="strategy_dashboard_refresh")

st.markdown(
    """
    <style>
    div[data-testid="stMetricValue"] {
        font-size: 1.8rem;
    }
    .status-badge {
        border-radius: 0.4rem;
        padding: 0.2rem 0.5rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Central Strategy Monitoring System")
st.caption("Auto-refresh every 30 seconds")


def load_strategy_data() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        table_exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name='strategy_heartbeats'
            """
        ).fetchone()

        if not table_exists:
            return pd.DataFrame()

        return pd.read_sql_query(
            """
            SELECT
                strategy_name,
                server_name,
                status,
                day_pnl,
                current_mtm,
                number_of_trades,
                last_update_time,
                received_at
            FROM strategy_heartbeats
            ORDER BY received_at DESC
            """,
            conn,
        )


def status_color(status: str, is_stale: bool) -> str:
    if is_stale:
        return "#f59e0b"  # yellow
    if status == "RUNNING":
        return "#16a34a"  # green
    return "#dc2626"  # red


def style_status_row(row: pd.Series) -> list[str]:
    color = status_color(str(row["Status"]), bool(row["Stale"]))
    status_style = f"background-color: {color}; color: white; font-weight: 600"
    styles = [""] * len(row)
    status_index = row.index.get_loc("Status")
    heartbeat_index = row.index.get_loc("Last Heartbeat")
    styles[status_index] = status_style
    if bool(row["Stale"]):
        styles[heartbeat_index] = "background-color: #fef3c7; color: #92400e; font-weight: 600"
    return styles

if not DB_PATH.exists():
    st.warning("Database not found. Run backend/main.py first.")
else:
    df = load_strategy_data()

    if df.empty:
        st.info("No strategy heartbeats available yet. Start sending POST /update_strategy updates.")
    else:
        now_utc = datetime.now(timezone.utc)
        df["last_update_time"] = pd.to_datetime(df["last_update_time"], errors="coerce", utc=True)
        df["received_at"] = pd.to_datetime(df["received_at"], errors="coerce", utc=True)
        df["is_stale"] = (now_utc - df["last_update_time"]) > HEARTBEAT_STALE_AFTER
        df["is_stale"] = df["is_stale"].fillna(True)

        running_count = int((df["status"] == "RUNNING").sum())
        stopped_or_error_count = int(df["status"].isin(["STOPPED", "ERROR"]).sum())
        total_day_pnl = float(df["day_pnl"].sum())
        total_mtm = float(df["current_mtm"].sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Running Strategies", running_count)
        m2.metric("Total Stopped or Error", stopped_or_error_count)
        m3.metric("Total Day P&L", f"{total_day_pnl:,.2f}")
        m4.metric("Total MTM", f"{total_mtm:,.2f}")

        c1, c2 = st.columns(2)
        with c1:
            pnl_fig = px.bar(
                df,
                x="strategy_name",
                y="day_pnl",
                color="status",
                title="Strategy-wise Day P&L",
                labels={"strategy_name": "Strategy", "day_pnl": "Day P&L", "status": "Status"},
                color_discrete_map={"RUNNING": "#16a34a", "STOPPED": "#dc2626", "ERROR": "#dc2626"},
            )
            pnl_fig.update_layout(height=380, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(pnl_fig, use_container_width=True)

        with c2:
            mtm_fig = px.bar(
                df,
                x="strategy_name",
                y="current_mtm",
                color="status",
                title="Strategy-wise MTM",
                labels={"strategy_name": "Strategy", "current_mtm": "MTM", "status": "Status"},
                color_discrete_map={"RUNNING": "#16a34a", "STOPPED": "#dc2626", "ERROR": "#dc2626"},
            )
            mtm_fig.update_layout(height=380, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(mtm_fig, use_container_width=True)

        table_df = df.rename(
            columns={
                "strategy_name": "Strategy Name",
                "server_name": "Server",
                "status": "Status",
                "day_pnl": "Day P&L",
                "current_mtm": "MTM",
                "number_of_trades": "Trades",
                "last_update_time": "Last Heartbeat",
            }
        )[
            ["Strategy Name", "Server", "Status", "Day P&L", "MTM", "Trades", "Last Heartbeat", "is_stale"]
        ]
        table_df["Stale"] = table_df.pop("is_stale")
        table_df["Last Heartbeat"] = table_df["Last Heartbeat"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        styled_table = table_df.style.apply(style_status_row, axis=1).hide(axis="columns", subset=["Stale"])

        st.subheader("Strategy Status Table")
        st.dataframe(
            styled_table,
            use_container_width=True,
            hide_index=True,
        )

