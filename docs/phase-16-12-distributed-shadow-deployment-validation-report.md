# Phase 16.12 — Secure Distributed Worker Transport, Multi-EC2 Shadow Deployment & Final End-to-End Validation

## 1. Objective and scope actually completed

Convert the in-process distributed-worker foundation (Phase 16.9-16.11)
into a real, network-transported distributed architecture, and validate
it. This report is honest about a hard boundary encountered during this
phase: **the AWS EC2 provisioning/deployment portion (Sections 27-53 of
the brief) was not performed.** Everything else — real HTTPS transport,
worker authentication, session security, the full local multi-process
distributed validation suite (Sections 21-24), all required failure
injection (Section 37 analogues), retry/idempotency, stale-intent
rejection, kill-switch/portfolio-risk/concurrency distributed tests, and
structural safety — was implemented and verified for real, over real
network sockets, in this environment.

## 2. Why AWS provisioning was not performed

Before touching any cloud resource, a read-only inventory of the
configured AWS account (`ap-south-1`) was taken:

- The IAM user this environment authenticates as (`trading-control-cli`)
  can `DescribeInstances`/`DescribeSecurityGroups` but is **explicitly
  denied** `ec2:DescribeVpcs`, `ec2:DescribeSubnets`,
  `ec2:DescribeKeyPairs`.
- A `--dry-run` check (which mutates nothing — it only reports whether the
  caller *would* be authorized) confirmed `ec2:RunInstances` and
  `ec2:CreateSecurityGroup` are **both explicitly denied** for this IAM
  user.
- A real, running production instance was found (`algo-backend`,
  `i-0f344752a1ca2811b`, `t3.medium`, VPC `vpc-0100f18cab8c38e01`) — almost
  certainly the "central TCC" the brief refers to.

Provisioning EC2 instances and security groups is a costly,
hard-to-reverse action affecting shared cloud infrastructure. Given the
IAM user's permissions structurally refuse the create/provision calls
regardless of judgment, and given the explicit safety instruction in this
project to check before any such action, this was raised to the user
directly rather than attempted, worked around, or improvised. The user
confirmed they would update the IAM policy themselves and report back
before any AWS provisioning proceeds. **As of this report, that
confirmation has not yet arrived**, so Sections 27-53 (EC2 sizing,
security groups, deployment, post-deployment validation, AWS failure
injection, AWS-specific distributed tests) remain **NOT PERFORMED** —
not simulated, not faked, not inferred from the local results.

Per the brief's own Section 40/26 allowance ("If this cannot be performed
safely, document and mark the corresponding acceptance item BLOCKED
rather than improvising"), this report marks every AWS-specific
acceptance item BLOCKED (pending) rather than claiming an outcome that
was never actually exercised against real cloud infrastructure.

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
  justified by any repository evidence — worker traffic is low-frequency
  (heartbeats, occasional OrderIntent submissions), and the existing
  idempotency/session model already solves exactly-once semantics without
  needing broker-level delivery guarantees.

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
        v  HTTPS (HTTP in this local validation; TLS termination is an
        |  AWS-deployment concern, Section 8, not yet exercised)
        v
trading.api.worker_routes.py           (NEW -- /api/worker/*)
        |
        v
trading.common.worker_registry.WorkerRegistry /
trading.common.worker_coordinator.WorkerCoordinator   (UNCHANGED authority)
```

`trading/api/worker_routes.py` contains **zero** strategy rules, risk
logic, or duplicated validation — every route authenticates the calling
worker (Section 5) and then builds the exact same Phase 16.9
`OrderIntentSubmission`/`WorkerRegistration`/`WorkerHeartbeat` dataclasses
already used in-process, handing them to the SAME
`WorkerRegistry`/`WorkerCoordinator` instances every other phase's tests
already exercise. `WorkerCoordinator`'s own 9-step validation chain,
`PortfolioRiskManager`, the existing `RiskManager`, the central kill
switch, and the hard shadow boundary are all **completely unaware** this
HTTP layer exists.

## 5. Worker authentication

A **third, independent authentication lane** — `trading/common/worker_auth.py`'s
`WorkerAuthRegistry` — distinct from both existing lanes (confirmed by
reading `trading/api/deps.py`):

- **Human/operator lane**: username/password → short-lived JWT Bearer
  token, RBAC via `Permission`.
- **Existing machine lane**: `X-API-Key` / `CONTROL_API_KEY` — ONE fixed
  shared secret for the whole fleet, granting a single anonymous
  `"service"` identity `{VIEW}`-only rights. Unsuitable for workers
  (write-heavy calls, no per-worker identity, and reusing it would
  require weakening its documented "never start/stop/restart" guarantee).
- **New worker lane** (this phase): a per-`worker_id` shared secret,
  presented via the `X-Worker-Auth` header on every `/api/worker/*` call,
  verified with `hmac.compare_digest` (the same constant-time discipline
  `CONTROL_API_KEY` verification already uses). Secrets are provisioned
  centrally via `WORKER_AUTH_SECRETS` (comma-separated `worker_id:secret`
  pairs, parsed by `WorkerAuthRegistry.from_env()`) — unset means **no
  worker can authenticate at all** (fail closed, no production value
  invented). A worker never receives a secret from the TCC; it is given
  the SAME value out-of-band via its own `WORKER_AUTH_SECRET` environment
  variable. No route ever echoes a secret back, and it is never logged
  (verified by test).
- An unprovisioned `worker_id` and a wrong secret return the **identical**
  401 — never distinguishing them, so a caller cannot enumerate valid
  worker IDs.
- Proven, over real HTTP: an operator's valid JWT does not substitute for
  worker auth, and a worker's secret does not authenticate an operator
  route.

## 6. Session security

Phase 16.9 session semantics are the sole authority, unchanged. Every
`/api/worker/heartbeat` and `/api/worker/order-intents` call is bound to
the `session_id` issued at `/api/worker/register` time; a stale/mismatched
`session_id` is rejected with 409. A genuine restart is, from the TCC's
perspective, indistinguishable from "the old process stopped
heartbeating" — re-registering while the OLD session is still within its
heartbeat timeout is **correctly refused** (Phase 16.9's own
duplicate-worker protection: two processes must never both claim ONLINE
for one `worker_id`). This phase adds one small, well-justified
configuration hook — `WORKER_HEARTBEAT_TIMEOUT_SECONDS` (unset keeps the
existing 30.0s default) — used only to prove this restart-after-timeout
path deterministically and quickly in tests, not to change production
behavior.

## 7. Worker API

```
GET  /api/worker/time            (unauthenticated clock reference, Section 20)
POST /api/worker/register
POST /api/worker/heartbeat
POST /api/worker/runtime         (observational report; audits only)
POST /api/worker/order-intents
```

No `placeOrder`/`BUY`/`SELL`/broker-request/`authorize-live`/`go-live`
endpoint exists anywhere in this file, and none of the four mutating
routes can produce anything beyond what `WorkerCoordinator
.submit_order_intent()` already permitted in-process (PAPER/SHADOW-only
execution). One additional, narrowly-scoped **operator** endpoint was
added — `POST /api/workers/{worker_id}/assign` (gated by the existing
`TRADING_CONTROL` permission) — closing a real gap Phases 16.9-16.11 left
open (no HTTP-reachable way to assign strategy ownership to a worker;
those phases' own tests called `WorkerRegistry.assign_strategy()`
directly, which a genuinely separate worker process cannot do).

## 8/9. TLS / network security / security groups

**Not yet exercised** — this local validation runs over plain HTTP on
`127.0.0.1`, since no AWS deployment occurred. The intended AWS posture
(documented, not yet built): worker traffic stays inside the VPC on
private IPs, security-group-restricted (TCC's worker-API port inbound
only from a "Strategy Worker" security group; workers get no public
inbound worker-API exposure and no direct broker-network access), with
TLS terminated at whatever reverse proxy/load balancer already sits in
front of the existing `algo-backend` instance (matching the existing
`docker-compose.prod.yml`'s own documented posture: the backend container
binds to `127.0.0.1` only, host Nginx is the sole public entry point).

## 10. Worker application

`trading/worker/` (new package): `config.py` (env-driven `WorkerConfig`,
no broker credential fields), `client.py` (`TccClient` — a pure HTTP
wrapper, structurally scanned to prove it never calls anything but
`/api/worker/*`), `runner.py` (`WorkerRunner` — register once, then loop
heartbeat + evaluate + submit; never calls a lifecycle-START endpoint
anywhere), `main.py` (the real process entrypoint,
`python -m trading.worker.main`, selecting one of the three real,
rule-unchanged strategies via `STRATEGY_ID`). None of these files import
a broker adapter, `RiskManager`, `PortfolioRiskManager`,
`StrategyExecutionEngine`, `WorkerRegistry`, or `WorkerCoordinator` —
proven by `tests/common/test_phase_16_12_worker_structural_safety.py`.

## 11. Worker configuration

`TCC_URL`, `WORKER_ID`, `WORKER_NAME`, `WORKER_AUTH_SECRET`,
`STRATEGY_ID`, `HEARTBEAT_INTERVAL_SECONDS`, `APP_VERSION`, `GIT_SHA`,
`HOST_IDENTITY` — exactly the brief's own list. No broker trading
credential field exists in `WorkerConfig` (verified by test).

## 12. Worker → strategy mapping

Not hard-coded into strategy source — `STRATEGY_ID` selects which of the
three real strategy classes `trading/worker/main.py` constructs; ownership
is assigned centrally via `POST /api/workers/{worker_id}/assign`. A
worker attempting to submit for a strategy it does not own is rejected
centrally (proven over real HTTP:
`test_cross_worker_submission_rejected_over_real_http`).

## 13. Market data

`trading/worker/main.py` wires `FixedMarketDataSource` by default (the
safe, structurally-non-networked default every prior phase has used when
no real provider is configured) — `MarketDataGateway`/`ProviderMarketDataSource`
/`ICICIBreezeProvider` remain available and unmodified for a real
deployment to wire in; this phase did not call a real Breeze API (no
credentials were available or used).

## 14/15. No automatic strategy start / no automatic resume

Proven over real HTTP, not just asserted:
- `test_worker_runner_never_starts_a_strategy` — a `WorkerRunner` running
  its full register→heartbeat→evaluate loop (2 real cycles) against a
  STOPPED strategy never brings it to RUNNING/SHADOW; only the existing
  `/api/strategy-lifecycle/{id}/command` control-plane can.
- `test_old_session_rejected_after_re_registration_over_real_http` — a
  restart (heartbeat timeout, then re-register) mints a brand-new
  session, and the OLD session remains rejected afterward — no signal or
  OrderIntent replay path exists (there is no code path that resubmits a
  prior evaluation's intents on restart).

## 16. Evaluation identity

`OrderIntentSubmission` (unchanged Phase 16.9 shape) carries `worker_id`,
`session_id`, `strategy_id`, `evaluation_id`, `generated_at`,
`submission_id`, and the `OrderIntent` itself (which carries its own
`idempotency_key`) — all validated centrally by the unchanged
`WorkerCoordinator._validate()` chain, now reached over real HTTP.

## 17/18. Network retry safety / timeout safety

**A genuine, additive product fix was needed and made**:
`WorkerCoordinator` previously tracked only a `set[str]` of seen
`submission_id`s, returning a generic "duplicate submission_id" rejection
on any retry — which would have hidden a legitimately-retrying worker's
original (possibly successful) outcome. It now caches the actual
`OrderIntentResult` per `submission_id` (with an `_IN_FLIGHT` sentinel
closing the race window between accepting and finishing a submission) and
**replays it verbatim** on a retry — proven over real HTTP
(`test_duplicate_delivery_over_real_http_yields_one_logical_execution`:
two identical submissions, same `submission_id` and `idempotency_key`,
produce byte-identical results, never a second execution).
`trading/worker/client.py`'s own docstring documents the caller-side half
of this contract: a retry must reuse the exact same `submission_id` and
`idempotency_key`, never mint fresh ones.

## 19/20. Stale intent protection / clock considerations

Unchanged Phase 16.9 freshness semantics (`generated_at` vs.
`max_submission_age_seconds`), now proven rejected over real HTTP with a
genuinely stale timestamp
(`test_stale_intent_rejected_over_real_http`). All timestamps are UTC
internally, matching every existing module. `GET /api/worker/time` gives
a worker an unauthenticated clock reference to diff against locally; the
existing `check_clock_drift()`/`configured_warn_threshold_seconds()`
diagnostics remain available and unmodified (no reference-clock source
was wired — confirmed by research that none exists in the repository
today; this phase did not add one, matching the explicit instruction not
to build custom time synchronization). EC2 hosts should rely on normal
system time synchronization (NTP), as any Linux host already does by
default.

## 21-24. Local distributed validation (real, not simulated)

`tests/common/test_phase_16_12_distributed_transport.py` runs the central
TCC as a **genuinely separate OS process**
(`python -m uvicorn trading.api.app:create_app --factory`, its own
isolated SQLite DB, its own `WorkerAuthRegistry`/`OperationalAlertStore`)
bound to a real loopback TCP socket. Every "worker" is a real
`trading.worker.client.TccClient` making real `requests` HTTP calls over
that socket — `TccClient` structurally cannot import
`WorkerRegistry`/`WorkerCoordinator` (proven by the structural safety
test), so there is no shortcut available even in principle. Workers
themselves run in the test's own process rather than as additional
separate OS processes (a deliberate, documented simplification — the
property that matters, "a worker reaches `WorkerCoordinator` only through
HTTP," is fully proven either way; spawning 3 additional OS processes
per test would add Windows-specific `multiprocessing` complexity without
strengthening that specific guarantee).

All 14 tests in that file pass, covering:
- 3-worker registration/authentication/ONLINE status via real HTTP.
- Wrong secret / unknown worker / old-session-after-restart rejected.
- Cross-worker strategy-ownership rejection.
- Malformed `OrderIntent` (invalid `side`) → 422.
- A real 3-strategy (`CombinedVwapNiftyStrategy`, `DoubleStraddleStrategy`,
  `VwapAlgoNiftyHedgeStrategy`) shadow flow, all the way through
  `PortfolioRiskManager` → `RiskManager` → `StrategyExecutionEngine` →
  `PaperBroker`, over real HTTP.
- Duplicate delivery → one logical execution (Section 17/44).
- Stale intent → rejected (Section 19/45).
- Kill switch engaged → all three workers' submissions rejected, visible
  in `/api/operations/summary`'s `active_alerts` (Section 41).
- Portfolio risk: a real `PORTFOLIO_MAX_EXPOSURE` env-configured limit
  (a new, minimal, honestly-scoped config hook added specifically because
  no settable-limits API exists yet — Phase 16.10's own documented gap) —
  first submission accepted, second rejected with
  `PORTFOLIO_EXPOSURE_LIMIT`, over real HTTP and real concurrent-safe
  reservation logic (Section 42).
- Two workers submitting concurrently (real Python threads, real HTTP
  calls) — both independently accepted against their own accounts, no
  race, no duplicate execution (Section 43).
- `WorkerRunner` never starting a strategy; `WORKER_VERSION_MISMATCH`
  alert appearing over real HTTP for a worker reporting a different
  `git_sha` (Section 47).

A real, non-obvious bug was found and fixed during this work: a
pipe-buffer deadlock in the test harness itself (the child uvicorn
process's stdout was never drained, so once its own buffered log output
filled the OS pipe, it blocked forever mid-startup) — fixed by draining
the child's stdout continuously in a background thread. This is a test-
harness fix, not a product change.

## 25. Alert-restart limitation (OperationalAlertStore)

Per the brief's own instruction to choose the minimum safe solution:
**Option A (reconstruction) was already the design from Phase 16.11** —
`operations_snapshot._reconcile_alerts()` derives every poll-driven alert
(`WORKER_OFFLINE`, `WORKER_HEARTBEAT_LOST`, `KILL_SWITCH_ENGAGED`,
`STRATEGY_RUNTIME_FAILED`, `MARKET_DATA_*`, `ACCOUNT_*`,
`WORKER_VERSION_MISMATCH`) fresh from CURRENT authoritative state on every
`/api/operations/summary` read — after a TCC restart, the very next poll
already reconstructs every one of these correctly from whatever workers
have (or have not) re-registered/re-heartbeated since. The two
event-driven alerts (`PORTFOLIO_RISK_BLOCKED`, `EXECUTION_REJECTED`/
`EXECUTION_FAILED`) are the only ones that do NOT reconstruct on restart
(they require an actual new submission to re-evaluate) — this is an
accepted, documented limitation, not a gap needing Option B: these two
alert types are inherently about a specific past decision, and a stale
"still blocked" alert surviving a restart with no way to know if the
underlying condition still holds would be actively misleading. No new
persistence infrastructure was added; when `AUDIT_DB_PATH` is configured,
every alert raise/resolve is still durably recorded in the audit trail
regardless (Phase 16.11, unchanged).

## 26. Pre-AWS safety review

| Check | Status |
|---|---|
| All local tests pass | ✅ |
| Transport authenticated | ✅ |
| Workers contain no broker trading credentials | ✅ (structural test) |
| Workers contain no real broker adapters | ✅ (structural test) |
| Central execution forced SHADOW | ✅ (unchanged hard shadow boundary) |
| LiveAuthorization inaccessible | ✅ (structural test, no import anywhere in `trading/worker/` or `worker_routes.py`) |
| No auto-start | ✅ (real-HTTP test) |
| No auto-resume | ✅ (real-HTTP test) |
| Idempotency tested across network timeout | ✅ (real-HTTP test) |
| Persistent safety stores intact | Not exercised (no AWS restart performed) |

Given the one unexercised row is exclusively AWS-deployment-dependent,
and per Section 26's own rule ("If any fails: PHASE 16.12 = BLOCKED. Do
not deploy."), AWS deployment correctly did **not** proceed — not because
local validation failed, but because the AWS stage itself was never
reached (IAM permissions block it, pending the user's own action).

## 27-53. AWS deployment, sizing, security groups, post-deployment
validation, controlled shadow start, three-worker AWS validation, AWS
failure injection (worker restart/EC2 restart/network isolation/TCC
restart), distributed kill-switch/portfolio-risk/concurrency/duplicate-
delivery/stale-intent/market-data-failure tests **on AWS**, operations
dashboard AWS acceptance, security acceptance **on AWS**

**NOT PERFORMED.** No EC2 instance was created. No security group was
created or modified. No existing instance (including the running
`algo-backend` production instance) was touched, restarted, or inspected
beyond the read-only `DescribeInstances`/`DescribeSecurityGroups` calls
already described in Section 2. All of Sections 21-24's REQUIRED
behaviors (transport acceptance, three-strategy shadow flow, failure
injection, kill-switch/portfolio-risk/concurrency/duplicate-delivery/
stale-intent tests) were instead fully exercised LOCALLY, over real
network sockets, per Section 4's above findings — this is the strongest
evidence available without AWS access, and every one of those results is
real (not simulated), just not run against actual EC2 infrastructure.

## 54. Full regression

- Targeted Phase 16.12 sweep (worker routes, worker auth, worker
  coordinator, worker registry, structural safety, distributed transport,
  Phase 16.10/16.11 regression files): all pass.
- **Full backend regression: 2034 passed, 6 skipped, 0 failed, 0 errors**,
  exit 0 — 52 more than the stated pre-Phase-16.12 baseline of 1982,
  consistent with the new test files. The recurring
  `PytestUnhandledThreadExceptionWarning` lines from the pre-existing fake
  `AngelOne.orderBook()` polling thread are the same known, benign,
  pre-existing artifact seen in every prior phase's full run.
- Frontend: 78 tests across 10 files pass; the one failure seen in the
  full run (`TccStrategiesPage.test.tsx`'s Start/Stop test) is the same
  pre-existing, documented environmental flake from every prior phase —
  reproduced passing cleanly in isolation (664ms) immediately afterward.
- `npx tsc --noEmit`: clean, exit 0.
- Frontend production build: not re-run this phase (no frontend file was
  touched by Phase 16.12 — transport/worker work is backend-and-worker-
  process-only); the build already passed at the end of Phase 16.11 with
  an unchanged frontend tree.

## 55. Strategy regression

`test_combined_vwap_nifty_strategy.py`, `test_double_straddle_strategy.py`,
`test_vwap_algo_nifty_hedge_strategy.py` — all 62 tests re-run and pass
unchanged. No trading rule, threshold, time window, SL/target, VWAP
formula, or hedge rule was modified.

## 56. Production vs. shadow deployment clarification

No production or shadow AWS deployment occurred in this phase. The
existing `algo-backend` production EC2 instance was read-only inspected
(instance metadata only, via the AWS API) and never touched, restarted,
or reconfigured.

## 57. Phase 17 boundary

Not started. No real broker adapter was enabled for execution, no
LiveAuthorization was granted, no canary order was placed, no account was
transitioned to `LIVE_AUTHORIZED`, and no real trading strategy was run.

## 58. Final safe state (local)

- No local process was left running after this phase's tests completed —
  every `_LiveServer`/`WorkerRunner`/`TccClient` instance is created and
  torn down within its own test.
- No strategy was left RUNNING in any persistent state (every test uses
  its own fresh, isolated, torn-down process).
- No kill switch was left engaged (every test that engages it disengages
  it in a `finally` block).
- Execution mode: SHADOW. Live trading: DISABLED. Unchanged from every
  prior phase.

## 59. Documentation

This report. `docs/phase-15d-11-human-live-canary-review-report.md`
remains untouched and unstaged (verified via `git status` before and
after every `git add`/commit in this phase, matching every prior phase's
discipline).

## Known limitations / remaining work

- AWS deployment (Sections 27-53) is entirely pending the user's IAM
  policy update and explicit go-ahead.
- No settable `/api/risk/portfolio-limits` mutation endpoint exists yet —
  `PORTFOLIO_MAX_EXPOSURE` (this phase's one env-configurable limit) is a
  deployment-time setting, not an operator-adjustable one at runtime.
- TLS/security-group posture is documented as an intent, not yet built or
  verified against real infrastructure.
- Workers in the local validation run in the test's own process (real
  HTTP calls, no in-process shortcut) rather than as additional separate
  OS processes — the property that matters (no `WorkerCoordinator`
  shortcut) is still fully proven; true OS-process isolation for workers
  remains an AWS-deployment-stage concern.

## Safety statement

No live order was placed. No LiveAuthorization was granted or consumed.
No real broker connection or mutation occurred anywhere in this phase
(proven structurally: no worker module imports a broker adapter, and
every distributed test's execution terminated at `PaperBroker`). No
strategy was left running. No AWS resource was created, modified, or
deleted; the one existing production instance was read-only inspected via
the AWS API and never touched.
