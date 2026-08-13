# Security posture — trading control center

Snapshot as of Milestone 12. Updated as the system evolves, not a
one-time checklist.

## In place

- **IAM least privilege everywhere**: `trading-control-cli` (setup),
  `TradingEC2SSMRole`, `TradingLambdaExecutionRole`, `TradingSchedulerRole`
  are each scoped to exactly the actions/resources they need — see
  `trading/infrastructure/*/iam_*.json`. None use `*` on `Resource` where
  AWS supports scoping it.
- **No inbound EC2 ports**: control goes through SSM only (Milestone 3).
  No SSH, no open ports for the dashboard/API to reach the instance.
- **No direct browser→AWS path**: dashboard → API (auth'd) → Lambda →
  SSM. The browser never holds an AWS credential.
- **API authentication**: shared `X-API-Key` header, checked on every
  `/api/*` route, **fails closed** if `CONTROL_API_KEY` is unset (503, not
  "allow everything") — see `trading/api/deps.py:require_api_key`.
- **API rate limiting** (Milestone 12): DB-backed fixed-window limiter,
  per API key — `trading/api/deps.py:enforce_rate_limit`. DB-backed
  specifically because Vercel serverless functions share no memory
  between invocations; an in-memory counter would silently reset on every
  cold start.
- **Audit log**: every control action (`start_algo`, `stop_algo`, ...)
  creates a `commands` row before the Lambda is even invoked, so it's
  traceable even if the invocation itself fails — `trading/api/routes.py`.
- **Input validation**: every request body is a Pydantic model
  (`trading/api/schemas.py`); every DB query goes through SQLAlchemy's
  parameterized queries, never raw string-interpolated SQL.
- **No secrets committed**: verified via `git log -p` grep for common
  secret patterns and a check of all tracked files (Milestone 12 audit) —
  clean. `.gitignore` (added Milestone 12 — there wasn't one before)
  covers `.env`, `.streamlit/secrets.toml`, and common credential file
  patterns going forward.
- **Broker safety gate**: `TRADING_MODE` must be the literal string
  `"live"` before any real broker adapter will place a real order —
  checked in the adapter itself, before any SDK call, not just at a
  higher layer that could be bypassed (Milestone 1).

## Explicitly deferred (not built, not faked)

- **Full per-user authentication / RBAC.** The shared API key is a
  pragmatic baseline for a single-operator system, not a substitute for
  real user identity. Flagged since Milestone 6, still the honest state
  here. Would need Supabase Auth (or similar) integration in the API
  layer plus a `users`/`roles` schema addition.
- **Supabase Row-Level Security.** Doesn't apply to this architecture as
  built: RLS policies evaluate against a Postgres session carrying a
  Supabase-issued JWT (via their client SDK/PostgREST). This project
  connects via a **direct Postgres connection string**
  (SQLAlchemy/psycopg2, see `trading/database/connection.py`) — there is
  no Supabase session for an RLS policy to check `auth.uid()` etc.
  against. Enabling RLS on these tables today would do nothing (the
  service-role connection bypasses it) or break every code path (if
  forced without a compatible session). If per-user auth is added later
  via Supabase Auth, RLS becomes meaningful again and should be
  revisited then — not before.
- **SAFE_STOP (position-aware stop).** `STOP_ALGO` stops the *process*
  today; it has no knowledge of open positions. Real position-aware
  square-off logic belongs in each strategy's `on_stop()` (the correct
  extension point, already wired — see `trading/algos/example_strategy/
  strategy.py`'s example block) once a real strategy exists with real
  positions to check against. Flagged since Milestone 2.
- **Server-side staleness alerting.** The dashboard detects
  RUNNING/STALE/OFFLINE client-side from heartbeat age (Milestone 8).
  There's no background process on Vercel serverless to notice *silence*
  (an algo that stops heartbeating without ever reporting ERROR) and
  alert on it server-side — that would need a scheduled check (extending
  Milestone 11's EventBridge Scheduler), not something the request-driven
  API can do on its own. What IS alerted: real-time ERROR-status
  heartbeats and status transitions (Milestone 12,
  `trading/api/routes.py:post_heartbeat`).

## Failure-mode summary (matches the project's own §19)

| Scenario | What actually happens |
|---|---|
| EC2 offline | `check_ec2_health` (Milestone 12) distinguishes this from a live SSM Agent; dashboard's System Health section surfaces it on-demand (30s cache), not proactively pushed |
| Algo crashed | Heartbeat stops → client-side STALE (20-60s) → OFFLINE (>60s); `trading_agent.py`'s own STATUS check self-heals a stale PID file to ERROR |
| Broker API unavailable | Reconnect-with-backoff in the algo process; `BrokerConfigError` (bad credentials) fails fast instead of retrying a permanent problem for ~90s |
| Lambda failure | `commands` row already exists with the request recorded before the Lambda call; failure updates it with `status: FAILED` + the real error, never silently lost |
| SSM failure | Surfaces as `ERROR`/`UNKNOWN` via `get_command_status`, never reported as `RUNNING` |
| Database unavailable | The algo's core broker/strategy loop doesn't depend on the DB at all — heartbeat/log/position reporting fail silently (logged, swallowed) without affecting trading |
| Dashboard unavailable | Zero effect on the EC2 algo loop — it's a pure read/control client |
| Network interruption | Retry+backoff on heartbeat/log senders; `reporting.py`'s trade/position/pnl reports fail silently rather than blocking the strategy loop |
