# CentralizedAlgoSystem — Detailed System Guide

The canonical architecture after the `trading/` consolidation. This
document is the reference; [`README.md`](README.md) is the short
orientation and [`trading/SECURITY.md`](trading/SECURITY.md) is the
security posture.

All commands below assume you are at the **repository root**. There are no
machine-specific absolute paths anywhere in this project.

Contents:

1. [Local development](#1-local-development)
2. [PostgreSQL](#2-postgresql)
3. [Alembic](#3-alembic)
4. [Docker](#4-docker)
5. [FastAPI application](#5-fastapi-application)
6. [Control-Center API](#6-control-center-api)
7. [Removed legacy surface](#7-removed-legacy-surface)
8. [Trading algorithms](#8-trading-algorithms)
9. [Paper trading](#9-paper-trading)
10. [Broker abstraction](#10-broker-abstraction)
11. [AngelOne-specific algos](#11-angelone-specific-algos)
12. [Heartbeats](#12-heartbeats)
13. [Monitoring](#13-monitoring)
14. [Logging](#14-logging)
15. [Security](#15-security)
16. [AWS deployment roadmap](#16-aws-deployment-roadmap)

---

## 1. Local development

**Requirements:** Python ≥ 3.9 (CI/Docker use 3.11). For anything beyond
the test suite you also need a PostgreSQL instance.

```bash
python -m venv .venv
. .venv/Scripts/activate                # Windows
# source .venv/bin/activate             # macOS/Linux
pip install -r requirements-dev.txt     # runtime deps + pytest
```

### Run the backend

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/centralized_algo"
export CONTROL_API_KEY="dev-control-key"
# TRADING_MODE defaults to "paper" — leave it

alembic upgrade head
uvicorn trading.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

Then: `http://localhost:8000/docs`, `GET /api/health`,
`GET /api/algos` (with the `X-API-Key` header).

The fastest full-stack path is Docker — see [§4](#4-docker).

### Run the tests

```bash
pytest
```

The suite (`tests/`) is deterministic and hermetic:

- `tests/conftest.py` pins `DATABASE_URL` to a throwaway SQLite file in a
  fresh temp dir (never the old shared `test_api.db`).
- An autouse fixture drops and recreates every table before each test.
- `_no_network` blocks any non-loopback socket connect (loopback stays
  open only for the event loop / TestClient portal).
- Lambda/SSM/boto, brokers, and Telegram are all mocked. `TRADING_MODE`
  and `BROKER` are forced to `paper`; AWS / broker / Telegram credentials
  are cleared.
- `configure_logging()` runs so tests exercise the real logging path.

Layout: `tests/api/`, `tests/database/`, `tests/common/`, `tests/core/`,
`tests/infrastructure/`.

### Run a strategy locally

```bash
pip install -r trading/algos/example_strategy/requirements.txt
cp trading/.env.example trading/.env      # defaults (BROKER=paper) work unchanged
python trading/agent/trading_agent.py START_ALGO example_strategy
python trading/agent/trading_agent.py STATUS   example_strategy
python trading/agent/trading_agent.py STOP_ALGO example_strategy
```

### Repository hygiene

`__pycache__/`, `*.pyc`, `*.log`, `*.db`, `.env` (except `*.env.example`),
`.pytest_cache/` are git-ignored. No generated log or database file is
tracked.

---

## 2. PostgreSQL

**PostgreSQL is the authoritative database.** SQLite is a fallback for the
isolated test suite and throwaway local scratch use only — never for the
Docker stack or production.

- One connection string: the `DATABASE_URL` environment variable, e.g.
  `postgresql://user:pass@host:5432/centralized_algo`.
- The engine, session factory and the single canonical SQLAlchemy `Base`
  live in `trading/database/connection.py`. It uses `NullPool` +
  `pool_pre_ping` (a pooled Postgres pooler already multiplexes) and, for
  the SQLite test fallback only, a per-connection `PRAGMA foreign_keys=ON`
  so tests don't silently accept FK violations Postgres would reject.
- Initially PostgreSQL runs **on the Backend EC2 itself**. Moving to RDS
  later is a `DATABASE_URL` change with no application code change.

**Tables** (all on one `Base`):

| Table | Purpose |
|---|---|
| `servers` | registered strategy hosts (EC2 instance id, region, os, repo path, provisioning status) |
| `algos` | registered strategies per server (name, script path, status, enabled) |
| `algo_runs` | per-run history (start/stop, pid, exit reason, pnl) |
| `heartbeats` | append-only control-centre heartbeat history |
| `logs` | shipped trading-significant log events |
| `positions` | current holdings (upserted; a 0-qty row is deleted) |
| `trades` | insert-only fill history |
| `daily_pnl` | one rollup row per algo/server/day (upserted) |
| `commands` | audit row for every control action (before Lambda is even called) |
| `rate_limit_windows` | DB-backed per-identity fixed-window counter |
| `users` / `auth_sessions` / `audit_log` | per-user auth: accounts, refresh sessions, security audit trail |
| `strategy_heartbeats` | dormant legacy table — retained for its historical rows; no active endpoint writes to it |

---

## 3. Alembic

Alembic is the migration mechanism. `create_all()` is **not** used for
production schema changes.

```bash
alembic upgrade head                          # apply everything
alembic revision --autogenerate -m "add X"    # generate the next migration (hand-review it)
alembic downgrade -1
alembic history        # list revisions
alembic current        # what the DB is at
alembic check          # error if models drift from migrations
```

- **Configuration:** `alembic.ini` leaves `sqlalchemy.url` blank;
  `alembic/env.py` injects it from
  `trading.database.connection.DATABASE_URL` and imports
  `trading.database.models` so every table is registered before
  autogenerate compares.
- **Baseline:** `alembic/versions/…_baseline_schema.py`
  (revision `402052c22dd1`, `down_revision = None`). A faithful snapshot
  of the pre-consolidation schema — pure `create_table` / `create_index`,
  nothing renamed or dropped. Verified: `alembic upgrade head` produces a
  schema **identical** to `create_all()` (11 tables, 96 columns, 38
  indexes, 15 FKs, 5 unique constraints), and `alembic check` reports no
  drift.
- **Fresh database** (Docker, CI, new environment): `alembic upgrade head`.
- **Existing database** that already has these tables but no
  `alembic_version` (built by an old `create_all()`): `alembic stamp head`
  **once**, then `alembic upgrade head` thereafter. Verified: after
  `stamp head`, `alembic check` is clean.
- PostgreSQL DDL rendered by the baseline uses `SERIAL` PKs and
  `TIMESTAMP WITH TIME ZONE`, wrapped in `BEGIN;…COMMIT;`.

---

## 4. Docker

`docker-compose.yml` brings up **two services**: `postgres` and `backend`.

```bash
cp .env.example .env
# edit .env: POSTGRES_PASSWORD (mirror it inside DATABASE_URL), CONTROL_API_KEY
docker compose up --build
```

- **`postgres`** — `postgres:16-alpine`, data in the `pgdata` named
  volume, `pg_isready` healthcheck. Not published to the host by default.
- **`backend`** — built from `Dockerfile`:
  - `python:3.11-slim`; deps from `requirements.txt` (all wheels, no build
    toolchain).
  - Runs as a **non-root user** `appuser` (uid 10001).
  - `ENTRYPOINT docker/entrypoint.sh` → `alembic upgrade head` → `exec` the
    CMD (`uvicorn trading.api.app:create_app --factory --host 0.0.0.0 --port 8000`). The
    backend does not become ready until migrations are applied.
  - `depends_on: postgres` with `condition: service_healthy`.
  - Healthcheck hits `GET /api/health`.
- **No secrets in the Dockerfile or compose file.** Every value comes from
  the environment via `${VAR:-default}` interpolated from `.env`. Defaults
  are non-sensitive and local-only. `DATABASE_URL` points at the
  `postgres` service — **the Docker stack never uses SQLite.**

Verify:

```bash
curl -s localhost:8000/api/health          # -> {"status":"ok",...,"database":"connected"}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/docs      # -> 200
curl -s localhost:8000/api/algos -H "X-API-Key: <your key>"       # -> []
docker compose logs backend | grep 402052c22dd1   # migration ran
docker compose down                        # clean shutdown; add -v to drop the volume
```

`.dockerignore` keeps `.git`, `tests`, caches, local DB/logs/state and
`.env` out of the build context.

Docker parity is deliberate: the same image can later run on ECS/EKS with
no application change.

---

## 5. FastAPI application

- **Factory:** `trading/api/app.py::create_app()` assembles the
  `FastAPI` object: `lifespan` (DB init + the stale-heartbeat watcher +
  admin bootstrap), CORS, security headers, and the routers — `/api`
  (control centre), `/api/auth`, `/api/admin`, `/api/health`, and the
  `/api/ws` WebSocket.
- **Entrypoint:** uvicorn runs the factory directly:
  `uvicorn trading.api.app:create_app --factory`. There is no separate
  `main` module.
- **Lifespan** calls `init_db()` once (idempotent; the canonical `Base`
  covers every table) and, unless `DISABLE_BACKGROUND_WATCHER` is set,
  schedules the stale-heartbeat watcher as a background task.
- **Docs:** `/docs` (Swagger), `/redoc`, `/openapi.json`.

---

## 6. Control-Center API

Mounted at `/api`. Every route is behind two dependencies:

1. `require_api_key` — checks the `X-API-Key` header against
   `CONTROL_API_KEY`. **Fails closed:** if `CONTROL_API_KEY` is unset on
   the server, every request gets HTTP 503 (never "allow all").
2. `enforce_rate_limit` — DB-backed fixed-window limiter, per API key
   (`RATE_LIMIT_MAX_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`, defaults
   60/60). Returns HTTP 429 with `Retry-After`.

`/api/health` is the one exception — it is unauthenticated and
rate-limit-exempt (it is a probe).

### Routes

| Route | Notes |
|---|---|
| `POST /api/algo/start` `stop` `restart` `update` | **async.** Writes a `commands` audit row, invokes the Lambda, returns a `command_id` + `job_id` immediately. Never claims RUNNING just because Lambda accepted the request. |
| `GET /api/command/{command_id}` | Poll for the real outcome. Reflects `trading_agent.py`'s own process-liveness check, not "SSM finished". Syncs the verified status back onto `algos.status`. |
| `GET /api/algo/status` | Convenience: STATUS + a short bounded wait. |
| `POST /api/servers` | Register an EC2 host. `auto_provision=true` (default) fires an async provisioning Lambda (attach IAM profile, reboot for SSM, clone repo, install deps) which reports back via `PATCH`. |
| `GET /api/servers`, `PATCH/DELETE /api/servers/{name}` | Delete refuses while algos are still registered against the server. |
| `POST /api/algos` | Register a strategy. This is the **only** way an `algos` row is created — start/heartbeat/log against an unregistered algo is a 404 (no silent auto-create). Also triggers a best-effort code sync on the target host. |
| `GET /api/algos`, `PATCH/DELETE /api/algos/{name}` | `GET` enriches each entry with `last_heartbeat` (computed `MAX(heartbeats.timestamp)`). Delete refuses while history exists unless `?force=true`. |
| `GET /api/server/status` | DB-cached; `?live=true` triggers a real `check_ec2_health` Lambda call (EC2 power state **and** SSM agent responsiveness). |
| `POST /api/heartbeat` | Append to `heartbeats`; update `algos.status` + `servers.last_heartbeat`. Fires Telegram alerts on status transitions. |
| `POST /api/logs`, `GET /api/logs` | Ingest / query shipped log events. Query filters: `level`, `event`, `log_date` (UTC day), `limit`. |
| `POST /api/positions`, `GET /api/positions` | Upsert current holdings; `quantity=0` deletes the row. |
| `POST /api/trades`, `GET /api/trades` | Insert-only fill history. |
| `POST /api/pnl`, `GET /api/pnl` | Upsert one rollup per algo/server/day. |
| `GET /api/pnl/today` | Bulk: `{ "algo|server": pnl }` for one calendar day in a single query. |

### Design principles

- Every control action is auditable — the `commands` row exists **before**
  the Lambda call, so a failed invocation is still recorded (`status:
  FAILED` + the real error), never lost.
- Every request body is a Pydantic model (`trading/api/schemas.py`); every
  query uses SQLAlchemy parameter binding, never string-interpolated SQL.
- `trading/api/lambda_client.py` reads `LAMBDA_FUNCTION_NAME` / `AWS_REGION`
  through `load_settings()`; on EC2 this uses the instance role, no
  long-lived keys.

---

## 7. Removed legacy surface

The pre-consolidation monitoring stack has been removed. For historical
context, it consisted of:

- `trading/api/legacy.py` — unauthenticated `POST /update_strategy`,
  `GET /strategies`, `GET /health`, backed by the `strategy_heartbeats`
  table.
- `strategy_agent/` — a second heartbeat client that posted
  `/update_strategy`.
- `dashboard/` + `streamlit_app.py` — a Streamlit dashboard that polled
  `GET /strategies`.
- `backend/` — an import shim (`backend.main:app`) and `database.py` /
  `models.py` / `schemas.py` re-export shims.
- `api/index.py` + `vercel.json` — a Vercel deployment entrypoint.

All of it is gone. Every strategy now heartbeats via
`ControlCenterHeartbeatAgent` → `POST /api/heartbeat`; daily P&L (and the
day-loss alert) go via `POST /api/pnl`; the dashboard is `frontend/`
(React, served from S3 + CloudFront); the sole deployment target is the
Backend EC2 (Docker + Nginx). The `strategy_heartbeats` table is kept
only for its historical rows — nothing writes to it.

---

## 8. Trading algorithms

### The template — `trading/algos/example_strategy/`

- `main.py` — fixed plumbing, the same for every strategy copied from the
  template: config → logging → PID file → broker connect (retry with
  exponential backoff; `BrokerConfigError` fails fast) → `on_start` →
  heartbeat agents + log shipper → tick loop → graceful shutdown →
  `on_stop` → broker disconnect. Emits an ordered set of structured
  events (`ALGO_STARTED`, `BROKER_CONNECTED`, `STRATEGY_INITIALIZED`,
  `HEARTBEAT_RUNNING`, `ALGO_RUNNING`, …, `ALGO_STOPPED_SAFELY`).
- `strategy.py` — the only file that changes per strategy:
  `on_start()` / `on_tick()` / `on_stop()`.
- Uses the **broker abstraction** ([§10](#10-broker-abstraction));
  default adapter is `PaperBroker`.

### Process control — `trading/agent/trading_agent.py`

One short-lived CLI command per invocation:
`START_ALGO`, `STOP_ALGO`, `RESTART_ALGO`, `STATUS`, `UPDATE`, `LOGS`.
State survives between invocations via three files under `trading/data/`:

| File | Written by | Meaning |
|---|---|---|
| `<algo>.pid` | the algo process | authoritative "alive right now" |
| `<algo>.state.json` | the agent | `started_at` / `stopped_at` / `last_command` metadata |
| `<algo>.stop_requested` | the agent (`request_stop`) | cross-platform graceful-stop flag the algo polls |

`STATUS` always re-checks real OS process liveness (not a stored flag). A
PID file with a dead process self-heals to `ERROR`. Graceful stop uses the
flag file (an OS signal from an unrelated process is unreliable on
Windows); a force-kill is the safety net after `STOP_GRACE_TIMEOUT_SECONDS`.

`STOP_ALGO` stops the **process**. It has no knowledge of open positions —
generic position-aware square-off (SAFE_STOP) belongs in each strategy's
`on_stop()` and is deferred (see [§15](#15-security)).

### Remote control

In production the Lambda orchestrator (`trading/infrastructure/lambda/
orchestrator.py`) runs these same CLI commands on the target host over
SSM. It routes per-server (each event carries `instance_id` / `repo_path`
/ `os_name`), returns `job_id`s, and surfaces the agent's own JSON output —
never "SSM succeeded" as "algo RUNNING".

---

## 9. Paper trading

**`TRADING_MODE=paper` is the safe default and the single gate for the
template path.** `Settings.is_live` (`trading/core/config.py`) is true only
when `TRADING_MODE` is *exactly* the string `live` (after strip +
lowercase). Anything else — unset, `paper`, `PAPER`, `test`, a typo,
`1`/`true`/`yes` — is treated as paper. An AWS deployment changes nothing
about this.

| Strategy | Live-order gate | Default |
|---|---|---|
| `example_strategy` and any template copy | `TRADING_MODE` must be exactly `live`; the real broker adapters also refuse `place_order`/`cancel_order` before any SDK call; the default adapter is `PaperBroker` (simulated fills) | **paper / safe** |
| `CombinedVwapNifty`, `DoubleStraddelAlgo` | **not** `TRADING_MODE` — each has its own `DRY_RUN` flag from env `BOT_DRY_RUN` (`BOT_DY_RUN` typo-alias), **default `true`**. `DRY_RUN=true` logs `[DRY_RUN] … SKIPPED (paper)` instead of placing orders. | **paper / safe** unless `BOT_DRY_RUN=false` **and** valid AngelOne credentials |
| `Vwap_Algo_Nifty_hedge` | **no dry-run gate.** Its order path (`rest_func.place_market_order` / `place_stoploss_order`) calls the AngelOne SDK directly. | Places **real orders** whenever it has a working AngelOne session. Run only against a paper/sandbox account — or don't start it — unless you intend live trading. |

If live broker code already exists in a strategy, it is preserved but must
not activate by accident: the template gate is `TRADING_MODE`, the two
straddle/VWAP algos gate on `BOT_DRY_RUN`, and `Vwap_Algo_Nifty_hedge`
must be controlled at the broker-account level.

---

## 10. Broker abstraction

`trading/common/broker.py`:

- `BrokerClient` — ABC: `connect`, `disconnect`, `is_connected`,
  `get_quote`, `place_order`, `cancel_order`, `get_positions`.
- `create_broker(config)` — factory keyed off `BROKER`
  (`paper` | `zerodha` | `angelone` | `icici_breeze`).
- Dataclasses: `Quote`, `OrderResult`, `Position`; exceptions
  `BrokerConnectionError` (retryable), `BrokerConfigError` (permanent —
  fails fast), `LiveTradingDisabledError`.

Adapters in `trading/common/brokers/`:

| Adapter | State |
|---|---|
| `paper_broker.py` | **Working simulator** — instant fills, zero live-order risk. Default. |
| `zerodha_kite.py`, `angelone.py`, `icici_breeze.py` | **Stubs.** `connect`/`get_quote`/`get_positions` raise `NotImplementedError`; `place_order`/`cancel_order` additionally refuse unless `TRADING_MODE=live` — checked in the adapter, before any SDK call, so a half-finished adapter still cannot fire a real order. |

**Scope:** only `example_strategy` (and copies of it) use this
abstraction. The three real strategies do **not** — see [§11](#11-angelone-specific-algos).
Unifying them onto `BrokerClient` is a separate future milestone; this
guide does **not** claim a broker abstraction exists for them.

---

## 11. AngelOne-specific algos

`CombinedVwapNifty`, `DoubleStraddelAlgo`, `Vwap_Algo_Nifty_hedge` are
self-contained strategies that **do not use `BrokerClient`**. Each vendors
its own AngelOne SmartAPI integration:

- `connectapi.py` — SmartConnect session / login
- `Websocket.py` (or `websocket_feed.py`) — market-data feed
- `token_file.py` — instrument/scrip master download
- `rest_func.py` — order placement / modification / cancellation
- `config.py` — per-algo parameters and (for two of them) the `DRY_RUN`
  paper switch
- `main.py` — the run loop (time-of-day driven)
- `monitor.py` — **the only bridge to the control centre**: best-effort
  `ControlCenterHeartbeatAgent` + `report_daily_pnl` / `report_position`,
  fully wrapped in `try/except` so a monitoring failure can never affect
  or stop the strategy. Uses `STRATEGY_NAME` / `SERVER_NAME` /
  `API_BASE_URL` / `CONTROL_API_KEY` from the environment (the same vars
  the agent's `START_ALGO` injects).

Because these algos hold their own broker session, the `TRADING_MODE` gate
does not protect them. Safety for them is: `BOT_DRY_RUN` (the two straddle/
VWAP algos, default paper) and the broker account they are pointed at
(`Vwap_Algo_Nifty_hedge` especially).

---

## 12. Heartbeats

One heartbeat path: `trading.common.heartbeat.ControlCenterHeartbeatAgent`.

| | Control-centre heartbeat |
|---|---|
| Sender class | `trading.common.heartbeat.ControlCenterHeartbeatAgent` |
| Endpoint | `POST /api/heartbeat` (service `X-API-Key`) |
| Storage | `heartbeats` — append-only; also sets `algos.status`, `servers.last_heartbeat` |
| Payload | `algo_id`, `server_id`, status, `cpu`, `memory` (psutil, this process), `pnl`, `position`, `timestamp` |
| Interval env | `CONTROL_HEARTBEAT_INTERVAL_SECONDS` (default 10) |
| Transport | daemon thread, retry + exponential backoff, non-blocking, never raises |
| Used by | every strategy — the three real algos via `monitor.py`, `example_strategy` directly (when `CONTROL_API_KEY` is set) |

Daily P&L is reported separately by `report_daily_pnl` → `POST /api/pnl`,
which also raises the `day_loss_exceeded` alert when
`abs(day_pnl) > DAY_LOSS_LIMIT`.

Related best-effort reporters (canonical only), all non-blocking and
failure-swallowing:

- `trading/common/log_shipper.py` — ships a curated event set
  (`START/STOP/ENTRY/EXIT/SL/RE-ENTRY/ERROR/WARNING/BROKER_ERROR/
  NETWORK_ERROR/SQUARE_OFF`) plus anything WARNING+ to `POST /api/logs`,
  via a bounded queue that drops the oldest entry on overflow.
- `trading/common/reporting.py` — `report_trade` / `report_position` /
  `report_daily_pnl` → `POST /api/trades|positions|pnl`. Single attempt,
  short timeout, logged-and-swallowed on failure: trading logic always
  takes priority over dashboard reporting.

---

## 13. Monitoring

### Server-side stale-heartbeat watcher — `trading/api/watcher.py`

Runs inside the FastAPI lifespan as a background task **unless
`DISABLE_BACKGROUND_WATCHER` is truthy** (set it on serverless; leave it
off on the EC2/Docker backend). Every `STALE_CHECK_INTERVAL_SECONDS`
(default 60) it:

1. Joins `algos → servers`, left-joins `MAX(heartbeats.timestamp)` per
   algo.
2. For every **non-STOPPED** algo whose latest heartbeat (or, if it never
   sent one, `algos.updated_at`) is older than `STALE_THRESHOLD_MINUTES`
   (default 2), fires
   `alert_service.heartbeat_missing(algo_name, server_name, minutes=…)`.

It is **strictly detection + alert**. It never stops a process, cancels an
order, or squares off a position — verified by test (DB rows unchanged
after a scan) and by a source scan for forbidden symbols. `STOPPED` algos
are excluded (a deliberately-stopped strategy going quiet is not a fault).

### Telegram alerts — `alerts/telegram.py`

Singleton `alert_service`. Six named alert types: `strategy_started`,
`strategy_stopped`, `strategy_crashed`, `strategy_recovered`,
`day_loss_exceeded`, `heartbeat_missing`. Features:

- Dedup window (`ALERT_DEDUP_SECONDS`, default 120) suppresses identical
  alerts; `strategy_recovered` clears the `missing`/`crashed`/`stopped`
  dedup keys so the next incident fires fresh.
- Retry with exponential backoff (`ALERT_MAX_RETRIES`,
  `ALERT_RETRY_DELAY`); 4xx is not retried.
- Thread-safe — safe to call from FastAPI worker threads and the watcher.
- **No `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`** → it logs the alert and
  returns `False`. It never raises and never makes a network call. This is
  the normal state in dev and CI.

### Health

`GET /api/health` (see [§6](#6-control-center-api)) — `SELECT 1` DB probe,
`200` / `503`, never 500, never leaks the DSN.

### Failure modes

| Scenario | What happens |
|---|---|
| Database unavailable | The strategy's broker/strategy loop does not touch the DB at all. Heartbeat/log/position reporting fail silently (logged, swallowed). `/api/health` returns 503 `degraded`. |
| Backend / dashboard down | Zero effect on a running strategy — it is a pure control/read client. |
| Broker API unavailable | Reconnect-with-backoff in the algo. `BrokerConfigError` (bad creds) fails fast instead of retrying a permanent problem. |
| Lambda failure | The `commands` row already exists; it is updated to `FAILED` + the real error, never lost. |
| SSM failure | Surfaces as `ERROR`/`UNKNOWN` via `get_command_status`, never `RUNNING`. |
| Algo crashed | Heartbeats stop → watcher fires `heartbeat_missing`; `trading_agent.py` STATUS self-heals a stale PID file to `ERROR`. |
| Network blip | Retry+backoff on heartbeat/log senders; `reporting.py` fails silently rather than blocking the loop. |

---

## 14. Logging

`trading/common/logger.py` is the single logging authority.

### Root — `configure_logging(*, level=None, force=False)`

Call **once** at process start. The FastAPI factory (`trading/api/app.py`)
calls it before importing the sub-modules that log at import time.

- Installs **exactly one** handler on the root logger: a
  `StreamHandler(sys.stdout)` with `RootJsonFormatter`
  (`{"timestamp","level","logger","message", event?, details?, exc_info?}`).
- **stdout only** — never touches the filesystem, so it behaves
  identically whether the FS is writable or read-only; container /
  CloudWatch / journald friendly.
- **Idempotent** (module flag). Removes any bare `logging.basicConfig()`
  `StreamHandler` (exact type — not `FileHandler`, not framework
  subclasses like pytest's capture handler) so there are **no duplicate
  lines**.
- `ensure_ascii` keeps non-ASCII (e.g. alert emoji) as `\uXXXX` escapes,
  so a `cp1252` Windows console can never raise `UnicodeEncodeError`
  mid-log.

### Per-component — `get_logger()` + `log_event()`

`get_logger(component, server, algo)` returns a structured logger
(`propagate=False`, its own handlers): a stdout `JsonFormatter`
(`{"timestamp","level","component","server","algo","message", event?,
details?, exc_info?}`) and — **when the FS is writable** — a
`RotatingFileHandler` at `trading/logs/<component>.log`
(10 MB × 5). On a read-only FS the file handler is skipped with a warning;
stdout logging still works.

`log_event(logger, level, event, **details)` emits one structured event
line and, if a `LogShipper` is attached, ships qualifying events to
`POST /api/logs`.

### Everything else

Other modules (including `alerts/telegram.py`) just call
`logging.getLogger(__name__)` and **propagate** — no module configures its
own handler or calls `basicConfig`. `alerts/telegram.py`'s old
`basicConfig` + ad-hoc `FileHandler` to `telegram_alerts.log` were
removed; if it is imported before `configure_logging()` runs, Python's
last-resort handler still prints WARNING+ to stderr — never a crash.

Generated `*.log` files are git-ignored and none are tracked.

---

## 15. Security

Summary; full posture in [`trading/SECURITY.md`](trading/SECURITY.md).

**In place**

- API-key auth on every `/api/*` route, **fails closed** (503) if
  `CONTROL_API_KEY` is unset.
- Per-key DB-backed fixed-window rate limiting.
- A `commands` audit row written **before** every control action.
- Pydantic validation on every request body; parameterised SQL only.
- No secrets committed; `.gitignore` covers `.env`, `*.log`, `*.db`,
  credential file patterns.
- `TRADING_MODE` safety gate for the template path; `BOT_DRY_RUN` for two
  of the AngelOne algos (both default to paper).
- Non-root container user (`appuser`, uid 10001).
- Server-side stale-heartbeat alerting (was a documented gap; now built —
  [§13](#13-monitoring)).
- Control path is Dashboard → API (auth'd) → Lambda → SSM; the browser
  never holds an AWS credential; no inbound port on Strategy EC2s.

**Explicitly deferred (not built, not faked)**

- Full per-user authentication / RBAC (the shared key is a
  single-operator baseline).
- Generic position-aware SAFE_STOP — `STOP_ALGO` stops the process only.
- Position reconciliation against the broker on strategy
  startup/restart.
- `Vwap_Algo_Nifty_hedge` has no dry-run gate — treat its broker account
  as the safety boundary.

---

## 16. AWS deployment roadmap

Region: **`ap-south-1` (Mumbai)**. Nothing here is deployed automatically;
this is the target and the order.

```
        React frontend (future)  ──  CloudFront + S3
        (served from S3 + CloudFront)
                     │  HTTPS
                Backend EC2  (t3.medium — 2 vCPU / 4 GB)
                Nginx → Uvicorn → FastAPI → PostgreSQL (on the box)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  Strategy EC2   Strategy EC2   Strategy EC2   (t3.small each)
   trading-agent + one or more algos → Broker APIs (Dhan / Angel / …)

  EventBridge (Asia/Kolkata) → Lambda → start/stop Strategy EC2s
  CloudWatch  → EC2/CPU/mem/disk, backend health, heartbeat gaps, errors, alarms
  S3          → market-data/, trading-logs/, strategy-logs/, trade-reports/, …
  Secrets Manager → broker credentials
```

### Layers

- **Control plane** (Backend EC2): FastAPI + WebSocket (future) + auth +
  strategy config/monitoring + risk config + dashboard APIs + kill switch.
- **Execution plane** (Strategy EC2s): market data, strategy calc, signal
  generation, risk checks, order execution, position management, broker
  comms, strategy state. Independent of the frontend and of each other.
- **Data plane**: PostgreSQL, S3, logs, trade history, reports.

### Milestones (target order)

| # | Scope |
|---|---|
| 4 | Backend EC2: FastAPI + PostgreSQL (on the box), clean structure, `/health` + `/api/health`, Alembic, Docker, Nginx reverse proxy + WebSocket forwarding |
| 5 | Independent Strategy EC2s (`strategy-01/02/03`), each with `STRATEGY_ID` / `INSTANCE_ID` / `BACKEND_URL` / broker config |
| 6 | Strategy supervision: registration, heartbeat, health, start/stop/pause/resume |
| 7 | React frontend (do not rewrite; connect to the new APIs) |
| 8 | S3: structured paths for logs / market data / reports |
| 9 | AWS Secrets Manager for broker credentials |
| 10 | Lambda + EventBridge: start/stop Strategy EC2s by tag, `Asia/Kolkata` schedule |
| 11 | Safe shutdown: disable entries → cancel orders → square off → **verify broker positions = 0** → persist → upload logs → stop. Verification fails ⇒ **do not stop**, alert. |
| 12 | CloudWatch monitoring + alarms |
| 13 | Centralised kill switch (emergency square-off across all strategies; report SUCCESS / PARTIAL / FAILED — never claim success without confirmation) |
| 14 | Position reconciliation on every startup/restart — the broker is the source of truth |
| 15 | Failure recovery: retry logic, idempotency, state recovery, no duplicate orders on retry |
| 16 | Production security: IAM least-privilege, security groups, SSH to your IP only, DB not publicly exposed, HTTPS, audit logs |
| 17 | RDS migration path — `DATABASE_URL` change only, no code change |
| 18 | ECS/EKS path — Docker compatibility already maintained; no EC2-specific filesystem assumptions |

### Non-negotiables

- Trading execution never depends on the frontend/dashboard.
- No Lambda for continuous trading execution.
- PostgreSQL not publicly exposed; never `0.0.0.0/0` for the DB.
- Internal strategy-management endpoints not exposed publicly.
- Nothing switches to live trading automatically — `TRADING_MODE=paper`
  stays the default.
