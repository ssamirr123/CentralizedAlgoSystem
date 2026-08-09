# CentralizedAlgoSystem - Detailed System Guide

This document explains what the system does, how each module works, and how to run everything locally.

## 1) What This System Is

`CentralizedAlgoSystem` is a central monitoring stack for algorithmic trading strategies running on one or more servers.

Each strategy process sends heartbeat updates to a central FastAPI backend. The backend stores the latest status for each `(strategy_name, server_name)` pair, triggers alert events, and exposes endpoints. A Streamlit dashboard reads the same database and visualizes operational health and P&L metrics.

---

## 2) High-Level Architecture

### Components

- `backend/` - FastAPI API + background watcher + DB update logic
- `strategy_agent/` - non-blocking heartbeat sender (threaded client)
- `dashboard/` - Streamlit operations dashboard (auto-refresh)
- `alerts/` - Telegram alert service with dedup + retries
- `analytics/` - report module (currently not wired into live flow)
- `data/tracker.db` - SQLite database file

### Data flow

1. Strategy process updates internal metrics.
2. `strategy_agent` thread posts heartbeat to backend every ~30s.
3. Backend upserts row in `strategy_heartbeats` table.
4. Backend triggers alert conditions (started/stopped/error/recovered/loss/stale).
5. Dashboard reads DB and renders KPIs + status table + charts.

---

## 3) Backend API (`backend/main.py`)

### Startup behavior

- Initializes DB tables via `init_db()`.
- Starts async stale-heartbeat watcher.
- Reads environment configs:
  - `DAY_LOSS_LIMIT` (default: `10000.0`)
  - `STALE_THRESHOLD_MINUTES` (default: `2`)
  - `STALE_CHECK_INTERVAL_SECONDS` (default: `60`)

### Endpoints

#### `POST /update_strategy`

Accepts heartbeat payload and creates/updates one record per strategy/server pair.

Request fields:

- `strategy_name`
- `server_name`
- `status` (`RUNNING`, `STOPPED`, `ERROR`)
- `current_mtm`
- `day_pnl`
- `number_of_trades`
- `last_update_time` (ISO datetime)

Server behavior:

- Finds existing row by `(strategy_name, server_name)`.
- If missing: insert new row.
- If present: update status/metrics/timestamps.
- Always updates server-side `received_at` to current UTC.
- Triggers status transition alerts.
- Triggers day-loss alert when `day_pnl < 0` and `abs(day_pnl) > DAY_LOSS_LIMIT`.

#### `GET /strategies`

Returns all current strategy rows ordered by latest `received_at` first.

#### `GET /health`

Returns service health payload with UTC timestamp.

---

## 4) Database Layer (`backend/database.py`, `backend/models.py`)

### Engine/session

- Uses SQLite at `data/tracker.db`.
- SQLAlchemy session lifecycle managed per request.

### Table: `strategy_heartbeats`

Main fields:

- `id` (PK)
- `strategy_name`
- `server_name`
- `status`
- `current_mtm`
- `day_pnl`
- `number_of_trades`
- `last_update_time` (client timestamp)
- `received_at` (backend timestamp)

Important constraint:

- Unique key on `(strategy_name, server_name)` ensures one latest row per pair.

---

## 5) Heartbeat Agent (`strategy_agent/agent.py`)

`StrategyHeartbeatAgent` is designed to be embedded inside each strategy process.

### Behavior

- Runs a daemon thread (`start()`) so heartbeat sending does not block strategy logic.
- Sends payload at configured interval (default 30s).
- Uses retry with exponential backoff on network/server failures.
- Avoids retrying 4xx errors (treated as client/config issue).
- Supports clean shutdown using `stop()`.
- Uses thread lock to safely read/write metrics from strategy loop.

### Strategy integration example

- `strategy_agent/sample_trading_strategy.py` simulates a strategy loop.
- Updates metrics every ~2s.
- Heartbeat thread sends snapshots independently.

---

## 6) Dashboard (`dashboard/streamlit_app.py`)

The dashboard reads SQLite directly and refreshes periodically.

### UI features

- KPI cards:
  - Running strategies
  - Stopped/Error strategies
  - Total Day P&L
  - Total MTM
- Status table with color coding:
  - Green: healthy `RUNNING`
  - Red: `STOPPED`/`ERROR`
  - Yellow: stale heartbeat
- Plotly bar charts:
  - Strategy-wise Day P&L
  - Strategy-wise MTM

### Stale logic

- Marks stale when heartbeat age exceeds ~2 minutes (dashboard-side check).

---

## 7) Alerts (`alerts/telegram.py`)

Provides a singleton `alert_service` with named alert methods:

- `strategy_started`
- `strategy_stopped`
- `strategy_crashed`
- `strategy_recovered`
- `day_loss_exceeded`
- `heartbeat_missing`

### Alert reliability features

- Dedup window (`ALERT_DEDUP_SECONDS`, default `120s`)
- Retry + exponential backoff (`ALERT_MAX_RETRIES`, `ALERT_RETRY_DELAY`)
- Thread-safe internals
- Graceful behavior when credentials are missing (logs warning, does not crash app)

### Required env vars for real Telegram delivery

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

---

## 8) Analytics Module (`analytics/reports.py`)

`build_price_report()` exists, but it queries `market_ticks`, which is not part of the current DB schema in this project. So this module is currently not part of the main runtime flow.

---

## 9) How To Run Everything (Local)

Use separate terminals for backend, dashboard, and sample strategy.

### Step 1: Go to project root

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
```

### Step 2: (Recommended) Create and activate virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
```

### Step 3: Install dependencies

Option A (single install from `pyproject.toml`):

```bash
python3 -m pip install .
```

Option B (module-wise requirements):

```bash
python3 -m pip install -r backend/requirements.txt
python3 -m pip install -r dashboard/requirements.txt
python3 -m pip install -r strategy_agent/requirements.txt
```

### Step 4: (Optional) Configure alert-related env vars

```bash
export TELEGRAM_BOT_TOKEN="<your_bot_token>"
export TELEGRAM_CHAT_ID="<your_chat_id>"
export DAY_LOSS_LIMIT="10000"
export STALE_THRESHOLD_MINUTES="2"
export STALE_CHECK_INTERVAL_SECONDS="60"
```

If Telegram vars are not set, backend still runs; alerts are only logged.

### Step 5: Run backend API (Terminal 1)

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

### Step 6: Verify backend quickly

```bash
curl -s http://127.0.0.1:8000/health
```

### Step 7: Run dashboard (Terminal 2)

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
streamlit run dashboard/streamlit_app.py
```

### Step 8: Run sample strategy sender (Terminal 3)

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
python3 strategy_agent/sample_trading_strategy.py
```

This starts simulated strategy metrics and periodic heartbeats to backend.

### Step 9: Check strategies via API

```bash
curl -s http://127.0.0.1:8000/strategies
```

---

## 10) Quick Manual POST Test (Without Agent)

```bash
curl -s -X POST http://127.0.0.1:8000/update_strategy \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_name": "mean_reversion_v1",
    "server_name": "ec2-ap-south-1a-i-01",
    "status": "RUNNING",
    "current_mtm": 1540.25,
    "day_pnl": 420.75,
    "number_of_trades": 18,
    "last_update_time": "2026-07-24T10:00:00Z"
  }'
```

---

## 11) Troubleshooting

- `ModuleNotFoundError`: ensure virtualenv is active and dependencies are installed.
- `Address already in use` on `:8000`: stop previous server or change port in uvicorn command.
- No dashboard data: confirm backend is running and heartbeats are arriving.
- No Telegram alerts: check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars.
- Stale rows appear unexpectedly: verify strategy host clock and heartbeat frequency.

---

## 12) File References

- `README.md`
- `backend/main.py`
- `backend/database.py`
- `backend/models.py`
- `backend/schemas.py`
- `dashboard/streamlit_app.py`
- `strategy_agent/agent.py`
- `strategy_agent/sample_trading_strategy.py`
- `alerts/telegram.py`
- `analytics/reports.py`
- `pyproject.toml`

