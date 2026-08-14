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
import time
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


def format_ist(iso_str: str | None) -> str:
    """Converts a UTC ISO timestamp from the API/DB to an IST display
    string -- the DB and API always deal in UTC (per the project's own
    spec), but every user-facing display must show IST, never raw UTC."""
    if not iso_str:
        return "—"
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M:%S IST")


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
#
# Cache TTLs are set ABOVE the 30s auto-refresh interval (not below it) --
# ttl=15 against a 30s refresh guarantees a cache miss on literally every
# tick (Streamlit shows a "Running load_x(...)" indicator, and each call
# currently costs ~4-5s against this project's Postgres/NullPool tradeoff),
# so the page was blocking on a fresh, visible network call every single
# refresh instead of actually benefiting from the cache in between.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=45)
def load_algos() -> list[dict]:
    resp = requests.get(f"{API_BASE_URL}/api/algos", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=45)
def load_servers() -> list[dict]:
    resp = requests.get(f"{API_BASE_URL}/api/servers", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=45)
def load_today_pnl_bulk() -> dict[str, float]:
    """One request for every algo's today P&L, instead of the dashboard
    looping over each algo and firing GET /api/pnl individually -- with
    several strategies registered, that meant several sequential ~4-5s
    blocking calls (each showing its own "Running load_today_pnl(...)"
    indicator) before the page could even finish rendering. Keyed by
    "algo_id|server_id" to match GET /api/pnl/today (algo names are only
    unique per server, not globally)."""
    resp = requests.get(
        f"{API_BASE_URL}/api/pnl/today",
        params={"pnl_date": datetime.now(IST).date().isoformat()},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def post_action(action: str, algo_id: str, server_id: str) -> dict:
    resp = requests.post(
        f"{API_BASE_URL}/api/algo/{action}",
        json={"algo_id": algo_id, "server_id": server_id, "requested_by": "dashboard"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()

    # The response above is just "the SSM command was accepted" (status
    # STARTING/STOPPING/etc.), not the real outcome -- and nothing else
    # would ever call GET /api/command/{id} to resolve it, which is also
    # the only place that syncs the verified result back onto algos.status.
    # Without polling here, the Action button would keep showing STOP for
    # an algo that has already actually stopped (algos.status stuck at
    # whatever the last heartbeat said, since a stopped process sends no
    # further heartbeats to ever correct it).
    command_id = result.get("command_id")
    if command_id is not None:
        deadline = time.monotonic() + 20
        with st.spinner(f"Waiting for {algo_id} on {server_id} to finish {action}ing..."):
            while result.get("status") in IN_FLIGHT_STATUSES and time.monotonic() < deadline:
                time.sleep(1.5)
                try:
                    poll_resp = requests.get(f"{API_BASE_URL}/api/command/{command_id}", headers=HEADERS, timeout=15)
                    poll_resp.raise_for_status()
                    result = poll_resp.json()
                except requests.exceptions.RequestException:
                    break  # keep the last known result rather than raising mid-poll

    return result


def create_server(server_id: str, ec2_instance_id: str, region: str, status: str) -> dict:
    resp = requests.post(
        f"{API_BASE_URL}/api/servers",
        json={"server_id": server_id, "ec2_instance_id": ec2_instance_id, "region": region, "status": status},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def update_server(current_server_id: str, **updates: str) -> dict:
    # Only send fields that actually changed -- PATCH semantics, an
    # omitted field must leave that column untouched server-side.
    body = {k: v for k, v in updates.items() if v}
    resp = requests.patch(
        f"{API_BASE_URL}/api/servers/{current_server_id}",
        json=body,
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def delete_server(server_id: str) -> None:
    resp = requests.delete(f"{API_BASE_URL}/api/servers/{server_id}", headers=HEADERS, timeout=10)
    resp.raise_for_status()


def create_algo(algo_id: str, server_id: str, script_path: str | None, status: str, enabled: bool) -> dict:
    body: dict = {"algo_id": algo_id, "server_id": server_id, "status": status, "enabled": enabled}
    if script_path:
        body["script_path"] = script_path
    # Longer timeout than other calls -- registration now also triggers a
    # best-effort git pull on the target instance (Lambda-side bounded to
    # ~8s), not just a DB write.
    resp = requests.post(f"{API_BASE_URL}/api/algos", json=body, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.json()


def edit_algo(algo_id: str, server_id: str, **updates: object) -> dict:
    # Only send fields that were actually provided -- PATCH semantics.
    body = {k: v for k, v in updates.items() if v is not None}
    resp = requests.patch(
        f"{API_BASE_URL}/api/algos/{algo_id}", params={"server_id": server_id},
        json=body, headers=HEADERS, timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def delete_algo(algo_id: str, server_id: str) -> None:
    resp = requests.delete(
        f"{API_BASE_URL}/api/algos/{algo_id}", params={"server_id": server_id}, headers=HEADERS, timeout=10,
    )
    resp.raise_for_status()


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


@st.cache_data(ttl=45)
def load_positions(algo_id: str, server_id: str) -> list[dict]:
    resp = requests.get(
        f"{API_BASE_URL}/api/positions",
        params={"algo_id": algo_id, "server_id": server_id},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=45)
def load_server_health(server_id: str) -> dict:
    """live=true triggers a real check_ec2_health Lambda call (Milestone
    12) -- distinguishes EC2 power state from SSM Agent responsiveness,
    which the cached DB status alone can't. Cached client-side above the
    30s auto-refresh interval (was exactly ttl=30, which could still race
    and miss right on the tick) so this doesn't fire a Lambda invocation
    on every single auto-refresh for every server."""
    resp = requests.get(
        f"{API_BASE_URL}/api/server/status",
        params={"server_id": server_id, "live": "true"},
        headers=HEADERS,
        timeout=20,  # live check chains through Lambda+SSM, slower than a plain DB read
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Header -- market status/P&L stay above the tabs since they're page-level
# summary, not tied to either the Dashboard or Config view specifically.
# ---------------------------------------------------------------------------
st.title(":material/monitoring: Algo Trading Control Center")

market_open = is_market_open()
try:
    algos = load_algos()
except requests.exceptions.RequestException as exc:
    st.error(f":material/wifi_off: Cannot reach the control-center API: {exc}")
    st.stop()

try:
    servers = load_servers()
except requests.exceptions.RequestException as exc:
    st.error(f":material/wifi_off: Could not load registered servers: {exc}")
    servers = []

try:
    pnl_by_algo = load_today_pnl_bulk()
except requests.exceptions.RequestException:
    pnl_by_algo = {}  # a P&L fetch failing shouldn't blank the whole page

today_total_pnl = sum(pnl_by_algo.get(f"{a['algo_id']}|{a['server_id']}", 0.0) for a in algos)

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

tab_dashboard, tab_config = st.tabs([
    ":material/monitoring: Dashboard",
    ":material/settings: Config",
])

# =============================================================================
# Dashboard tab -- read-only status: System Health, strategy control table
# (start/stop actions against already-registered strategies, not CRUD on the
# registration itself), Positions, Trading Logs.
# =============================================================================
with tab_dashboard:
    # -------------------------------------------------------------------
    # System Health -- master prompt's literal Milestone 12 deliverable.
    # Reaching this point at all already proves Vercel + the API + Supabase
    # are up (the page wouldn't have loaded otherwise), so those aren't
    # re-shown as separate rows -- would just be theater. What's NOT
    # implied by the page loading: whether the EC2 instance and its SSM
    # Agent are actually reachable, which is what the live check verifies.
    # -------------------------------------------------------------------
    st.subheader(":material/health_and_safety: System Health")

    if not servers:
        st.info(":material/hourglass_empty: No servers registered yet. Add one in the Config tab.", icon=":material/info:")
    else:
        for srv in sorted(servers, key=lambda s: s["server_id"]):
            sid = srv["server_id"]
            try:
                health = load_server_health(sid)
            except requests.exceptions.RequestException as exc:
                st.write(f":material/error: **{sid}**: could not reach the API to check ({exc})")
                continue

            ec2_ok = health["status"] == "RUNNING"
            ssm_ok = health.get("live_check_healthy") is True
            ec2_icon = "🟢" if ec2_ok else "🔴"
            ssm_label = health.get("ssm_status") or "UNKNOWN"
            ssm_icon = "🟢" if ssm_ok else ("🔴" if health.get("ssm_status") else "❓")

            health_cols = st.columns([2, 2, 2, 2])
            health_cols[0].write(f"**{sid}**")
            health_cols[1].write(f"{ec2_icon} EC2: {health['status']}")
            health_cols[2].write(f"{ssm_icon} SSM Agent: {ssm_label}")
            health_cols[3].write(f":material/dns: `{srv['ec2_instance_id']}` ({srv['region']})")

        st.caption("EC2/SSM health is a live check (Lambda -> AWS), cached 30s -- everything else on this page is DB state.")

    st.divider()

    # -------------------------------------------------------------------
    # Strategy control table, Positions, and Trading Logs -- all gated on
    # `algos` being non-empty. Deliberately an `if` here, not `st.stop()`:
    # stopping would also prevent the Config tab (rendered after this in
    # the script) from ever showing up on a brand-new install with zero
    # strategies -- exactly when Config is needed most, to register one.
    # -------------------------------------------------------------------
    st.subheader(":material/list_alt: Strategies")

    if not algos:
        st.info(
            ":material/hourglass_empty: No strategies registered yet. Add one in the Config tab.",
            icon=":material/info:",
        )
    else:
        algo_names = sorted({a["algo_id"] for a in algos})
        server_names = sorted({a["server_id"] for a in algos})

        header_cols = st.columns([2, 2, 2, 2, 2])
        for col, label in zip(header_cols, ["Strategy", "Server", "Status", "Day P&L (₹)", "Action"]):
            col.markdown(f"**{label}**")

        for algo in algos:
            algo_id = algo["algo_id"]
            server_id = algo["server_id"]
            raw_status = algo["status"]
            display_status = effective_status(raw_status, algo.get("last_heartbeat"))
            # Matches load_today_pnl's old per-algo default exactly -- 0.0
            # for "no P&L row yet today", not None/"--" (that's reserved
            # for the P&L *fetch itself* failing, handled by pnl_by_algo
            # already defaulting to {} above).
            pnl = pnl_by_algo.get(f"{algo_id}|{server_id}", 0.0)

            row_cols = st.columns([2, 2, 2, 2, 2])
            row_cols[0].write(algo_id)
            row_cols[1].write(server_id)
            row_cols[2].write(status_label(display_status))
            row_cols[3].write("—" if pnl is None else f"₹{pnl:,.2f}")

            # Action availability follows the RAW stored status (what the
            # backend actually tracks), not the heartbeat-derived display
            # status -- a STALE/OFFLINE algo still marked RUNNING should
            # still offer a STOP button, since the underlying process may
            # genuinely be running and just not heartbeating.
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

        # -----------------------------------------------------------
        # Positions -- current holdings per algo/server. A closed
        # position (qty 0) has no row at all (see POST /api/positions),
        # not a zero-quantity one, so "no positions" genuinely means
        # flat, not "data hasn't arrived yet" vs. "was open and closed"
        # ambiguity.
        #
        # Note: "Max loss" / "Stop loss" from the master prompt's Risk
        # section aren't shown here -- there's no schema field for a
        # strategy's configured risk limits (a per-strategy config
        # concern, not yet implemented), and fabricating placeholder
        # numbers would be worse than omitting them.
        # -----------------------------------------------------------
        st.divider()
        st.subheader(":material/account_balance_wallet: Positions")

        pos_cols = st.columns(2)
        p_algo = pos_cols[0].selectbox("Strategy", algo_names, key="pos_algo")
        p_server = pos_cols[1].selectbox("Server", server_names, key="pos_server")

        try:
            positions = load_positions(p_algo, p_server)
        except requests.exceptions.RequestException as exc:
            st.error(f":material/wifi_off: Could not load positions: {exc}")
            positions = []

        if not positions:
            st.info(":material/hourglass_empty: No open positions for this strategy/server.", icon=":material/info:")
        else:
            st.dataframe(
                [
                    {
                        "Symbol": p["symbol"],
                        "Quantity": p["quantity"],
                        "Avg Price (₹)": p["average_price"],
                        "Last Price (₹)": p.get("last_price"),
                        "P&L (₹)": p.get("pnl"),
                        "Updated (IST)": format_ist(p["updated_at"]),
                    }
                    for p in positions
                ],
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Avg Price (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                    "Last Price (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                    "P&L (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                },
            )

        # -----------------------------------------------------------
        # Trading logs -- filterable by algo, server, date, level,
        # event type, per the project's own spec. Only shows curated
        # trading-significant events + WARNING/ERROR (see
        # trading/common/log_shipper.py) -- not every line the local
        # structured logger emits.
        # -----------------------------------------------------------
        st.divider()
        st.subheader(":material/receipt_long: Trading Logs")

        filter_cols = st.columns(5)
        f_algo = filter_cols[0].selectbox("Strategy", algo_names, key="log_algo")
        f_server = filter_cols[1].selectbox("Server", server_names, key="log_server")
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
                        "Time (IST)": format_ist(row["timestamp"]),
                        "Level": row["level"],
                        "Event": row["event"],
                        "Details": row.get("details") or {},
                    }
                    for row in log_rows
                ],
                hide_index=True,
                use_container_width=True,
            )

# =============================================================================
# Config tab -- all CRUD: register/edit/delete servers and strategies.
# =============================================================================
with tab_config:
    # -------------------------------------------------------------------
    # Manage servers -- add/edit/delete against POST, PATCH, DELETE
    # /api/servers. Delete is refused server-side while algos are still
    # registered against a server, so no client-side cascade check is
    # needed here -- just surface whatever the API says went wrong.
    # -------------------------------------------------------------------
    st.subheader(":material/dns: Servers")

    st.markdown("**Register a new server**")
    with st.form("add_server_form", clear_on_submit=True):
        new_cols = st.columns(4)
        new_server_id = new_cols[0].text_input("Server ID (name)")
        new_ec2_id = new_cols[1].text_input("EC2 instance ID")
        new_region = new_cols[2].text_input("Region", value="ap-south-1")
        new_status = new_cols[3].selectbox(
            "Status", ["UNKNOWN", "RUNNING", "STOPPED"], index=0
        )
        if st.form_submit_button(":material/add: Add server"):
            if not new_server_id or not new_ec2_id or not new_region:
                st.warning("Server ID, EC2 instance ID, and region are all required.")
            else:
                try:
                    create_server(new_server_id, new_ec2_id, new_region, new_status)
                    st.success(f"Registered server '{new_server_id}'.")
                    load_servers.clear()
                    st.rerun()
                except requests.exceptions.RequestException as exc:
                    st.error(f"Could not register server: {exc}")

    if servers:
        st.markdown("**Existing servers**")
        for srv in sorted(servers, key=lambda s: s["server_id"]):
            sid = srv["server_id"]
            with st.expander(f"{sid}"):
                with st.form(f"edit_server_form_{sid}"):
                    edit_cols = st.columns(4)
                    edit_server_id = edit_cols[0].text_input("Server ID (name)", value=sid)
                    edit_ec2_id = edit_cols[1].text_input("EC2 instance ID", value=srv["ec2_instance_id"])
                    edit_region = edit_cols[2].text_input("Region", value=srv["region"])
                    edit_status = edit_cols[3].selectbox(
                        "Status", ["UNKNOWN", "RUNNING", "STOPPED"],
                        index=["UNKNOWN", "RUNNING", "STOPPED"].index(srv["status"])
                        if srv["status"] in ("UNKNOWN", "RUNNING", "STOPPED") else 0,
                    )
                    if st.form_submit_button(":material/save: Save changes"):
                        try:
                            update_server(
                                sid,
                                server_id=edit_server_id if edit_server_id != sid else "",
                                ec2_instance_id=edit_ec2_id,
                                region=edit_region,
                                status=edit_status,
                            )
                            st.success(f"Updated server '{sid}'.")
                            load_servers.clear()
                            st.rerun()
                        except requests.exceptions.RequestException as exc:
                            st.error(f"Could not update server: {exc}")

                confirm_key = f"confirm_delete_{sid}"
                if not st.session_state.get(confirm_key):
                    if st.button(":material/delete: Delete server", key=f"delete_btn_{sid}"):
                        st.session_state[confirm_key] = True
                        st.rerun()
                else:
                    st.warning(f"Delete '{sid}'? This cannot be undone.")
                    confirm_cols = st.columns(2)
                    if confirm_cols[0].button(":material/delete_forever: Confirm delete", key=f"confirm_delete_btn_{sid}"):
                        try:
                            delete_server(sid)
                            st.success(f"Deleted server '{sid}'.")
                        except requests.exceptions.RequestException as exc:
                            st.error(f"Could not delete server: {exc}")
                        st.session_state.pop(confirm_key, None)
                        load_servers.clear()
                        st.rerun()
                    if confirm_cols[1].button("Cancel", key=f"cancel_delete_btn_{sid}"):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()

    st.divider()

    # -------------------------------------------------------------------
    # Manage strategies -- add/edit/delete against POST, PATCH, DELETE
    # /api/algos.
    # -------------------------------------------------------------------
    st.subheader(":material/precision_manufacturing: Strategies")

    if not servers:
        st.info("Register a server first before adding a strategy.")
    else:
        server_options = sorted(s["server_id"] for s in servers)

        st.markdown("**Register a new strategy**")
        st.caption(
            ":material/info: Registering also triggers a code sync (git pull) -- currently always against "
            "the single configured EC2 instance, regardless of which server you pick below."
        )
        with st.form("add_algo_form", clear_on_submit=True):
            new_algo_cols = st.columns(4)
            new_algo_id = new_algo_cols[0].text_input("Strategy ID (name)")
            new_algo_server = new_algo_cols[1].selectbox("Server", server_options)
            new_algo_script = new_algo_cols[2].text_input("Script path (optional)")
            new_algo_status = new_algo_cols[3].selectbox("Status", ["STOPPED", "RUNNING", "ERROR"], index=0)
            new_algo_enabled = st.checkbox("Enabled", value=True)
            if st.form_submit_button(":material/add: Add strategy"):
                if not new_algo_id:
                    st.warning("Strategy ID is required.")
                else:
                    try:
                        with st.spinner(f"Registering '{new_algo_id}' and syncing code to the server..."):
                            result = create_algo(
                                new_algo_id, new_algo_server, new_algo_script or None,
                                new_algo_status, new_algo_enabled,
                            )
                        st.success(f"Registered strategy '{new_algo_id}' on '{new_algo_server}'.")
                        if result.get("sync_success"):
                            st.success(f":material/cloud_sync: Code synced to the server: {result.get('sync_message') or 'done'}")
                        else:
                            st.warning(
                                f":material/cloud_off: Registered, but code sync failed: "
                                f"{result.get('sync_message') or 'unknown error'}. You may need to sync manually."
                            )
                        load_algos.clear()
                        st.rerun()
                    except requests.exceptions.RequestException as exc:
                        st.error(f"Could not register strategy: {exc}")

        if algos:
            st.markdown("**Existing strategies**")
            for algo in sorted(algos, key=lambda a: (a["server_id"], a["algo_id"])):
                aid = algo["algo_id"]
                asid = algo["server_id"]
                with st.expander(f"{aid} @ {asid}"):
                    with st.form(f"edit_algo_form_{aid}_{asid}"):
                        edit_algo_cols = st.columns(3)
                        edit_algo_script = edit_algo_cols[0].text_input("Script path", value=algo["script_path"])
                        edit_algo_status = edit_algo_cols[1].selectbox(
                            "Status", ["STOPPED", "RUNNING", "ERROR"],
                            index=["STOPPED", "RUNNING", "ERROR"].index(algo["status"])
                            if algo["status"] in ("STOPPED", "RUNNING", "ERROR") else 0,
                        )
                        edit_algo_enabled = edit_algo_cols[2].checkbox("Enabled", value=algo["enabled"])
                        if st.form_submit_button(":material/save: Save changes"):
                            try:
                                edit_algo(
                                    aid, asid, script_path=edit_algo_script,
                                    status=edit_algo_status, enabled=edit_algo_enabled,
                                )
                                st.success(f"Updated strategy '{aid}'.")
                                load_algos.clear()
                                st.rerun()
                            except requests.exceptions.RequestException as exc:
                                st.error(f"Could not update strategy: {exc}")

                    confirm_key = f"confirm_delete_algo_{aid}_{asid}"
                    if not st.session_state.get(confirm_key):
                        if st.button(":material/delete: Delete strategy", key=f"delete_algo_btn_{aid}_{asid}"):
                            st.session_state[confirm_key] = True
                            st.rerun()
                    else:
                        st.warning(f"Delete '{aid}' on '{asid}'? This cannot be undone.")
                        confirm_algo_cols = st.columns(2)
                        if confirm_algo_cols[0].button(
                            ":material/delete_forever: Confirm delete", key=f"confirm_delete_algo_btn_{aid}_{asid}"
                        ):
                            try:
                                delete_algo(aid, asid)
                                st.success(f"Deleted strategy '{aid}'.")
                            except requests.exceptions.RequestException as exc:
                                st.error(f"Could not delete strategy: {exc}")
                            st.session_state.pop(confirm_key, None)
                            load_algos.clear()
                            st.rerun()
                        if confirm_algo_cols[1].button("Cancel", key=f"cancel_delete_algo_btn_{aid}_{asid}"):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
