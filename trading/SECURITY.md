# Security posture — CentralizedAlgoSystem

A living snapshot, updated as the system evolves — not a one-time
checklist. Current as of the `trading/` architecture consolidation.

See also: [`../README.md`](../README.md) and
[`../SYSTEM_DETAILED_GUIDE.md`](../SYSTEM_DETAILED_GUIDE.md) §15.

---

## In place

### Authentication & rate limiting

- **API-key auth on every `/api/*` route.** Shared `X-API-Key` header,
  checked against `CONTROL_API_KEY` in `trading/api/deps.py:require_api_key`.
  **Fails closed:** if `CONTROL_API_KEY` is unset on the server, every
  request gets HTTP 503 — never "allow everything".
- **Per-key rate limiting.** DB-backed fixed-window limiter,
  `trading/api/deps.py:enforce_rate_limit` (`RATE_LIMIT_MAX_REQUESTS` per
  `RATE_LIMIT_WINDOW_SECONDS`, default 60/60), returns 429 + `Retry-After`.
  DB-backed rather than in-memory so it works across serverless cold
  starts *and* multiple backend workers/processes. `require_api_key` runs
  first, so an invalid key 401s before the limiter ever queries the DB.
- **`/api/health` is the only unauthenticated `/api/*` route** — it is a
  probe. It reports `database: connected|error: <ExceptionClass>` and
  **never** includes the connection string / DSN.

### Auditing & input handling

- **Every control action is auditable.** `POST /api/algo/{start,stop,
  restart,update}` writes a `commands` row (algo, server, command,
  `requested_by`, PENDING) **before** the Lambda is invoked. A failed
  invocation updates it to `FAILED` + the real error — never silently
  lost. `trading/api/routes.py`.
- **Input validation.** Every request body is a Pydantic model
  (`trading/api/schemas.py`, `trading/api/legacy.py`). Every DB query goes
  through SQLAlchemy parameter binding — no string-interpolated SQL
  anywhere.
- **No silent auto-create.** An `algos` row is created only by
  `POST /api/algos`. A start/heartbeat/log/position call against an
  unregistered algo is a 404, so a typo'd id or a stray call from a
  decommissioned strategy cannot quietly reappear.

### Secrets

- **No secrets committed.** Verified by grepping tracked files and history
  for common secret patterns — clean. `.gitignore` covers `.env`,
  `.env.*` (keeping `*.env.example`), `*.log`, `*.db`/`*.sqlite3`,
  `.streamlit/secrets.toml`, and `*.pem`/`*.p12`/`*.pfx`/`credentials.json`.
- **No secrets in the Dockerfile or `docker-compose.yml`.** Every value is
  an environment variable with a non-sensitive local-only default;
  `.env` is git-ignored and `.dockerignore`'d.
- **Centralised config.** `trading/core/config.py:load_settings()` is the
  single place backend/control-plane settings (including
  `CONTROL_API_KEY`, `AWS_REGION`, `LAMBDA_FUNCTION_NAME`, Telegram tokens,
  broker credentials) are read. Nothing logs a credential — the canonical
  JSON logger records messages, not `Settings` objects.
- On EC2, `trading/api/lambda_client.py` uses the instance role (via
  `AWS_REGION` + default credential resolution), not a long-lived key.

### Trading safety gate

- **`TRADING_MODE` must be the literal string `live`** before the
  broker-abstraction path will place a real order. `Settings.is_live`
  (`trading/core/config.py`) is true only for `live` after strip +
  lowercase; anything else (unset, `paper`, `test`, `1`/`true`/`yes`, a
  typo) is paper. The check is enforced **in each real broker adapter,
  before any SDK call** — not at a higher layer that could be bypassed.
  Default: `paper`. An AWS deployment changes nothing about this.
- The two straddle/VWAP AngelOne algos (`CombinedVwapNifty`,
  `DoubleStraddelAlgo`) gate independently on `BOT_DRY_RUN` (env, default
  `true` = paper). `Vwap_Algo_Nifty_hedge` has **no** dry-run gate — its
  safety boundary is the broker account it is pointed at.

### Infrastructure

- **No inbound port on Strategy EC2s.** Control is via SSM only — no SSH,
  nothing for the dashboard/API to connect to directly.
- **No browser → AWS path.** Dashboard → API (auth'd) → Lambda → SSM. The
  browser never holds an AWS credential.
- **IAM least privilege** for the setup user and the EC2/Lambda/Scheduler
  roles — `trading/infrastructure/*/iam_*.json`, no `*` on `Resource`
  where AWS supports scoping it.
- **Non-root container.** The backend image runs as `appuser` (uid 10001).
- **Schema changes via Alembic**, hand-reviewed migrations — not an
  unconditional `create_all()` against production.

### Monitoring

- **Server-side stale-heartbeat alerting** (`trading/api/watcher.py`) —
  runs in the backend lifespan, scans the canonical `algos`/`heartbeats`
  model, and fires `alert_service.heartbeat_missing` for any non-STOPPED
  algo silent beyond `STALE_THRESHOLD_MINUTES`. Detection + alert only —
  it never stops a process, cancels an order, or squares off (asserted by
  test). This closes the previously-documented "no server-side silence
  detection" gap.
- Real-time status-transition alerts (`strategy_started/stopped/crashed/
  recovered`) and `day_loss_exceeded` fire from `POST /api/heartbeat` and
  the legacy `POST /update_strategy`. Telegram delivery has dedup + retry
  and is a no-op (logged, never raised) when creds are absent.

---

## Explicitly deferred (not built, not faked)

- **Full per-user authentication / RBAC.** The shared API key is a
  pragmatic baseline for a single-operator system, not a substitute for
  real user identity. Would need an auth provider integration in the API
  layer plus a `users`/`roles` schema addition. The schema is designed to
  accept it later.
- **Managed-Postgres Row-Level Security** (Supabase/RDS IAM-auth style).
  Doesn't apply as built: RLS policies evaluate against a Postgres session
  carrying a provider-issued JWT via that provider's client SDK. This
  project connects with a **direct connection string**
  (SQLAlchemy/psycopg2, `trading/database/connection.py`) — there is no
  such session for a policy to check against, and a service-role
  connection bypasses RLS anyway. Revisit only if per-user auth is added
  via a compatible provider.
- **Generic position-aware SAFE_STOP.** `STOP_ALGO` (and the kill switch)
  stop the **process**; they have no knowledge of open positions.
  Real square-off belongs in each strategy's `on_stop()` (the wired
  extension point — see `trading/algos/example_strategy/strategy.py`).
  The safe-shutdown milestone requires: disable entries → cancel orders →
  square off → **verify broker positions = 0** → persist → upload logs →
  stop; verification fails ⇒ **do not stop**, raise an alert.
- **Position reconciliation** against the broker on strategy
  startup/restart. On a crash/restart the broker's actual positions and
  open orders must be treated as the source of truth; local state must not
  be assumed correct. Not yet implemented.
- **`Vwap_Algo_Nifty_hedge` dry-run gate.** It places real orders whenever
  it has a working AngelOne session. Until it grows a `BOT_DRY_RUN`-style
  switch, control it at the broker-account level.

---

## Failure-mode summary

| Scenario | What actually happens |
|---|---|
| Database unavailable | The strategy's broker/strategy loop does not touch the DB. Heartbeat/log/position reporting fail silently (logged, swallowed). `GET /api/health` returns 503 `degraded` with `database: "error: <Class>"` — no 500, no DSN leak. |
| Backend / dashboard down | Zero effect on a running strategy — it is a pure control/read client. |
| Algo crashed / goes silent | Heartbeats stop → the watcher fires `heartbeat_missing` after `STALE_THRESHOLD_MINUTES`; `trading_agent.py` STATUS self-heals a stale PID file to `ERROR`. |
| Broker API unavailable | Reconnect-with-backoff in the algo. `BrokerConfigError` (bad credentials) fails fast instead of retrying a permanent problem. |
| Lambda failure | The `commands` row already records the request; it is updated to `FAILED` + the real error, never lost. |
| SSM failure | Surfaces as `ERROR`/`UNKNOWN` via `get_command_status`, never reported as `RUNNING`. |
| EC2 offline vs dead SSM agent | `check_ec2_health` distinguishes power state from SSM agent responsiveness; surfaced on-demand via `GET /api/server/status?live=true`. |
| Network interruption | Retry + backoff on the heartbeat/log senders; `reporting.py` trade/position/pnl reports fail silently rather than blocking the strategy loop. |
| Read-only filesystem | Root logging is stdout-only (always works); per-component file handlers are skipped with a warning; the app keeps running. |
| Telegram not configured | `alert_service` logs the alert and returns `False` — never a network call, never an exception. |
