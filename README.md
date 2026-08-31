# CentralizedAlgoSystem

A control plane for algorithmic trading strategies that run across one or
more servers, plus the strategies themselves.

- **Frontend** — a React + TypeScript dashboard (`frontend/`) served as a
  static build from **S3 behind CloudFront**. Talks only to `/api/*`
  (same origin via CloudFront → the backend). JWT auth + RBAC.
- **Control plane** — a FastAPI backend: auth/RBAC, strategy registry,
  start/stop/restart control (via AWS Lambda → SSM), heartbeat + log +
  P&L ingestion, `/api/health`, and a realtime WebSocket at `/api/ws`.
  Runs behind **Nginx on a Backend EC2** in production; containerised with
  Docker for local/CI.
- **Execution plane** — strategy processes (`trading/algos/*`) on separate
  Strategy EC2 instances, driven by the `trading-agent` CLI (invoked
  remotely via Lambda → SSM). Each runs independently; the backend or
  dashboard being down never stops a running strategy.
- **Data plane** — PostgreSQL (authoritative, local to the Backend EC2),
  plus per-run structured logs and CloudWatch metrics.

> **Safety default: `TRADING_MODE=paper`.** Nothing places a real order
> unless `TRADING_MODE` is *exactly* `live` (and, for the AngelOne algos,
> their own dry-run flag is turned off). See
> [Paper trading & live safety](#paper-trading--live-safety).

Full milestone-by-milestone target architecture and AWS roadmap:
[`SYSTEM_DETAILED_GUIDE.md`](SYSTEM_DETAILED_GUIDE.md).
Security posture: [`trading/SECURITY.md`](trading/SECURITY.md).

---

## Repository layout

```
frontend/               React + TypeScript dashboard (Vite). Built to static
                        files, served from S3 + CloudFront. Calls /api/* only.

trading/
  api/
    app.py              create_app() -- the FastAPI application factory
                        (uvicorn runs it as `trading.api.app:create_app --factory`)
    routes.py           control-center API  (/api/*, JWT/RBAC + rate limit)
    auth_routes.py      /api/auth/*  (login / refresh / logout / me / change-password)
    admin_routes.py     /api/admin/* (user administration + audit log)
    health.py           GET /api/health  (DB probe, unauthenticated)
    realtime/           WebSocket stream at /api/ws (Stage 19)
    watcher.py          server-side stale-heartbeat detection
    deps.py             auth (JWT + service key) + RBAC + rate-limit + DB session
    lambda_client.py    invokes the orchestration Lambda
    schemas.py          Pydantic request/response models for /api/*
    security/           passwords, JWT tokens, the permission model, audit
  core/
    config.py           load_settings() -- the single backend Settings layer
  common/
    config.py           load_config() -- the per-algo runtime config (TradingConfig)
    logger.py           configure_logging() + get_logger()/log_event()  (canonical)
    broker.py           BrokerClient ABC + create_broker() factory
    brokers/            paper (working), zerodha/angelone/icici_breeze (stubs)
    heartbeat.py        ControlCenterHeartbeatAgent -> POST /api/heartbeat
    log_shipper.py      ships curated log events -> POST /api/logs
    reporting.py        report_trade/position/daily_pnl -> POST /api/*
    utils.py            PID files, graceful-shutdown flag, process liveness
  database/
    connection.py       the ONE canonical SQLAlchemy Base / engine / session
    models.py           all tables (control-center + auth: users/sessions/audit)
    init_db.py          create_all() helper -- dev/tests only, NOT prod migrations
  agent/
    trading_agent.py    per-host CLI: START_ALGO / STOP_ALGO / RESTART_ALGO /
                        STATUS / UPDATE / LOGS
  algos/
    example_strategy/   the broker-agnostic template (uses BrokerClient)
    CombinedVwapNifty/  |
    DoubleStraddelAlgo/ |  three real strategies -- vendor their own AngelOne
    Vwap_Algo_Nifty_hedge/  SmartAPI client; do NOT use the broker abstraction
  infrastructure/       Lambda orchestrator, EventBridge Scheduler, IAM, SSM,
                        backend + frontend deployment, CloudWatch config

alembic/                migration environment; versions/ holds the migrations
alembic.ini
Dockerfile              backend image (uvicorn factory, non-root)
docker-compose.yml      backend + PostgreSQL, local/CI
docker/entrypoint.sh    runs `alembic upgrade head`, then the CMD
.env.example            docker-compose configuration template (placeholders)

alerts/telegram.py      Telegram alert service (dedup + retry; no-op without creds)
tests/                  the pytest suite
```

---

## Quickstart

### Option A — Docker (backend + PostgreSQL)

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD (keep it in sync inside DATABASE_URL)
# and CONTROL_API_KEY
docker compose up --build
```

The `backend` service waits for PostgreSQL to be healthy, runs
`alembic upgrade head`, then serves on `http://localhost:8000`.

```bash
curl -s localhost:8000/api/health          # {"status":"ok",...,"database":"connected"}
open  http://localhost:8000/docs           # OpenAPI UI
curl -s localhost:8000/api/algos -H "X-API-Key: $CONTROL_API_KEY"   # []

docker compose down                        # clean shutdown (add -v to drop the DB volume)
```

### Option B — Local Python

Requires Python ≥ 3.9 (CI uses 3.11) and a PostgreSQL you can reach.

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt

export DATABASE_URL="postgresql://user:pass@localhost:5432/centralized_algo"
export CONTROL_API_KEY="dev-control-key"
# TRADING_MODE defaults to paper -- leave it

alembic upgrade head
uvicorn trading.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

Without `DATABASE_URL`, the app falls back to a local SQLite file. That is
for **isolated tests only** — the production/Docker database is PostgreSQL.

### Run the test suite

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is fully isolated: its own throwaway SQLite file per session,
every table dropped/recreated per test, all network blocked, Lambda/SSM/
boto/broker/Telegram mocked. No real services are touched.

---

## API surface

### Control-Center API — `/api/*`

The canonical API. Every route requires the `X-API-Key` header
(`CONTROL_API_KEY`) and is rate-limited per key; auth **fails closed**
(HTTP 503) if `CONTROL_API_KEY` is unset on the server.

| Group | Routes |
|---|---|
| Algo control (async → poll) | `POST /api/algo/{start,stop,restart,update}` → `GET /api/command/{id}` |
| Algo status | `GET /api/algo/status` |
| Servers | `GET/POST /api/servers`, `PATCH/DELETE /api/servers/{name}`, `GET /api/server/status` |
| Algos | `GET/POST /api/algos`, `PATCH/DELETE /api/algos/{name}` |
| Ingestion (from strategies) | `POST /api/heartbeat`, `POST /api/logs`, `POST /api/positions`, `POST /api/trades`, `POST /api/pnl` |
| Read | `GET /api/logs`, `GET /api/positions`, `GET /api/trades`, `GET /api/pnl`, `GET /api/pnl/today` |

Control actions never claim success just because Lambda accepted the
request — the caller polls `GET /api/command/{id}` for the real,
process-liveness-verified outcome.

### `GET /api/health` (unauthenticated)

```json
{"status": "ok", "service": "centralized-algo-backend",
 "timestamp": "…", "database": "connected"}
```

`database` is checked with `SELECT 1`. If PostgreSQL is unreachable the
endpoint returns **HTTP 503** with `"status":"degraded"` and
`"database":"error: <ExceptionClass>"` — it never 500s, and never leaks
the connection string.

---

## Configuration

All backend/control-plane settings are read in **one place** —
`trading/core/config.py`, via `load_settings()`. Environment variable
names and defaults are unchanged from earlier scattered reads; nothing was
renamed.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local SQLite | PostgreSQL in Docker/production |
| `TRADING_MODE` | `paper` | **exactly `live` to permit real orders** |
| `BROKER` | `paper` | template algo only: `paper\|zerodha\|angelone\|icici_breeze` |
| `CONTROL_API_KEY` | *(unset → 503)* | shared `/api/*` key |
| `LOG_LEVEL` | `INFO` | |
| `DISABLE_BACKGROUND_WATCHER` | *(off)* | set truthy on serverless; leave on for the EC2/Docker backend |
| `STALE_THRESHOLD_MINUTES` | `2` | watcher: silence beyond this → `heartbeat_missing` alert |
| `STALE_CHECK_INTERVAL_SECONDS` | `60` | watcher scan cadence |
| `DAY_LOSS_LIMIT` | `10000.0` | legacy day-loss alert threshold |
| `RATE_LIMIT_MAX_REQUESTS` / `_WINDOW_SECONDS` | `60` / `60` | per-key fixed window |
| `AWS_REGION` / `LAMBDA_FUNCTION_NAME` | *(unset)* | control actions need these |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(unset)* | alerts log-only without them |
| broker creds | *(unset)* | `ZERODHA_*`, `ANGELONE_*`, `ICICI_BREEZE_*` |

The **per-algo runtime** keeps its own loader, `trading/common/config.py`
(`load_config()` → `TradingConfig`). The two layers read the same
variables with the same defaults for the fields they share.

Templates: [`trading/.env.example`](trading/.env.example),
[`trading/api/.env.example`](trading/api/.env.example),
[`.env.example`](.env.example) (docker-compose). **Never commit a real
`.env`.**

---

## Database & migrations

One canonical SQLAlchemy `Base` lives in
`trading/database/connection.py`; every model is in
`trading/database/models.py`.

**Alembic** is the migration mechanism.

```bash
alembic upgrade head           # apply migrations (Docker entrypoint does this)
alembic revision --autogenerate -m "add X"   # create the next migration
alembic downgrade -1
```

- Fresh database (Docker, CI, a new environment): `alembic upgrade head`.
- A database that already has these tables but no `alembic_version`
  (e.g. built by an old `create_all()`): `alembic stamp head` **once**,
  then `alembic upgrade head` thereafter.
- `create_all()` (`init_db()`) remains only for the isolated test fixture
  and quick local scratch use — **not** a production migration path.

Tables: `servers, algos, algo_runs, heartbeats, logs, positions, trades,
daily_pnl, commands, rate_limit_windows` (control centre) + the legacy
`strategy_heartbeats`.

---

## Trading algorithms

### The template — `trading/algos/example_strategy/`

`main.py` is fixed plumbing (config → logging → PID file → broker connect
with backoff → `on_start` → heartbeat agents + log shipper → tick loop →
graceful shutdown → `on_stop`). Only `strategy.py`
(`on_start`/`on_tick`/`on_stop`) changes per strategy. It uses the
**broker abstraction** and defaults to the working `PaperBroker`.

### The three real strategies

`CombinedVwapNifty`, `DoubleStraddelAlgo`, `Vwap_Algo_Nifty_hedge` are
self-contained. **They do not use `BrokerClient`** — each vendors its own
AngelOne SmartAPI client (`connectapi.py`, `Websocket.py`,
`token_file.py`). They talk to the control centre only through their own
`monitor.py` (heartbeat + P&L/position reporting), which is best-effort
and can never stop the strategy.

### Process control — `trading/agent/trading_agent.py`

A short-lived CLI, one command per invocation:

```bash
python trading/agent/trading_agent.py START_ALGO example_strategy
python trading/agent/trading_agent.py STATUS   example_strategy
python trading/agent/trading_agent.py STOP_ALGO example_strategy
```

State is tracked via `trading/data/<algo>.pid` (written by the algo),
`<algo>.state.json` (metadata) and `<algo>.stop_requested` (graceful-stop
flag). `STATUS` always re-checks real OS process liveness. In production
these commands are issued remotely by the orchestration Lambda over SSM.

> `STOP_ALGO` stops the **process**. Position-aware square-off belongs in
> each strategy's `on_stop()` and is not yet implemented generically
> (see SECURITY.md → *deferred*).

---

## Paper trading & live safety

| Strategy | Live-order gate | Default |
|---|---|---|
| `example_strategy` (+ any template copy) | `TRADING_MODE` must be exactly `live`; real broker adapters also refuse before any SDK call; default adapter is `PaperBroker` | **paper / safe** |
| `CombinedVwapNifty`, `DoubleStraddelAlgo` | **not** `TRADING_MODE`; each has its own `BOT_DRY_RUN` env flag (`BOT_DY_RUN` typo-alias), default `true` | **paper / safe** unless `BOT_DRY_RUN=false` **and** valid AngelOne credentials |
| `Vwap_Algo_Nifty_hedge` | **no dry-run gate** — its order path calls the broker directly | places **real orders** whenever it has a working AngelOne session. Run only against a paper/sandbox account, or don't start it, unless you intend live trading |

An AWS deployment does **not** flip anything to live: `TRADING_MODE`
defaults to `paper`, and `is_live` is true only for the literal string
`live`.

---

## Broker abstraction

`trading/common/broker.py` — `BrokerClient` ABC + `create_broker(config)`
factory. Adapters in `trading/common/brokers/`:

- `paper_broker.py` — a full working simulator (instant fills, zero
  live-order risk). **Default.**
- `zerodha_kite.py`, `angelone.py`, `icici_breeze.py` — **stubs**.
  `connect`/`get_quote`/`get_positions` raise `NotImplementedError`;
  `place_order`/`cancel_order` additionally refuse unless
  `TRADING_MODE=live`.

This abstraction is used by **`example_strategy` only**. The three real
AngelOne strategies bypass it entirely (see above). Unifying them onto
`BrokerClient` is a separate, future milestone.

---

## Heartbeats & monitoring

Every strategy runs `ControlCenterHeartbeatAgent`
(`trading/common/heartbeat.py`), which `POST`s to `/api/heartbeat` every
`CONTROL_HEARTBEAT_INTERVAL_SECONDS` (10). The backend appends to the
`heartbeats` table and updates `algos.status` + `servers.last_heartbeat`.
Daily P&L is reported separately via `report_daily_pnl` → `POST /api/pnl`,
which also fires the day-loss alert when `abs(day_pnl) > DAY_LOSS_LIMIT`.
The dashboard receives all of this live over the `/api/ws` WebSocket, with
polling as the fallback.

**Server-side staleness detection** (`trading/api/watcher.py`) runs inside
the app lifespan unless `DISABLE_BACKGROUND_WATCHER` is set. Every
`STALE_CHECK_INTERVAL_SECONDS` it scans the canonical model and fires
`alert_service.heartbeat_missing(algo, server, minutes=…)` for any
**non-STOPPED** algo whose latest heartbeat (or `algos.updated_at`
fallback) is older than `STALE_THRESHOLD_MINUTES`. It is strictly
detection + alert — it never stops a process, cancels an order, or squares
off.

Alerts go through `alerts/telegram.py` (`alert_service`): 6 named alert
types, a dedup window, retry with backoff, thread-safe. With no
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` it logs the alert and returns —
**never raises**.

---

## Logging

`trading/common/logger.py` is the single logging authority.

- `configure_logging()` — call once at process start (the app factory
  does). Installs **exactly one** structured-JSON handler on the root
  logger, to **stdout only** (works identically on a writable or
  read-only filesystem; container/CloudWatch friendly). Idempotent;
  removes any stray `logging.basicConfig()` handler so there are no
  duplicate lines.
- `get_logger(component, …)` + `log_event(logger, level, event, **details)`
  — per-component structured logs used by the strategy runtime; adds a
  rotating file under `trading/logs/` when the FS is writable and falls
  back to stdout-only when it is not.

`alerts/telegram.py` and other modules just call
`logging.getLogger(__name__)` and propagate — no module configures its own
handlers. Generated `*.log` files are git-ignored.

---

## Security

See [`trading/SECURITY.md`](trading/SECURITY.md) for the current posture:
per-user login (username + password, bcrypt) → short-lived JWT access
token + rotating refresh cookie (httpOnly, Secure, SameSite=Strict);
RBAC with the permissions `VIEW / START / STOP / RESTART /
TRADING_CONTROL / ADMIN`; the shared `X-API-Key` reduced to a VIEW-only
machine identity; CSRF double-submit on the cookie endpoints; a CORS
allow-list; per-identity rate limiting; an append-only `audit_log`;
Pydantic validation + parameterised SQL; no committed secrets; the
`TRADING_MODE` gate; and a non-root container user.

---

## AWS deployment roadmap

Target production shape (details and milestone breakdown in
[`SYSTEM_DETAILED_GUIDE.md`](SYSTEM_DETAILED_GUIDE.md)):

```
        S3 + CloudFront  (React frontend, static build)
                     │
              Backend EC2  (ap-south-1)
              Nginx → Uvicorn → FastAPI → PostgreSQL (local, on the box)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   Strategy EC2  Strategy EC2  Strategy EC2   →  Broker APIs (Dhan/Angel/…)
                     │
   EventBridge → Lambda  →  start/stop strategy EC2s (safe shutdown flow)
   CloudWatch          →  logs, alarms
   S3                  →  historical data, trading logs, reports
   Secrets Manager     →  broker credentials
```

- **Now:** Docker parity (`docker-compose.yml`) so the same image can run
  on the Backend EC2 and later move to ECS/EKS without app changes.
- **RDS later:** change `DATABASE_URL` only — no application code change.
- **PostgreSQL runs on the Backend EC2 initially**, not RDS.
- Lambda must never terminate a Strategy EC2 while positions may exist —
  disable new entries → cancel orders → square off → verify broker
  positions = 0 → persist state → upload logs → stop. If verification
  fails: **do not stop**, raise an alert.
