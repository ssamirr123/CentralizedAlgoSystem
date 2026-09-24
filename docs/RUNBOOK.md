# Operator Runbook — AI Research Platform

No credentials appear in this document. See `.env.example` for variable
names only; real values live in `/etc/centralized-algo/backend.env` on
the host (root-owned, `chmod 600`) or AWS Secrets Manager, never here.

## AI jobs stuck (RUNNING forever)

1. Check `GET /api/ai-research/{id}/status` (or the backtest/options
   equivalent) — if `current_stage` hasn't advanced in longer than the
   configured timeout (`AI_RESEARCH_TIMEOUT_SECONDS`/`AI_OPTIONS_TIMEOUT_SECONDS`),
   the process likely crashed mid-job without a clean restart.
2. Restart the backend (`sudo systemctl restart centralized-algo-backend`).
   Startup recovery (`recover_interrupted_jobs()`) will mark any row
   still `RUNNING` as `FAILED` with `error_category="INTERRUPTED"`.
3. Verify: `GET /api/ai-research/history` shows the job as `FAILED`, not
   stuck `RUNNING`.
4. Re-submit the job — a fresh run gets a new ID; nothing about the stuck
   row blocks a new submission.

## LLM rate limit / provider errors

1. Check the job's `provider_error` field — `LLM_RATE_LIMIT`,
   `LLM_AUTH`, or `LLM_TIMEOUT` distinguish the cause without exposing
   the raw provider response.
2. `LLM_RATE_LIMIT`: wait for the provider's window to reset (varies by
   provider/plan); no code-side retry storm will make this worse — a
   rate-limited call is never automatically retried.
3. `LLM_AUTH`: the configured provider API key is missing/invalid/
   expired. Rotate it in `/etc/centralized-algo/backend.env`, then
   restart the backend (env vars are read at request time within the
   process, but the initial `os.environ` snapshot for a already-running
   container needs a restart to pick up a changed env file).
4. If a provider is having a broader outage: set
   `AI_WORKLOADS_ENABLED=false` (or the narrower `AI_OPTIONS_RESEARCH_ENABLED`/
   `AI_BACKTEST_ENABLED`) to stop new submissions while you wait it out —
   this does not affect the Trading Control Center or Options
   Intelligence (which has no LLM dependency).

## Breeze unavailable

1. Check `GET /api/market/session/status` for `session_state`/`feed_state`.
2. A daily Breeze session token expiring is the most common cause —
   re-provision it via `POST /api/market/session` (ADMIN permission)
   with a freshly-generated token (Breeze's manual daily browser-login
   flow; no programmatic login exists).
3. Existing trading execution is never affected by Breeze market-data
   outages — Breeze here is a market-data credential, entirely separate
   from any broker's trading credential (verify: `trading/common/brokers/`
   is a disjoint import graph from `trading/market_data/providers/`).
4. Historical AI Research/Options Intelligence results already persisted
   remain readable regardless of current Breeze availability (Options
   Intelligence's `as_of` path never calls Breeze).

## Database unavailable

1. `GET /api/health` returns `503` with `status:"degraded"`,
   `database:"error: <ExceptionClass>"` — never a raw connection string
   or password.
2. No job can silently "complete" during a DB outage — every
   `mark_completed`/`mark_failed` call requires a DB write; if the write
   fails, the job stays in its last successfully-persisted state (never
   falsely marked done).
3. Once the DB returns, `GET /api/health` recovers automatically on the
   next request (no restart needed for this specific failure mode,
   verified by `test_health_recovers_after_transient_failure`).
4. Check container logs for the Postgres container specifically
   (`docker compose logs postgres`) if the outage persists beyond a
   transient network blip.

## Disk usage high

1. Check `/var/lib/centralized-algo/pgdata` (Postgres data) and
   `AI_RESEARCH_DATA_DIR` (TradingAgents' own cache/checkpoint files —
   containment-only, safe to clear if the disk is critical and no
   research is mid-run).
2. No automatic retention/purge job runs today — `MARKET_DATA_RETENTION_DAYS`/
   `OPTION_DATA_RETENTION_DAYS` are configured limits, not enforced by a
   scheduler. If a manual purge is needed, use the existing
   `purge_older_than()` repository function for AI Research explicitly —
   never delete market-data/option rows without deliberately deciding to
   (Section 21: valuable historical data is never auto-purged).

## Disable AI workloads (incident kill switch)

```
# in /etc/centralized-algo/backend.env
AI_WORKLOADS_ENABLED=false
```

then restart the backend. This is NOT the trading kill switch — the
existing Trading Control Center, broker connectivity, and
`TRADING_MODE` are completely unaffected. Read-only AI history remains
browsable.

## Restart service

```
sudo systemctl restart centralized-algo-backend
# or, from the app directory:
docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml restart backend
```

## Verify recovery after a restart

```
curl -s https://<domain>/api/health
# expect: {"status":"ok", "database":"connected", "ai_subsystem": {...}, "market_data_subsystem": {...}}

curl -s https://<domain>/api/ai-research/history -H "Authorization: Bearer <token>"
curl -s https://<domain>/api/ai-options-research/history -H "Authorization: Bearer <token>"
# expect: any job that was RUNNING before the restart now shows FAILED
#         (error_category=INTERRUPTED), never still RUNNING.
```

## Check migrations

```
docker compose ... exec backend python -m alembic current
docker compose ... exec backend python -m alembic check
# expect: current == head; check reports "No new upgrade operations detected."
```
