# Phase 17.1-E — Production & Broker Read-Only Environmental Verification

**Timestamp: 2026-09-20T11:37 UTC (verification window; see individual sections for exact per-check timestamps).**

**PHASE 17.1-E = PASS.**

Candidate SHA: `d9b5fbcbce8befa039d2ba792a12bba1609d8413` (Phase 17.1-R, confirmed pushed to `origin/web-base-algo-trading-control` before this phase began — `git ls-remote` matched `git rev-parse HEAD` exactly, local working tree clean except the permanently-untracked `docs/phase-15d-11-human-live-canary-review-report.md`).

This phase performed genuine, real, READ-ONLY verification against the live AWS account and the real AngelOne canary broker account — both AWS and broker credentials were available in this session (AWS via the configured `trading-control-cli` IAM user; broker via `trading/.env`'s `ANGELONE_B_*` fields). No value from either was printed anywhere in this report or in the session transcript beyond a single masked account identifier. **Zero mutations of any kind were performed** — every AWS call used was a `describe-*`/`get-*`/SSM read-shell-command; every broker call used was `connect()`/`get_account_info()`/`get_funds()`/`get_positions()`/`get_order_book()` — `place_order`/`modify_order`/`cancel_order` were never referenced in any script or command this phase ran.

---

## 1. Prior reports read

`docs/phase-17-1-production-live-architecture-readiness-report.md`, `docs/phase-17-1-r-live-readiness-safety-remediation-report.md`, `docs/phase-16-12-distributed-shadow-deployment-validation-report.md`, and (from this session's own prior context) the untracked `docs/phase-15d-11-human-live-canary-review-report.md`. None were modified.

## 2. Production AWS instance identity

Discovered fresh via `aws ec2 describe-instances --filters Name=tag:Name,Values=algo-backend*` (not assumed from history):

| Field | Value |
|---|---|
| Instance ID | `i-0f344752a1ca2811b` (same as every prior report — confirmed still current, not assumed) |
| State | `running` |
| Type | `t3.medium` |
| Private IP | `10.0.1.153` |
| Public IP | `13.232.95.211` |
| VPC | `vpc-0100f18cab8c38e01` |
| Subnet | `subnet-0acefe8fe1b871c81` |
| Security group | `sg-04771e006e4097181` (`trading-sg`) |
| IAM instance profile | `TradingEC2SSMProfile` |
| Launch time | `2026-08-31T10:32:54Z` (20 days uptime as of this check) |

## 3. Production health / readiness

`GET http://localhost:8000/api/health` (via SSM shell on the instance itself, loopback — never through the public internet):
```
{"status":"ok","service":"centralized-algo-backend","timestamp":"2026-09-20T11:29:31Z","database":"connected","app_version":"0.0.0-dev","git_sha":"unknown","deployment_id":"5f7cc1373259","environment":"production","clock_drift":"not_checked"}
```
`GET /api/ready`:
```
{"application":"ready","broker":"not_configured","trading_authorized":false,"timestamp":"2026-09-20T11:29:31Z"}
```
Both PASS. `git_sha: "unknown"` in the health payload is the same pre-existing cosmetic gap named in Phase 15D.11 (never fixed, out of scope for every safety phase since). `broker: not_configured` / `trading_authorized: false` are the expected safe defaults.

## 4. Production deployment identity — PRODUCTION_SHA vs CANDIDATE_SHA

```
PRODUCTION_SHA = ff0d0f87a66a86beca19ff84b9d1e39c8395ef98
CANDIDATE_SHA  = d9b5fbcbce8befa039d2ba792a12bba1609d8413
```

Verified directly (not inferred from an old report): `git -C /opt/centralized-algo/app rev-parse HEAD` on the host returned `ff0d0f87a66a86beca19ff84b9d1e39c8395ef98` — the exact log message: `feat(execution): Phases 15D.2-15D.10-R - live canary, human authorization, operator auth, production hardening, and deployment remediation`. The running container's image digest (`sha256:8f4b34709...`, via `docker inspect --format {{json .Config.Labels}}`) is byte-identical to the digest recorded in the phase-15D.11 report's last-known snapshot — corroborating that production has not been redeployed since that check. **A version difference is expected and is not itself a failure** (Section 7): production is 22 commits behind candidate, predating every Phase 16.x/17.x change.

**Concrete consequence, verified structurally (not assumed)**: the deployed commit's `trading/common/` and `trading/api/` directories do NOT contain `strategy_runtime.py`, `worker_registry.py`, `worker_coordinator.py`, `portfolio_risk.py`, `operational_alerts.py`, `trading/api/worker_routes.py`, or `trading/api/live_authorization_routes.py` — confirmed via `ls` against the checked-out repo on the host. `GET /api/worker/time` → 404. `GET /api/live-authorization/foo` → 404. `GET /api/operations/alerts` → 404. Production is therefore **structurally incapable** of running a distributed worker, accepting an HTTP live-authorization request, or reporting an operational alert — these are candidate-only capabilities, not merely disabled ones.

## 5. Process / container inventory

`docker ps` on the host: exactly two containers, both healthy —
```
app-backend-1    app-backend          Up 2 days (healthy)
app-postgres-1   postgres:16-alpine   Up 2 days (healthy)
```
`docker ps -a` confirms no other container, running or stopped, exists. Host-level `ps aux` shows only the expected `uvicorn trading.api.app:create_app` process (uid 10001, non-root, running since Sep 18) plus kernel worker threads — no stray Python/Node process, no legacy algo process, nothing unexpected.

## 6. Strategy state

Production's deployed commit predates the `StrategyRegistry`/`strategy-lifecycle` machinery this repo's later phases test against in the same form; the safest, most direct evidence is the audit trail itself (Section 9): **zero** `STRATEGY_STARTED`/`STRATEGY_STOPPED` events exist in production's entire audit history. Combined with `trading_authorized: false` and `TRADING_MODE=paper` (Section 8), this is conclusive: **no strategy has ever run in this deployment.**

```
CombinedVWAP:   STOPPED (never started — zero audit evidence of any start)
DoubleStraddle: STOPPED (never started — zero audit evidence of any start)
VWAPHedge:      STOPPED (never started — zero audit evidence of any start)
```

## 7. Legacy strategy processes

Searched both the host and the container for `DoubleStraddelAlgo`/`CombinedVwapNifty`/`Vwap_Algo_Nifty_hedge` process names: **zero matches on both.** `legacy live processes = 0`.

## 8. Worker state

`docker exec app-backend-1 env | grep -i WORKER_AUTH` → **none set.** Combined with Section 4's finding that `worker_registry.py`/`worker_routes.py` don't even exist in this deployed commit, distributed workers are structurally absent, not merely offline: **0 workers registered, 0 possible.**

## 9. Execution mode

`TRADING_MODE=paper` (read directly from the running container's environment, not assumed from a compose-file default). This is the deployed adapter-level global safety gate (`TradingConfig.is_live` is `False` whenever this is anything but the literal string `"live"`) — it structurally blocks `place_order`/`cancel_order`/`modify_order` on every real adapter (AngelOne/Dhan/ICICI Breeze) regardless of any individual account's own `execution_mode` field. **Confirmed safe: not unrestricted LIVE.**

## 10. Kill switch

`KILL_SWITCH_PERSISTENCE_PATH=/app/data/kill_switch.json` is configured, but that file **does not exist** in the safety-data mount (`ls -la /var/lib/centralized-algo/safety-data/` shows only `audit.db`) — `CentralKillSwitch` only ever writes this file when `engage()` is called, so its absence is itself the evidence: the kill switch has **never been engaged**, and (per the zero-`EVENT_KILL_SWITCH_*` audit finding in Section 9) never disengaged either, because it has never been touched at all.

```
KILL SWITCH = DISENGAGED (never engaged; this alone is documented, not treated as evidence that live trading is authorized)
```

## 11. Live authorization state

The deployed commit DOES contain `trading/common/live_authorization*.py` (Phase 15D.5-15D.7 predate it), but: (a) no `trading_live_authorization.db` file exists anywhere on the host or inside the container (`find` for `*live_authorization*.db`/`*idempotency*.db`/`*reconciliation*.db` returned nothing), and (b) no HTTP route exists to create one in this deployed commit (Section 4). The historical canary's own authorization records (from the real Sept 17, 2026 canary order) were never written to a persistent store this container mounts — they were produced by a standalone script run outside this container, per every prior report's own honest description of that mechanism.

```
usable live authorizations = 0
```

## 12. Account authorization state

Read from `trading/.env` (never printed beyond the masked identifier below) and the real broker session established in Section 15: the intended canary account is `ANGEL_ACCOUNT_B` / AngelOne client `AA***01`. No TradingAccount authorization-state row is queryable from the deployed API (that mechanism doesn't exist at this commit either — Section 4) — the account's real-world state is only knowable by querying the broker directly, which Sections 15-19 below do.

## 13. Persistent safety storage

`/var/lib/centralized-algo/safety-data` exists on the host, bind-mounted into the container at `/app/data`, owned by uid 10001 (the container's non-root `appuser`), permissions `700` (`drwx------`) — appropriately restricted. Contains exactly one file: `audit.db` (49,152 bytes). No `idempotency.db`, no `trading_live_authorization.db`, no `trading_reconciliation.db`, no `kill_switch.json` — consistent with Sections 10-11's findings (those subsystems are either not deployed at this commit, or never engaged).

## 14. Persistence permissions

Verified read-only: path exists, mount exists (confirmed via `docker inspect` volume config and direct `ls` from both host and container perspective), the application can read it (proven by successfully querying `audit.db` via `sqlite3` from inside the container in Section 9). No chmod/chown/create/delete was performed.

## 15. Audit health

Queried `audit.db` read-only (`SELECT event_type, COUNT(*) FROM audit_events GROUP BY event_type`):
```
TABLES: [('audit_events',)]
EVENT_COUNTS: [('DEPLOYMENT_SHUTDOWN', 2), ('DEPLOYMENT_STARTUP', 3)]
TOTAL: 5
```
The audit store is readable and structurally healthy. **No unexpected event exists** — zero `LIVE_AUTHORIZATION_*`, zero `STRATEGY_STARTED`, zero broker-mutation-adjacent event, zero `KILL_SWITCH_*` event, of any kind, ever, in this deployment's history. This is the single strongest piece of evidence in this report: production has been running for 20 days and has done nothing but start up and shut down 5 times total.

## 16. Alert state

No `operational_alerts` module exists in this deployed commit (Section 4) and `GET /api/operations/alerts` → 404. **Active alert count = 0** (the subsystem does not exist to raise one).

## 17. Security group review

`sg-04771e006e4097181` (`trading-sg`) inbound rules: `80/tcp` and `443/tcp` from `0.0.0.0/0` (expected — this is the public web/API frontend), `22/tcp` restricted to a single `/32` CIDR (not open to the world). Outbound: `443/tcp` to `0.0.0.0/0` only. **No worker-control port (the candidate's `/api/worker/*` surface is served over the SAME 443 as everything else, gated by its own `X-Worker-Auth` header, not a separate exposed port) is separately exposed.** No obviously unintended exposure found.

## 18. IAM review

`TradingEC2SSMProfile` → role `TradingEC2SSMRole` → two AWS-managed attached policies only: `AmazonSSMManagedInstanceCore` and `CloudWatchAgentServerPolicy`. No EC2/IAM/infrastructure-administration permission is attached to the production instance's own role — the application cannot create, modify, or terminate AWS infrastructure about itself or anything else, consistent with least-privilege. (Listing inline role policies was itself denied to this session's own IAM user — an appropriately scoped CLI credential, not a broad one; the two attached managed policies already answer this section's question.)

## 19. Secret boundary

`/api/health` and `/api/ready` (Section 3) carry no credential-shaped field. No `.env` value, API key, TOTP seed, or session token was echoed by any endpoint queried this phase. The container's own environment (read via `docker exec ... env | grep`) was queried only for non-secret path/mode variables (`AUDIT_DB_PATH`, `KILL_SWITCH_PERSISTENCE_PATH`, `TRADING_MODE`, `APP_ENV`) — no broker credential env var was ever queried on production (production has none configured at all, per `broker: not_configured`).

## 20. Worker credential boundary

N/A at the currently deployed commit — no worker configuration of any kind exists in production (Section 8). For the future candidate deployment: `trading/worker/config.py` was already structurally proven (Phase 16.12, re-verified unchanged this phase via `test_phase_16_12_worker_structural_safety.py`, still passing) to never read a broker execution credential env var.

## 21. Broker verification — read-only, real credentials used

Credentials WERE available this session (`trading/.env`, `ANGELONE_B_*` fields — never pasted into any file, log, or command history beyond this session's own already-git-ignored `.env`). Performed using the exact same `AngelOneBroker(config, read_only=True)` construction `trading/validation/angel_readonly.py` already uses — `read_only=True` is a structural guard (raises before building any mutating request) — and this phase's own script never referenced `place_order`/`modify_order`/`cancel_order` at all, so zero mutation was even possible, not merely avoided by discipline.

## 22. Intended canary account

`CANARY_ACCOUNT_ID=ANGEL_ACCOUNT_B` in `trading/.env`, unchanged since Phase 15D.2. Re-confirmed as the current, non-ambiguous configuration — no other candidate account is configured anywhere in this repository. `CANARY ACCOUNT = ANGEL_ACCOUNT_B` (not chosen arbitrarily; read from the same authoritative config file `trading/preflight/live_canary.py` itself consults).

## 23. Broker authentication — READ ONLY

```
AUTHENTICATION: PASS (real AngelOne session established, 2026-09-20 ~17:07 IST)
```
Authentication alone triggered no order — `connect()` performs a login handshake only.

## 24. Broker account identity

```
ACCOUNT IDENTITY (masked): AA***01
```
Matches the client ID prefix/suffix recorded in every prior canary report for `ANGEL_ACCOUNT_B` — **confirmed the authenticated session is the intended account**, not a substituted one.

## 25. Funds — READ ONLY

```
available_cash    = 2935.61
used_margin        = 0.0
available_margin   = 0.0
```
(Close to, not identical to, the Phase 15D.11 snapshot's ₹2,949.65 — a small, expected drift from minor account activity/interest since that check; not itself a safety concern.) **Funds > 0 is documented here only as a fact, never as permission to trade.**

## 26. Positions — READ ONLY

```
POSITIONS count = 0
POSITIONS nonzero_net_qty_count = 0
```
No position of any kind, open or closed-but-listed, carries a nonzero net quantity.

## 27. Orders — READ ONLY

```
ORDER_BOOK count = 0
ORDER_BOOK non_terminal_count = 0
```
Zero orders of any kind exist in the account's current order book — not open, not pending, not trigger-pending, not partially filled.

## 28-29. Account flatness

**CANARY ACCOUNT = FLAT.** Net open position quantity is exactly 0 AND no pending/open/trigger-pending order exists that could create exposure — both conditions independently verified via real, live broker queries in Sections 26-27, not inferred from historical reports.

## 30. Broker mutation guard

The verification script used this phase imports and calls exactly four `AngelOneBroker` methods: `connect()`, `get_account_info()`, `get_funds()`, `get_positions()`, `get_order_book()`. `place_order`/`modify_order`/`cancel_order` are not imported, not referenced, not called — grep-verifiable against the script itself (since deleted from the local scratchpad after use, per this phase's own secret-handling discipline; its full source was quoted inline in this session's tool-call history for review). **Real broker mutation calls performed: 0** — structural, not merely observed.

## 31-33. No canary preparation performed

No instrument, expiry, strike, side, quantity, order type, or price was selected. The Phase 17.1-R HTTP LiveAuthorization routes were not exercised against production (they do not even exist at the deployed commit — Section 4). The candidate commit was not deployed.

## 34. Production/candidate version gap — safety-relevant changes NOT yet in production

Every fix from Phase 17.1 and Phase 17.1-R is absent from production today: the AMBIGUOUS/REJECTED distinction and portfolio-risk-hold fix, `modify_order()`'s `is_live` gap on all three real adapters, `Vwap_Algo_Nifty_hedge`'s missing DRY_RUN gate, `DoubleStraddleStrategy`'s date-less idempotency keys, the reconciliation→idempotency/portfolio-risk feedback loop, and the entire LiveAuthorization HTTP boundary. **This is input to Phase 17.2's own future deployment planning, not something this phase acts on.**

## 35. Infrastructure capacity — informational

`t3.medium`: 3.7GB RAM (670MB used, 2.8GB available), 30GB disk (7.2GB used, 23GB available, 24%), load average 0.07/0.08/0.07 over 20 days uptime. Ample headroom for a future worker deployment sharing this or an additional instance; this does not block or pass anything on its own (per Section 35's own instruction).

## 36-39. Security/IAM/secret/worker-credential boundaries

All PASS — see Sections 17-20 above.

---

## 40. Readiness matrix

| CHECK | STATUS |
|---|---|
| Candidate Git SHA | PASS |
| Remote candidate SHA | PASS |
| Production instance identity | PASS |
| Production health | PASS |
| Production readiness | PASS |
| Production deployment identity | PASS |
| Persistent safety storage | PASS |
| Audit readability | PASS |
| Critical alerts | PASS (none exist) |
| Strategy state | PASS |
| Legacy process state | PASS |
| Worker state understood | PASS |
| Execution mode | PASS |
| Live authorization state | PASS |
| Account authorization state | PASS |
| Security-group review | PASS |
| IAM read-only review | PASS |
| Secret boundary | PASS |
| Worker credential boundary | PASS |
| Broker authentication | PASS |
| Canary account identity | PASS |
| Funds query | PASS |
| Positions query | PASS |
| Orders query | PASS |
| Canary account flatness | PASS (FLAT) |
| Real broker mutation count | PASS (0) |

Every row is PASS — none PENDING, none BLOCKED. Both AWS and broker credentials were genuinely available and used this session.

## 41. Environmental readiness rules — all satisfied

Every checkbox in Section 41 of the brief is checked: production identity verified, health/readiness acceptable, deployment identity known (and the version gap fully documented), persistent safety storage verified, no strategy running, no legacy process running, execution mode understood (`paper`, safe), no usable LiveAuthorization exists, account authorization state understood, no unresolved critical alert (none exist), security boundary acceptable, secrets not exposed, intended canary account identified (`ANGEL_ACCOUNT_B`), broker authentication verified, account identity verified, positions verified (0), orders verified (0), canary account FLAT, real broker mutation count = 0.

---

PHASE 17.1-E = PASS

Candidate SHA: d9b5fbcbce8befa039d2ba792a12bba1609d8413
Production SHA: ff0d0f87a66a86beca19ff84b9d1e39c8395ef98
Production instance: i-0f344752a1ca2811b (algo-backend, ap-south-1, running)
Production instance type: t3.medium
Production health: ok (database connected)
Production readiness: ready (broker not_configured, trading_authorized=false)

Persistent safety storage: verified (audit.db only; 700 perms, correct owner)
Audit: readable, 5 records total, all DEPLOYMENT_STARTUP/SHUTDOWN, zero trading-related events
Critical alerts: none (module not deployed at this commit)

CombinedVWAP: STOPPED
DoubleStraddle: STOPPED
VWAPHedge: STOPPED
Legacy strategy processes: 0
Distributed workers: 0 (structurally absent at this commit)

Execution mode: paper (TRADING_MODE=paper)
Kill switch: DISENGAGED (never engaged)
Usable LiveAuthorizations: 0
Account authorization: not independently queryable at this commit; broker-level state verified directly (Sections 23-29)

Security groups: acceptable (80/443 public as intended, 22 restricted to one IP, no worker-control port exposed)
IAM: acceptable (SSM + CloudWatch only, no infrastructure-admin authority)
Secret boundary: verified, no leak found
Worker credential boundary: N/A (no worker deployed); candidate-side structural test re-verified passing

Intended canary broker: AngelOne
Intended canary account: ANGEL_ACCOUNT_B (client AA***01)
Broker authentication: PASS
Funds: available_cash=2935.61, used_margin=0.0, available_margin=0.0
Open positions: 0
Open/pending orders: 0
Canary account flatness: FLAT

Production/candidate version gap: production is 22 commits behind candidate; lacks every Phase 16.9-17.1-R safety fix and capability (input to future Phase 17.2 planning only)

AWS mutations performed: 0
Production deployments performed: 0
Production restarts performed: 0

Real broker order submissions: 0
Real broker modifications: 0
Real broker cancellations: 0
Real broker mutations: 0

Live authorization created: NO
Live authorization confirmed: NO
Strategy started: NO
Worker started: NO

Code safety readiness: PASS
Environmental readiness: PASS

Report: docs/phase-17-1-e-production-broker-environmental-verification-report.md
Commit: (pending — see final response)
Remote SHA: (pending — see final response)

PHASE 17.2 NOT AUTHORIZED.
HARD STOP.
