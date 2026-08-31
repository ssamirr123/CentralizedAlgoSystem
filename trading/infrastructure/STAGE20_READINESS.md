# Stage 20 — End-to-End Paper Trading Validation — production-readiness report

Date: 2026-08-31 · Mode: **PAPER** (verified: backend container + strategy
box both `TRADING_MODE=paper`, `BROKER=paper`) · No live orders placed.

Architecture under test:
`React → CloudFront/S3 → FastAPI EC2 → PostgreSQL → Lambda → SSM → Strategy EC2 → Paper Broker`

## Verdict

**Not production-ready.** The data plane (registration, telemetry
ingestion, persistence, auth/RBAC, restart recovery) is solid. The
**control plane is broken end-to-end**: the dashboard cannot start/stop a
strategy because the FastAPI→Lambda hop was never wired (P0). A
deployment split-brain (P0) also meant that after an unplanned reboot
during this validation the dashboard had **zero user accounts**.

---

## What passed

| Area | Evidence |
|---|---|
| Strategy registration | `GET/POST/DELETE /api/algos`; duplicate → **409**, no partial row; new algo persists and is deletable |
| EC2 status | `check_ec2_health` (direct Lambda) → `RUNNING / ssm ONLINE / healthy: true` |
| START / STOP / RESTART | **direct Lambda** → `job_id`, agent executes, `get_command_status` resolves; strategy process stops/starts on the box |
| Heartbeat | flowing ~10s, monotonic, 1247 rows, visible in `/api/algos` + `/strategies` |
| P&L / trades / positions / logs | POST (service key) → GET round-trips; rows persist (`daily_pnl`, `trades` insert-only, `positions` upsert, `logs` with level filter) |
| Alerts | ERROR log stored; status-transition + day-loss + stale-heartbeat alert paths wired (Telegram); audit rows on denials |
| Database persistence | survived **backend container restart** AND a **full EC2 stop/start** (10:30 UTC reboot) — bind-mount Postgres, every row intact, `alembic` at head |
| Restart recovery | (a) EC2 stop/start → systemd → `docker compose` → healthy, 0 data loss; (b) strategy STOP → **watchdog 2-strike auto-recovery ~20s**; (c) backend container restart → 200 in ~4s |
| Lambda failures | bogus action → `{success:false,error:"unknown action"}`, no crash |
| SSM failures | bad instance → `InvalidInstanceId` surfaced; bad algo → agent `AlgoNotFoundError` surfaced via poll, **no junk left on box** |
| EC2 failures | bogus instance → `InvalidInstanceID.NotFound` surfaced; **safe-stop guard blocks `stop_ec2` while `example_strategy` is alive** (lists PID, requires `force:true`) |
| API failures | 401 (no auth), 403 (RBAC — service key cannot START), 422 (malformed), **429** (60 req/min per identity enforced), 502→200 (~4s) on restart |
| Database failures | prod PG not killable safely; `_check_database()` probe verified live; degraded path (`503` + `database: error:<Class>`, never 500) is unit-tested (`tests/api/test_health.py`) |
| Multiple market sessions | 2× START→heartbeat-advance→STOP cycles + 1 direct-Lambda STOP→watchdog-recovery cycle; telemetry + persistence checked each cycle |
| Auth / RBAC | admin login via CloudFront; Stage 18 boundaries hold under load |
| Frontend | loads via CloudFront (200 + SPA shell), deep-link `/commands` 200, `http→https` 301, HSTS + `nosniff`, `/api/health` proxied |
| No live orders | `TRADING_MODE=paper` on both EC2s; paper broker; `example_strategy` placed 0 orders |

---

## Gaps

### P0 — blockers

**1. FastAPI → Lambda hop not wired.** `LAMBDA_FUNCTION_NAME` is unset in
the backend container and the backend instance role `TradingEC2SSMRole`
has no `lambda:InvokeFunction`. Every dashboard-initiated action fails:

```
commands.error = "LAMBDA_FUNCTION_NAME environment variable is not set"
GET /api/server/status?live=true  -> degrades to cached ("status":"UNKNOWN")
POST /api/algo/{start,stop,...}    -> command status FAILED
```

The strategy is controllable today only by EventBridge→Lambda and the
local systemd unit + watchdog. Flagged as pending since Stage 15.

**Fix (needs an admin identity — `trading-control-cli` cannot grant IAM):**
```sh
aws iam put-role-policy --role-name TradingEC2SSMRole \
  --policy-name InvokeTradingOrchestrator \
  --policy-document file://trading/infrastructure/lambda/iam_backend_invoke_orchestrator_policy.json
# then on the backend box: add to /opt/centralized-algo/app/.env
#   LAMBDA_FUNCTION_NAME=TradingOrchestrator
#   AWS_REGION=ap-south-1
# add both to the docker-compose environment: block, then
#   docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml up -d
```
Then re-run START/STOP/RESTART from the dashboard.

**2. Database split-brain.** Stage 18 was deployed and verified with bare
`docker compose up` → the **`app_pgdata` named volume**. Production boot
(the `centralized-algo-backend` systemd unit) runs
`docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml up` →
the **`/var/lib/centralized-algo/pgdata` bind mount** — a *different*
Postgres. The bind-mount DB only got the Stage-18 `users` / `auth_sessions`
/ `audit_log` tables created (by migration, on the reboot during this
stage) and was never seeded — bootstrap env had been removed after
seeding the *other* DB. Result: **after the 10:30 UTC reboot the
dashboard had 0 users; nobody could log in.** A Stage 20 admin was
created on the running (bind-mount) DB to continue:

> username `admin` — password handed over separately (in the SSM command
> output); **rotate it via the UI**.

The `app_pgdata` volume still holds the Stage-18 admin + 59 audit rows,
now orphaned. **Decide which DB is canonical** (the bind-mount one is
what survives reboots → keep it) and always deploy with the same compose
invocation the systemd unit uses.

### P1

**3. Uncontrolled continuous deploy.** systemd unit does
`ExecStartPre=git pull --ff-only` + `up -d --build` on every boot. Any
push to `web-base-algo-trading-control` auto-deploys on the next backend
stop/start. Stage 18 reached prod this way. Stage 19 (`57d4283`,
currently unpushed) would deploy on the next push + reboot **without the
nginx WebSocket-upgrade change it needs**. Gate deploys behind a tag or a
deploy branch.

**4. Legacy `ec2_start` / `ec2_stop` EventBridge schedules act on the
BACKEND EC2** on an uncontrolled cadence — this caused a ~3-minute
outage at 10:30 UTC during this validation. Flagged "redundant" in
Stage 15, never removed. Delete them. (`TradingSchedule-StopEC2` at
16:00 IST correctly targets the strategy box and was **correctly blocked
by the safe-stop guard** today — that part works.)

**5. Dashboard STOP does not stick.** A STOP issued outside the
systemd/scheduler layer is reverted by the watchdog in ~20s (Stage 15
"coordination gap"). Either the stop path must also `systemctl stop` the
`centralized-algo-strategy@<algo>` unit, or the watchdog needs to honour
a scheduler-set pause flag.

### P2

6. Backend restart = ~4s of `502` (no draining / blue-green).
7. `example_strategy` is a template that places no orders — real
   paper-broker **trade generation** is unvalidated (only the ingestion
   path is).
8. CloudWatch agent can't tail `/var/log/nginx/*.log` (perm) —
   pre-existing; host metrics + `HealthOK` canary unaffected.
9. Realtime (Stage 19) not deployed → dashboard is poll-only (works, by
   design fallback).

---

## System state left after this stage

- **Strategy EC2** `i-0b0c8bb83adeb716b`: 1× `example_strategy` RUNNING
  (watchdog-recovered), watchdog timer active, `TRADING_MODE=paper`.
- **Backend EC2** `i-0f344752a1ca2811b` (EIP `13.232.95.211`): healthy,
  `TRADING_MODE=paper`, `alembic` at `a1b2c3d4e5f6`, Postgres private
  (`127.0.0.1:5432`), nginx + CloudWatch agent + `HealthOK` canary
  active. **New admin user `admin`** on the running DB.
- **CloudFront** `d1135mn36rkeep.cloudfront.net`: serving the Stage-18
  frontend; HTTPS + SPA routing OK.
- Test data left (paper, harmless): 1 trade, 1 position, 2 `daily_pnl`,
  a few `S20_*` logs.
- No repository changes beyond this report +
  `iam_backend_invoke_orchestrator_policy.json`.

## Deploy outcome (2026-08-31, "deploy all")

Applied:
- **Stage 19 realtime** deployed. Backend rebuilt via the prod overlay
  (`docker compose -f docker-compose.yml -f
  trading/infrastructure/backend/docker-compose.prod.yml up -d --build`)
  at commit `f1b0d24`; `/api/ws` live. nginx on the box already had the
  WebSocket upgrade `map` + headers — **no nginx change needed**.
  Frontend rebuilt (`index-CwsyyL7s.js`) and pushed to S3 + CloudFront
  invalidated. E2E verified through CloudFront: `wss://…/api/ws`
  handshake with `bearer.<jwt>` subprotocol → `hello`, live `heartbeat`
  + `pnl` events, monotonic/unique `seq`, ping→pong, bad token → close
  `1008`.
- **P0 #2** — deploys now use the same compose invocation as the systemd
  unit (the `/var/lib/centralized-algo/pgdata` bind mount is canonical);
  the Stage 20 `admin` user is on that DB and survives restarts.
- **P0 #1 (partial)** — `LAMBDA_FUNCTION_NAME=TradingOrchestrator` +
  `AWS_REGION=ap-south-1` set in the backend container + compose
  passthrough. Still needs the IAM grant below.

Still requires an admin identity (`trading-control-cli` cannot):

```sh
# P0 #1 — let the backend invoke the orchestrator
aws iam put-role-policy --role-name TradingEC2SSMRole \
  --policy-name InvokeTradingOrchestrator \
  --policy-document file://trading/infrastructure/lambda/iam_backend_invoke_orchestrator_policy.json

# P1 #4 — stop the legacy schedules bouncing the backend EC2
aws scheduler delete-schedule --region ap-south-1 --name ec2_stop
aws scheduler delete-schedule --region ap-south-1 --name ec2_start
```

After the IAM grant, dashboard START/STOP/RESTART + live EC2 health work
end-to-end (re-run the Stage 20 control-plane checks to confirm).

## Recommended order of remediation

1. **#2** pin the canonical DB + fix the deploy command (prevents another
   "no users" incident).
2. **#1** wire the FastAPI→Lambda hop (makes the dashboard actually
   control trading).
3. **#4** delete the legacy backend stop/start schedules.
4. **#3** gate deploys.
5. **#5** reconcile watchdog vs dashboard STOP.
6. Deploy Stage 19 (with the nginx change) for realtime.
7. Re-run this Stage 20 matrix end-to-end once #1 + #2 are done.
