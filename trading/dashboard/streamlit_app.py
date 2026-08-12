"""
Algo Trading Control Center dashboard.

Separate from dashboard/streamlit_app.py (the old simple heartbeat
monitor) -- this is the real control surface: start/stop/restart actions
against the Milestone 6 API, backed by Milestone 5's richer schema.

Reads the bulk list views (GET /api/algos, /api/servers) for the table,
per-algo P&L for today's total, and calls the async control endpoints
(POST /api/algo/start etc.) for actions -- never assumes an action
succeeded just because the API call returned; the table only reflects
what GET /api/algos reports on the next refresh (DB-backed, itself only
updated once a command resolves via Milestone 6's polling).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="Algo Trading Control Center",
    page_icon=":material/monitoring:",
    layout="wide",
)

API_BASE_URL = st.secrets.get("API_BASE_URL", os.environ.get("API_BASE_URL", ""))
CONTROL_API_KEY = st.secrets.get("CONTROL_API_KEY", os.environ.get("CONTROL_API_KEY", ""))

if not API_BASE_URL or not CONTROL_API_KEY:
    st.error(
        ":material/error: `API_BASE_URL` and `CONTROL_API_KEY` must both be configured in "
        "this app's Streamlit secrets."
    )
    st.stop()

API_BASE_URL = API_BASE_URL.rstrip("/")
HEADERS = {"X-API-Key": CONTROL_API_KEY}

st_autorefresh(interval=30_000, key="control_center_refresh")

# ---------------------------------------------------------------------------
# Status display -- icon + text always together, never color-only (per the
# project's own explicit requirement).
# ---------------------------------------------------------------------------
STATUS_DISPLAY = {
    "RUNNING": ("🟢", "RUNNING"),
    "STARTING": ("🟡", "STARTING"),
    "STOPPING": ("🟡", "STOPPING"),
    "RESTARTING": ("🟡", "RESTARTING"),
    "UPDATING": ("🟡", "UPDATING"),
    "PENDING": ("🟡", "PENDING"),
    "STOPPED": ("⚪", "STOPPED"),
    "ERROR": ("🔴", "ERROR"),
    "STALE": ("⚠️", "STALE"),
    "OFFLINE": ("🔴", "OFFLINE"),
}

IN_FLIGHT_STATUSES = {"STARTING", "STOPPING", "RESTARTING", "UPDATING", "PENDING"}

# Heartbeat staleness thresholds, per the project's own spec: <20s RUNNING,
# 20-60s STALE, >60s OFFLINE. Configurable via secrets/env, not hard-coded.
HEARTBEAT_STALE_AFTER_SECONDS = int(
    st.secrets.get("HEARTBEAT_STALE_AFTER_SECONDS", os.environ.get("HEARTBEAT_STALE_AFTER_SECONDS", 20))
)
HEARTBEAT_OFFLINE_AFTER_SECONDS = int(
    st.secrets.get("HEARTBEAT_OFFLINE_AFTER_SECONDS", os.environ.get("HEARTBEAT_OFFLINE_AFTER_SECONDS", 60))
)


def status_label(s: str) -> str:
    icon, text = STATUS_DISPLAY.get(s, ("❓", "UNKNOWN"))
    return f"{icon} {text}"


def effective_status(stored_status: str, last_heartbeat_iso: str | None) -> str:
    """Overrides a stored RUNNING status with STALE/OFFLINE based on
    heartbeat recency -- other statuses (STOPPED, ERROR, in-flight ones)
    pass through unchanged, since heartbeat age is meaningless for an
    algo that isn't supposed to be sending them right now."""
    if stored_status != "RUNNING":
        return stored_status
    if not last_heartbeat_iso:
        return "OFFLINE"  # claims RUNNING but has never sent a heartbeat

    last_hb = datetime.fromisoformat(last_heartbeat_iso.replace("Z", "+00:00"))
    if last_hb.tzinfo is None:
        last_hb = last_hb.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - last_hb).total_seconds()

    if age_seconds < HEARTBEAT_STALE_AFTER_SECONDS:
        return "RUNNING"
    if age_seconds < HEARTBEAT_OFFLINE_AFTER_SECONDS:
        return "STALE"
    return "OFFLINE"


def is_market_open(now: datetime | None = None) -> bool:
    """NSE regular session: 09:15-15:30 IST, Mon-Fri. Does not account for
    market holidays -- a documented simplification, not a real calendar."""
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_time <= now <= close_time


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=15)
def load_algos() -> list[dict]:
    resp = requests.get(f"{API_BASE_URL}/api/algos", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=15)
def load_today_pnl(algo_id: str, server_id: str) -> float:
    resp = requests.get(
        f"{API_BASE_URL}/api/pnl",
        params={"algo_id": algo_id, "server_id": server_id},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    today = datetime.now(IST).date().isoformat()
    for row in rows:
        if row["date"] == today:
            return float(row["pnl"])
    return 0.0


def post_action(action: str, algo_id: str, server_id: str) -> dict:
    resp = requests.post(
        f"{API_BASE_URL}/api/algo/{action}",
        json={"algo_id": algo_id, "server_id": server_id, "requested_by": "dashboard"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def load_logs(algo_id: str, server_id: str, level: str, event: str, log_date, limit: int) -> list[dict]:
    params: dict[str, str | int] = {"algo_id": algo_id, "server_id": server_id, "limit": limit}
    if level and level != "All":
        params["level"] = level
    if event:
        params["event"] = event
    if log_date:
        params["log_date"] = log_date.isoformat()
    resp = requests.get(f"{API_BASE_URL}/api/logs", params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title(":material/monitoring: Algo Trading Control Center")

market_open = is_market_open()
try:
    algos = load_algos()
except requests.exceptions.RequestException as exc:
    st.error(f":material/wifi_off: Cannot reach the control-center API: {exc}")
    st.stop()

today_total_pnl = 0.0
for a in algos:
    try:
        today_total_pnl += load_today_pnl(a["algo_id"], a["server_id"])
    except requests.exceptions.RequestException:
        pass  # a single algo's P&L fetch failing shouldn't blank the whole page

with st.container(horizontal=True):
    st.metric(
        ":material/schedule: Market Status",
        "OPEN" if market_open else "CLOSED",
        border=True,
    )
    st.metric(
        ":material/trending_up: Today's P&L",
        f"₹{today_total_pnl:,.2f}",
        border=True,
    )

st.divider()

# ---------------------------------------------------------------------------
# Strategy control table
# ---------------------------------------------------------------------------
if not algos:
    st.info(
        ":material/hourglass_empty: No algos registered yet. They appear here once a "
        "start/stop/restart/update action has been issued for them via the API.",
        icon=":material/info:",
    )
    st.stop()

header_cols = st.columns([2, 2, 2, 2, 2])
for col, label in zip(header_cols, ["Strategy", "Server", "Status", "Day P&L (₹)", "Action"]):
    col.markdown(f"**{label}**")

for algo in algos:
    algo_id = algo["algo_id"]
    server_id = algo["server_id"]
    raw_status = algo["status"]
    display_status = effective_status(raw_status, algo.get("last_heartbeat"))

    try:
        pnl = load_today_pnl(algo_id, server_id)
    except requests.exceptions.RequestException:
        pnl = None

    row_cols = st.columns([2, 2, 2, 2, 2])
    row_cols[0].write(algo_id)
    row_cols[1].write(server_id)
    row_cols[2].write(status_label(display_status))
    row_cols[3].write("—" if pnl is None else f"₹{pnl:,.2f}")

    # Action availability follows the RAW stored status (what the backend
    # actually tracks), not the heartbeat-derived display status -- a
    # STALE/OFFLINE algo that's still marked RUNNING should still offer a
    # STOP button, since the underlying process may genuinely be running
    # and just not heartbeating.
    action_col = row_cols[4]
    if raw_status in IN_FLIGHT_STATUSES:
        action_col.button(status_label(raw_status), key=f"noop-{algo_id}-{server_id}", disabled=True)
    elif raw_status == "RUNNING":
        if action_col.button("STOP", key=f"stop-{algo_id}-{server_id}"):
            try:
                result = post_action("stop", algo_id, server_id)
                if not result.get("success"):
                    st.error(f"Stop request for {algo_id} failed: {result.get('message') or 'unknown error'}")
                else:
                    st.cache_data.clear()
                    st.rerun()
            except requests.exceptions.RequestException as exc:
                st.error(f"Failed to reach the API to stop {algo_id}: {exc}")
    else:
        if action_col.button("START", key=f"start-{algo_id}-{server_id}"):
            try:
                result = post_action("start", algo_id, server_id)
                if not result.get("success"):
                    st.error(f"Start request for {algo_id} failed: {result.get('message') or 'unknown error'}")
                else:
                    st.cache_data.clear()
                    st.rerun()
            except requests.exceptions.RequestException as exc:
                st.error(f"Failed to reach the API to start {algo_id}: {exc}")

st.caption("Auto-refreshes every 30s. Status/P&L come from the last known DB state, not a live check on every load.")

# ---------------------------------------------------------------------------
# Trading logs -- filterable by algo, server, date, level, event type, per
# the project's own spec. Only shows curated trading-significant events +
# WARNING/ERROR (see trading/common/log_shipper.py) -- not every line the
# local structured logger emits.
# ---------------------------------------------------------------------------
st.divider()
st.subheader(":material/receipt_long: Trading Logs")

algo_names = sorted({a["algo_id"] for a in algos})
server_names = sorted({a["server_id"] for a in algos})

filter_cols = st.columns(5)
f_algo = filter_cols[0].selectbox("Strategy", algo_names)
f_server = filter_cols[1].selectbox("Server", server_names)
f_level = filter_cols[2].selectbox("Level", ["All", "INFO", "WARNING", "ERROR", "CRITICAL"])
f_event = filter_cols[3].text_input("Event (exact match)", placeholder="e.g. ENTRY")
f_date = filter_cols[4].date_input("Date", value=None)

try:
    log_rows = load_logs(f_algo, f_server, f_level, f_event.strip(), f_date, limit=200)
except requests.exceptions.RequestException as exc:
    st.error(f":material/wifi_off: Could not load logs: {exc}")
    log_rows = []

if not log_rows:
    st.info(
        ":material/hourglass_empty: No matching log events. Only curated trading events "
        "(START/STOP/ENTRY/EXIT/SL/RE-ENTRY/SQUARE_OFF) and WARNING+ get shipped here -- "
        "everything else stays in the instance's local log files.",
        icon=":material/info:",
    )
else:
    st.dataframe(
        [
            {
                "Time (UTC)": row["timestamp"],
                "Level": row["level"],
                "Event": row["event"],
                "Details": row.get("details") or {},
            }
            for row in log_rows
        ],
        hide_index=True,
        use_container_width=True,
    )
