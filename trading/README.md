# trading/ — Milestone 1: EC2 Trading Runtime

Deploy target: this whole folder maps 1:1 onto `/home/trading/` on the
EC2 instance (matches the master prompt's target layout).

## What this is

A broker-agnostic runtime scaffold for running one algo reliably:
structured logging, config via env vars, graceful shutdown, broker
reconnect-with-backoff, PID file, and a heartbeat sent to the existing
`backend/` monitoring API (reuses `strategy_agent/agent.py` unchanged).

This is **not** yet a process manager — Milestone 2 (`trading-agent`) will
add remote start/stop/restart. Milestone 1 is just: one algo process, run
manually, reliably.

## What's NOT real yet

`common/brokers/zerodha_kite.py`, `angelone.py`, and `icici_breeze.py` are
stubs. Their `connect()` / `get_quote()` / `get_positions()` raise
`NotImplementedError`, and `place_order()` / `cancel_order()` additionally
refuse to run unless `TRADING_MODE=live` — checked before any SDK call, so
a half-finished adapter still cannot fire a real order. Fill in the
`# TODO` blocks with real SDK calls when you're ready for a given broker.

Until then, run everything with `BROKER=paper` (the default) — a fully
working simulated broker with instant fills and zero live-order risk.

## Run it

```bash
cd CentralizedAlgoSystem
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r trading/algos/example_strategy/requirements.txt
pip install -r strategy_agent/requirements.txt

cp trading/.env.example trading/.env
# edit trading/.env if needed — defaults (BROKER=paper) work with no changes

# load the env file (bash/zsh):
set -a; source trading/.env; set +a
```

Start the existing monitoring backend in another terminal so heartbeats
have somewhere to go (optional — the algo keeps running even if this is
down, heartbeat send just logs a warning and retries):

```bash
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Then run the algo:

```bash
python trading/algos/example_strategy/main.py
```

## Expected output

Structured JSON log lines (stdout + `trading/logs/example_strategy.log`),
in this order:

```json
{"event": "ALGO_STARTED", ...}
{"event": "BROKER_CONNECTED", "broker": "paper", ...}
{"event": "MARKET_DATA_CONNECTED", "symbol": "NIFTY", ...}
{"event": "STRATEGY_INITIALIZED", ...}
{"event": "HEARTBEAT_RUNNING", "interval_seconds": 30, ...}
{"event": "ALGO_RUNNING", ...}
```

Then it loops silently (ticking every `STRATEGY_LOOP_INTERVAL_SECONDS`)
until you stop it.

Stop with Ctrl+C — expect a clean shutdown sequence:

```json
{"event": "SHUTDOWN_INITIATED", ...}
{"event": "STRATEGY_STOP", ...}
{"event": "ALGO_STOPPED_SAFELY", ...}
```

Exit code `0` on clean shutdown.

## Test procedure

1. Run with defaults (`BROKER=paper`) — confirm the 6 startup events above
   appear in order, then Ctrl+C and confirm the 3 shutdown events appear.
2. Check `trading/logs/example_strategy.log` exists and contains the same
   JSON lines.
3. Check `trading/data/example_strategy.pid` exists while running and is
   removed after shutdown.
4. With the backend running, `curl http://127.0.0.1:8000/strategies` and
   confirm `example_strategy` / `local-dev` shows up with `status: RUNNING`.
5. Kill the backend while the algo is running — confirm the algo keeps
   ticking (only heartbeat send logs warnings, nothing else breaks).
6. Set `BROKER=zerodha` (or angelone/icici_breeze) with no credentials —
   confirm it fails fast with `BROKER_CONNECT_FATAL` / `BROKER_NOT_IMPLEMENTED`,
   not a live order attempt.

## Troubleshooting

- `ModuleNotFoundError: No module named 'trading'` — run from the repo
  root (`CentralizedAlgoSystem/`), not from inside `trading/`.
- `ModuleNotFoundError: No module named 'strategy_agent'` — same as above;
  `main.py` inserts the repo root onto `sys.path` automatically when run
  directly, but only if invoked from the file path shown above.
- Heartbeat not appearing on the dashboard — confirm `API_BASE_URL` points
  at a reachable backend and check `trading/logs/example_strategy.log` for
  `HEARTBEAT` warnings from `strategy_agent.agent`.
