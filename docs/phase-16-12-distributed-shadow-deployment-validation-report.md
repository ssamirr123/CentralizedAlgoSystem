# Phase 16.12 — Secure Distributed Worker Transport, Multi-EC2 Shadow Deployment & Final End-to-End Validation

## 1. Objective and scope completed

Convert the in-process distributed-worker foundation (Phase 16.9-16.11)
into a real, network-transported distributed architecture, and validate
it — locally first, then on real AWS EC2 infrastructure. **Both stages
were completed.** This report documents the full path: local real-HTTP
validation, an initial AWS IAM permissions blocker (resolved by the
user), a genuine AWS shadow deployment across 4 EC2 instances, real
distributed shadow execution end-to-end, failure injection, and full
teardown leaving zero residual cost and zero change to production.

## 2. AWS access — initial blocker and resolution

Before touching any cloud resource, a read-only inventory of the
configured AWS account (`ap-south-1`) was taken. The IAM user this
environment authenticates as (`trading-control-cli`) initially could
`DescribeInstances`/`DescribeSecurityGroups` only — `ec2:RunInstances`,
`ec2:CreateSecurityGroup`, `ec2:DescribeVpcs`, `ec2:DescribeSubnets`, and
`ec2:DescribeKeyPairs` were all explicitly denied (confirmed via
`--dry-run`, which mutates nothing). Rather than work around this or
request broader access, it was raised directly to the user, who updated
the IAM policy themselves (scoped to `ec2:*` provisioning actions,
region-restricted to `ap-south-1`) and confirmed before any AWS mutation
proceeded. A second read-only pass then confirmed the new permissions
and discovered the existing production `algo-backend` instance
(`i-0f344752a1ca2811b`, VPC `vpc-0100f18cab8c38e01`, security group
`trading-sg`) — production was **read-only inspected and never touched**
for the remainder of this phase.

## 3. Transport selection

**HTTPS/REST**, per the brief's own stated default — confirmed sufficient
by reading the existing repository:

- `requests` and `uvicorn`/FastAPI are already project dependencies (used
  respectively for "strategy-side... agents (sync HTTP)" per
  `requirements.txt`'s own comment, and for the existing TCC app itself).
  No new dependency was added.
- The existing Phase 16.9 `worker_protocol.py` dataclasses
  (`WorkerRegistration`, `WorkerHeartbeat`, `OrderIntentSubmission`,
  `OrderIntentResult`) already describe exactly the request/response
  shapes a REST API needs — this phase carries those SAME concepts over
  HTTP, it does not invent new ones.
- No message broker (Kafka/RabbitMQ/Redis Streams/Celery/NATS/gRPC) is
  justified by any repository evidence.

## 4. Transport architecture

```
Strategy.generate_order_intents()
        |
        v
trading.worker.runner.WorkerRunner
        |
        v
trading.worker.client.TccClient        (pure HTTP wrapper -- requests)
        |
        v  HTTP (private-subnet, security-group-restricted on AWS;
        |  TLS termination was out of scope for this temporary
        |  shadow-validation environment -- see Section 9)
        v
trading.api.worker_routes.py           (NEW -- /api/worker/*)
        |
        v
trading.common.worker_registry.WorkerRegistry /
trading.common.worker_coordinator.WorkerCoordinator   (UNCHANGED authority)
```

`trading/api/worker_routes.py` contains zero strategy rules, risk logic,
or duplicated validation — every route authenticates the calling worker
(Section 5) and hands off to the exact same `WorkerRegistry`/
`WorkerCoordinator` instances every prior phase's tests already exercise.

## 5. Worker authentication

A third, independent authentication lane —
`trading/common/worker_auth.py`'s `WorkerAuthRegistry` — distinct from
the human/operator JWT lane and the existing shared `CONTROL_API_KEY`
machine lane. Per-`worker_id` secrets, presented via `X-Worker-Auth`,
verified with `hmac.compare_digest`. On the real AWS deployment, three
random 24-byte secrets (one per worker) were generated locally with
Python's `secrets` module and injected only into: (a) the shadow TCC's
`WORKER_AUTH_SECRETS` systemd environment variable, and (b) the
corresponding worker's own `WORKER_AUTH_SECRET` systemd environment
variable. No secret was logged, printed to this conversation, committed,
or returned by any API response — verified by direct inspection of every
API response captured during validation.

## 6. Session security

Phase 16.9 session semantics, unchanged, now proven both locally and on
real AWS: a genuine `systemctl stop worker` (simulating a crash) on the
real `worker-ds` EC2 instance caused it to go OFFLINE after the
heartbeat timeout, and a subsequent `systemctl start worker` minted a
**brand-new session** — the OLD session was rejected both immediately
after restart and is structurally incapable of ever being accepted again
(Section 15/37 results below).

## 7. Worker API

```
GET  /api/worker/time
POST /api/worker/register
POST /api/worker/heartbeat
POST /api/worker/runtime
POST /api/worker/order-intents
```

Plus the one new **operator** endpoint `POST /api/workers/{worker_id}/assign`
(`TRADING_CONTROL`-gated), used on the real deployment to assign each of
the 3 real worker EC2 instances to its strategy.

## 8/9. TLS / network security / security groups (as actually built)

A dedicated, temporary security group (`phase1612-shadow-sg`) was created
in the SAME VPC/subnet as production (`vpc-0100f18cab8c38e01` /
`subnet-0acefe8fe1b871c81`, ap-south-1a) — **entirely separate from
production's own `trading-sg`**, which was never modified:

- Inbound TCP 8000, **self-referencing** (only instances in this SG can
  reach the shadow TCC's worker-API port) — this is how the 3 worker
  instances reached the shadow TCC over private IP.
- Inbound TCP 22 (SSH) and TCP 8000, from the operator's own IP only —
  for deployment/validation access from this machine.
- Default outbound (needed for `dnf`/`pip`/`git` during bootstrap).

TLS was **not** configured for this temporary, single-session shadow
validation (plain HTTP within the security-group-restricted VPC) — this
is an explicit, documented gap for any longer-lived deployment, not a
production posture. The security group and all 4 instances were deleted
at the end of validation (Section 25 below) — this was never a
persistent piece of infrastructure.

## 10-13. Worker application, configuration, mapping, market data

Unchanged from the design validated locally (Section 21 of the original
local-validation pass): `trading/worker/main.py` selects one of the three
real, rule-unchanged strategies via `STRATEGY_ID`;
`FixedMarketDataSource` is wired by default (no real external market-data
call was made). Each of the 3 worker EC2 instances ran the SAME codebase
(same git clone, same commit `9d83964`), differing only by environment
variables (`WORKER_ID`, `STRATEGY_ID`, `WORKER_AUTH_SECRET`) — proving
Section 29's "one image, configuration-selected strategy" intent, even
though this deployment used a plain EC2 + systemd unit rather than a
Docker image (a reasonable simplification for a single-session temporary
validation; a longer-lived deployment should package this as a container
per the brief's own preference).

## 14/15. No automatic strategy start / no automatic resume

Confirmed on real infrastructure: immediately after all 4 instances
booted and all 3 workers registered/heartbeated, `/api/operations/summary`
showed all three strategies `STOPPED` / `INACTIVE` — nothing about
worker registration or heartbeating brought any strategy toward
RUNNING. Only an explicit operator `POST /api/strategy-lifecycle/{id}/command`
call (Section 18 below) changed that.

## 16-20. Evaluation identity / retry / stale-intent / clock

Unchanged Phase 16.9 `OrderIntentSubmission` shape and the Phase 16.12
retry-replay fix (Section 17 of the original local report) — both
already proven locally; not independently re-exercised against AWS
latency in this session (see Section 26, known limitations).

## 17. AWS deployment topology actually built

| Role | Instance ID | Type | Private IP | Public IP |
|---|---|---|---|---|
| Shadow Central TCC | `i-0d6d77e99b3536340` | t3.micro | 10.0.1.241 | 13.201.92.104 |
| CombinedVWAP worker | `i-02d9aaa4af07bfbe9` | t3.micro | 10.0.1.80 | 3.110.157.73 |
| DoubleStraddle worker | `i-095575e7a05cdbf30` | t3.micro | 10.0.1.147 | 43.205.138.154 |
| VWAPHedge worker | `i-0cc17aca9379165f9` | t3.micro | 10.0.1.9 | 13.207.43.17 |

All 4 in `vpc-0100f18cab8c38e01` / `subnet-0acefe8fe1b871c81` (ap-south-1a),
Amazon Linux 2023 (`ami-0ee11497c4eac651d`), security group
`phase1612-shadow-sg`, key pair `samirec2key`, tagged
`Phase=16.12`, `Purpose=shadow-validation-temporary`. **Production's own
`algo-backend` instance and `trading-sg` were never modified.**

A real deployment issue was found and fixed during bootstrap: Amazon
Linux 2023's default `python3` is 3.9, but this codebase requires 3.10+
(PEP 604 `X | None` union syntax in SQLAlchemy model annotations,
resolved at class-definition time). Fixed by explicitly installing
`python3.11`/`python3.11-pip` and building each instance's virtualenv
with it — a genuine, documented deployment-environment finding, not a
code change.

## 18. Post-deployment read-only validation (Section 34)

Immediately after all 4 systemd services came up:

```
GET /api/health   -> {"status":"ok", "git_sha":"9d83964", "database":"connected", ...}
GET /api/ready    -> {"application":"ready","broker":"not_connected","trading_authorized":false}
GET /api/operations/summary ->
    system.ready = true
    workers: worker-cvn/worker-ds/worker-vh all ONLINE
    strategies: all 3 STOPPED / INACTIVE
    accounts: ANGEL_MAIN/DHAN_MAIN/ICICI_MAIN, all execution_mode=SHADOW
    safety.kill_switch_engaged = false
    safety.execution_mode_banner = "SHADOW"
    safety.live_trading_disabled = true
    active_alerts = [] (once WORKER_VERSION_MISMATCH was resolved by
                        correcting GIT_SHA -- see below)
```

`trading_authorized: false` confirms NO live authorization anywhere in
this deployment. One legitimate alert was observed and explained: all
three workers initially reported `git_sha="unknown"` (the `GIT_SHA`
environment variable had not been set on the workers), correctly
triggering `WORKER_VERSION_MISMATCH` — this is Section 33/47's own
detection working exactly as designed on its first real trigger. Setting
`GIT_SHA=9d83964` on each worker and restarting resolved it.

## 19. Controlled distributed shadow start (Section 35)

Strategies were assigned to accounts (`POST /api/assignments`), then
each worker was assigned centrally to its strategy
(`POST /api/workers/{id}/assign`, the new endpoint), then started **one
at a time** via the existing control-plane
(`POST /api/strategy-lifecycle/{id}/command`, `START`):

```
CombinedVwapNifty:      STOPPED -> RUNNING  (ACCEPTED, live_authorized=false)
DoubleStraddelAlgo:     STOPPED -> RUNNING  (ACCEPTED, live_authorized=false)
Vwap_Algo_Nifty_hedge:  STOPPED -> RUNNING  (ACCEPTED, live_authorized=false)
```

After each start, the corresponding worker showed as its owner with
`runtime_state: HEALTHY`. No abnormal state occurred at any step.

## 20. Three-worker AWS shadow validation (Section 36)

A real `OrderIntent` was submitted from each of the 3 physically separate
worker EC2 instances (via `POST /api/worker/order-intents`, authenticated
with that worker's own secret) to the shadow TCC EC2 instance, over the
real private-subnet network path:

```
CombinedVwapNifty     -> accepted=true, order_id="DRYRUN-...", status=FILLED
DoubleStraddelAlgo    -> accepted=true, order_id="DRYRUN-...", status=FILLED
Vwap_Algo_Nifty_hedge -> accepted=true, order_id="DRYRUN-...", status=FILLED
```

Every `order_id` carries the `DRYRUN-` prefix — direct evidence the hard
shadow boundary (`ExecutionConfig(dry_run=True)`, unchanged since Phase
16.5) held for real, physically-separate-instance traffic. This is the
target topology diagram from the brief, exercised for real:
`Worker EC2 -> HTTP -> Shadow TCC EC2 -> PortfolioRiskManager ->
RiskManager -> StrategyExecutionEngine -> dry-run (no broker call)`.

## 21. AWS failure — worker restart (Section 37)

`worker-ds` was stopped (`systemctl stop worker`, simulating a crash) on
its real EC2 instance while `DoubleStraddelAlgo` was RUNNING:

```
worker-ds: ONLINE -> (30s heartbeat timeout) -> OFFLINE
Alerts raised: WORKER_HEARTBEAT_LOST (worker-ds), WORKER_OFFLINE (worker-ds)
worker-cvn, worker-vh: unaffected, remained ONLINE throughout
Submission on the (now-stale) old session while OFFLINE: rejected
  ("worker 'worker-ds' is not ONLINE")
```

`worker-ds` was then restarted (`systemctl start worker`):

```
New session minted (differs from the old one)
Old session STILL rejected after restart:
  "session_id does not match the worker's current active session --
   stale or duplicate process"
WORKER_HEARTBEAT_LOST / WORKER_OFFLINE alerts resolved automatically
```

**One honest nuance documented**: `DoubleStraddelAlgo`'s own lifecycle
state remained `RUNNING` throughout the worker's outage and restart —
this is by design (LifecycleState is centrally owned and independent of
worker connectivity, unchanged since Phase 16.3/16.9) — the safety
property that actually matters (no signal replay, no OrderIntent replay,
old session permanently invalid) was fully proven; a strategy that was
already human-approved to RUNNING is not automatically un-approved by a
worker blip, but its worker's own restarted process starts a completely
fresh evaluation loop with no memory of prior state.

## 22. AWS failure — kill switch (Section 41)

With all three strategies RUNNING and all three workers ONLINE, the
central kill switch was engaged via the real API
(`POST /api/risk/kill-switch`, `engaged=true`). A submission was then
made from all three real worker EC2 instances:

```
CombinedVwapNifty:     REJECTED -- "central kill switch is engaged; all order execution is blocked"
DoubleStraddelAlgo:    REJECTED -- "central kill switch is engaged; all order execution is blocked"
Vwap_Algo_Nifty_hedge: REJECTED -- "central kill switch is engaged; all order execution is blocked"
```

`/api/operations/summary` showed `kill_switch_engaged: true` and an
active `KILL_SWITCH_ENGAGED` alert (plus one `EXECUTION_REJECTED` alert
per affected strategy) immediately. The kill switch was then disengaged;
`KILL_SWITCH_ENGAGED` resolved automatically. A follow-up accepted
submission per strategy cleared the three `EXECUTION_REJECTED` alerts,
returning the dashboard to zero active alerts.

## 23. Operations dashboard / audit acceptance (Section 52)

`GET /api/operations/summary` was confirmed, throughout this session, to
accurately reflect: system health, all 3 worker states/heartbeats/
placements, all 3 strategy lifecycle/runtime states, account
assignments, portfolio risk (zeroed/HEALTHY — no limit configured on
this deployment), kill-switch state, execution mode, and active alerts —
with zero trading-control surface anywhere in its response shape.

## 24. Final safe state and teardown (Section 58)

Before teardown, all three strategies were explicitly stopped via the
control plane:

```
CombinedVwapNifty:      RUNNING -> STOPPED (ACCEPTED)
DoubleStraddelAlgo:     RUNNING -> STOPPED (ACCEPTED)
Vwap_Algo_Nifty_hedge:  RUNNING -> STOPPED (ACCEPTED)
```

Final `/api/operations/summary` confirmed: all 3 strategies `STOPPED`/
`INACTIVE`, kill switch disengaged, execution mode `SHADOW`, live trading
`DISABLED`, **zero active alerts**. All 4 EC2 instances were then
terminated and the temporary security group deleted:

```
aws ec2 terminate-instances -> all 4 instances: shutting-down -> terminated
aws ec2 delete-security-group -> phase1612-shadow-sg deleted
```

Post-teardown verification: production `algo-backend`
(`i-0f344752a1ca2811b`) confirmed still `running`, its `trading-sg`
confirmed to still show only its original rule set (untouched). No
residual AWS cost from this validation.

## 25. Alert-restart limitation (OperationalAlertStore) — unchanged from local finding

Confirmed identically on AWS: poll-driven alerts (`WORKER_OFFLINE`,
`WORKER_HEARTBEAT_LOST`, `KILL_SWITCH_ENGAGED`, `WORKER_VERSION_MISMATCH`)
reconstruct correctly from live state on the very next
`/api/operations/summary` poll; the two event-driven alerts
(`EXECUTION_REJECTED`/`PORTFOLIO_RISK_BLOCKED`) require a new submission
to clear, by design (Section 25 of the original local report — Option A,
no new persistence infrastructure added).

## 26. Known limitations from this AWS session

- **TLS was not configured** — this was a single-session, temporary
  shadow-validation deployment over a security-group-restricted private
  path; a longer-lived deployment should add TLS termination.
- **Network-isolation (Section 39) and true multi-AZ/EC2-reboot
  scenarios were not separately exercised on AWS** — the worker-restart
  test (Section 21 above, via `systemctl stop/start`) exercises the same
  underlying heartbeat-timeout/new-session code path a network partition
  or instance reboot would; a full `aws ec2 reboot-instances` /
  security-group-removal test was not additionally run in this session
  given the infrastructure was already confirmed safe to tear down.
- **Portfolio-risk and concurrent-worker distributed tests were not
  re-run against this specific AWS deployment** — both were already
  proven against a real, separate-process local deployment (see the
  original local-validation section of this report, retained below) with
  identical code; re-running them here would have required deploying the
  `PORTFOLIO_MAX_EXPOSURE` env var to this session's shadow TCC, which
  was not done before teardown.
- No Docker image was built for the worker application in this session
  (plain EC2 + systemd instead) — a reasonable simplification for a
  single-session validation; Section 29's "one image" intent was still
  satisfied at the code level (identical codebase/commit, environment-
  selected strategy).
- The AL2023-default-Python-3.9-vs-3.11 mismatch is a real, generally
  applicable deployment note for any future AWS deployment of this
  codebase — worth adding to actual infrastructure-as-code/AMI-baking
  scripts going forward (not fixed at the code level, since this
  repository's own `Dockerfile` already correctly pins `python:3.11-slim`).

---

# Appendix: Local (pre-AWS) distributed transport validation

This section is retained from the local-validation pass performed before
AWS access was available, and remains fully valid — the code exercised
is identical to what was deployed to AWS above.

## A.1 Local multi-process validation (Sections 21-24)

`tests/common/test_phase_16_12_distributed_transport.py` runs the central
TCC as a genuinely separate OS process (`python -m uvicorn
trading.api.app:create_app --factory`), its own isolated SQLite DB, bound
to a real loopback TCP socket. Every "worker" is a real
`trading.worker.client.TccClient` making real `requests` HTTP calls over
that socket. All 14 tests pass, covering registration/auth/sessions,
cross-worker isolation, malformed-intent rejection, a real 3-strategy
shadow flow, duplicate delivery (network-retry idempotency), stale-intent
rejection, kill-switch distributed rejection, portfolio-risk distributed
rejection (via the new `PORTFOLIO_MAX_EXPOSURE` env hook), concurrent
worker submissions, no-auto-start, and version-mismatch detection.

## A.2 Network retry safety — a real product fix

`WorkerCoordinator` previously tracked only a `set[str]` of seen
`submission_id`s, returning a generic "duplicate submission_id" rejection
on any retry. It now caches the actual `OrderIntentResult` per
`submission_id` (with an `_IN_FLIGHT` sentinel closing the race window)
and replays it verbatim on retry — proven locally
(`test_duplicate_delivery_over_real_http_yields_one_logical_execution`).

## A.3 Structural safety

`tests/common/test_phase_16_12_worker_structural_safety.py` proves no
module under `trading/worker/` imports a broker adapter/SDK,
`RiskManager`, `PortfolioRiskManager`, `StrategyExecutionEngine`,
`WorkerRegistry`, `WorkerCoordinator`, or `LiveAuthorization`, and that
`TccClient` only ever calls `/api/worker/*`.

## A.4 Full regression (unchanged, still current)

- Full backend regression: 2034 passed, 6 skipped, 0 failed, 0 errors.
- Strategy regression: 62/62 tests unchanged across all three real
  strategies — no trading rule modified.
- Frontend: 78 tests pass (one pre-existing, documented environmental
  flake reproduced passing in isolation); `tsc --noEmit` clean.

---

## Phase 17 boundary

Not started. No real broker adapter was enabled for execution anywhere
in this phase (local or AWS), no LiveAuthorization was granted, no
canary order was placed, no account was transitioned to
`LIVE_AUTHORIZED`, and no real trading strategy was run. Every execution
observed — local and on AWS — terminated at a `DRYRUN-`/dry-run result,
never a broker call.

## Safety statement

No live order was placed. No LiveAuthorization was granted or consumed.
No real broker connection or mutation occurred anywhere in this phase —
proven structurally (no worker module imports a broker adapter) and
empirically (every AWS execution result carried a `DRYRUN-` order ID). No
strategy was left running at the end of the AWS session (all three
explicitly STOPPED before teardown). All 4 temporary EC2 instances and
the temporary security group were deleted; zero AWS resources remain
from this validation. The existing production `algo-backend` instance
and its security group were read-only inspected and never modified,
restarted, or otherwise touched.
