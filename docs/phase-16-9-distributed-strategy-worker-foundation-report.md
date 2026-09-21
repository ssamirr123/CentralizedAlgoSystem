# Phase 16.9 — Distributed Strategy Worker Foundation

## 1. Objective

Introduce the architectural foundation for eventually running strategies on
independent worker processes/servers, while the Trading Control Center (TCC)
remains the sole, central authority for safety, risk, execution, audit, and
broker routing. This phase is **architecture/foundation only**: no real
worker process, no real EC2 instance, no LiveAuthorization, no live order,
no real broker mutation, and no automatic strategy start were introduced or
performed.

## 2. Discovered architecture (before changes)

- `trading/common/strategy_runtime.py` (Phase 16.5-16.8): `StrategyRuntime`
  is the only seam that turns a `Strategy`'s `OrderIntent`s into a call
  against `StrategyExecutionEngine.execute()`, behind a hard shadow
  boundary (`dry_run=True`, `_assert_simulated_broker()`, shadow-broker
  wiring in `execution_state.py`).
- `trading/common/strategy_assignment.py` (Phase 16.2): `StrategyAssignment`
  is the existing, single source of truth for which account a strategy is
  authorized against. Phase 16.9 does not replace or duplicate this — a
  worker is an *execution placement* concept, layered on top of, not
  instead of, this existing account assignment.
- `trading/common/idempotency_store.py`: the only idempotency mechanism in
  the system; reused unchanged for worker-submitted intents.
- No existing worker, agent, RPC, or queue infrastructure existed anywhere
  in the repository. No evidence of Kafka/RabbitMQ/Redis/Celery use or
  planning was found, so none was introduced (per the explicit instruction
  not to add infrastructure dependencies without repository evidence).

## 3. Files added

- `trading/common/worker_identity.py` — `WorkerStatus` enum
  (REGISTERED/ONLINE/OFFLINE/DEGRADED/STOPPED) and the `WorkerInfo`
  dataclass describing a worker's identity and placement. Contains no
  credential field, no broker field, and no authorization field.
- `trading/common/worker_registry.py` — `WorkerRegistry`: central,
  in-process, thread-safe bookkeeping of known workers and which worker
  (if any) owns which strategy. Owns heartbeat-timeout-based staleness
  detection (`DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 30.0`, injectable clock
  for tests). Raises `UnknownWorkerError`, `DuplicateWorkerSessionError`,
  `StrategyAlreadyOwnedError` for the corresponding safety violations.
- `trading/common/worker_protocol.py` — transport-independent, pure,
  serializable dataclasses describing every message a worker can send or
  receive: `WorkerRegistration`, `WorkerHeartbeat`, `StrategyStartCommand`,
  `StrategyStopCommand`, `StrategyEvaluateCommand`, `StrategyRuntimeUpdate`,
  `OrderIntentSubmission`, `OrderIntentResult`. No transport (HTTP/queue/
  socket) is implemented in this phase — these are in-process Python
  dataclasses only, deliberately so a real transport can be added later
  without changing the validation/authority logic.
- `trading/common/worker_coordinator.py` — `WorkerCoordinator`: the single
  central authority that validates and executes a worker-submitted
  `OrderIntent`. It never constructs a second execution engine or broker;
  its only path to execution is `StrategyRuntime.execute_worker_intent()`.
- `tests/common/test_worker_registry.py`,
  `tests/common/test_worker_protocol.py`,
  `tests/common/test_worker_coordinator.py`,
  `tests/common/test_phase_16_9_worker_structural_safety.py` — new test
  suites described in Section 8.

## 4. Files modified

- `trading/common/strategy_runtime.py` — added two public methods:
  `is_strategy_active(strategy_id)` (reads existing `StrategyStatus`, adds
  no new activity vocabulary) and `execute_worker_intent(intent, *,
  owning_strategy_id)` (re-validates `intent.strategy_id` against the
  owning strategy, re-asserts the existing hard shadow boundary, then
  delegates to the same `StrategyExecutionEngine.execute()` every other
  phase has used — no new engine, no new broker path).
- `trading/api/execution_state.py` — wires an empty `WorkerRegistry` and a
  `WorkerCoordinator` into `ExecutionState`. Nothing registers, starts, or
  heartbeats a worker at startup; `worker_registry.list_workers() == []`
  immediately after construction (verified by test).
- `trading/api/execution_routes.py` — added three read-only endpoints
  (`GET /api/workers`, `GET /api/workers/{worker_id}`,
  `GET /api/workers/{worker_id}/strategies`), all under the existing VIEW
  permission, no new mutation endpoint. Extended `StrategyLifecycleOut`
  with `worker_id`, `worker_status`, `worker_last_heartbeat_at` so the
  existing lifecycle view can show worker placement without introducing a
  sixth status vocabulary.
- `frontend/src/api/types.ts`, `frontend/src/api/endpoints.ts` — added the
  `Worker` type and `listWorkers()` client, and extended
  `StrategyLifecycle` with the three worker fields above.
- `frontend/src/pages/TccLifecyclePage.tsx` — added a read-only "Worker"
  column showing worker id and status. No new control (start/stop/assign
  worker) was added to the UI.
- `tests/api/test_execution_routes.py` — added a "WORKERS (read-only)" test
  section and extended the pre-existing `ExecutionState` structural test.
- `frontend/src/pages/TccLifecyclePage.test.tsx` — extended fixtures and
  added a worker-placement display test.

## 5. WorkerRegistry design

`WorkerRegistry` is purely central, in-process state — it is not a
network service in this phase. A worker "registers" by calling
`register_worker(worker_id=..., name=..., version=..., git_sha=...,
host_identity=...)`, which:

- Refuses (raises `DuplicateWorkerSessionError`) if a worker with the same
  `worker_id` is currently `ONLINE` — preventing two processes from
  silently sharing one identity.
- Otherwise creates a fresh `WorkerInfo` with a brand-new `session_id`
  (a `uuid4`), status `ONLINE`, and `last_heartbeat_at` set from the
  registry's own (injectable) clock — never the caller's claimed time.
- Preserves any strategy assignments the `worker_id` held from a prior
  session (see Section 7) rather than wiping them, since a restart is not
  a re-provisioning event.

Heartbeats (`record_heartbeat(worker_id, session_id=..., ...)`) must
present the exact `session_id` issued at registration; a mismatch raises
`DuplicateWorkerSessionError`, which is the mechanism preventing a stale or
duplicated worker process from being confused with the current one
(Section 10, duplicate worker protection).

Staleness is recomputed lazily (on `get_worker`, `list_workers`, and
`record_heartbeat`) by comparing `now - last_heartbeat_at` against
`heartbeat_timeout_seconds` (default 30s); a worker that misses its
heartbeat window is marked `OFFLINE` automatically — the registry never
requires an explicit disconnect signal to notice a dead worker.

## 6. Worker protocol

`worker_protocol.py` defines the full message vocabulary a worker and the
TCC would exchange, as plain, transport-independent dataclasses:

- Worker → TCC: `WorkerRegistration`, `WorkerHeartbeat`,
  `OrderIntentSubmission` (carries the worker's `session_id`, the owning
  `strategy_id`, and the `OrderIntent` itself, by reference — no copying,
  no re-serialization of broker-relevant fields).
- TCC → Worker: `StrategyStartCommand`, `StrategyStopCommand`,
  `StrategyEvaluateCommand`, `OrderIntentResult`.
- TCC-internal telemetry: `StrategyRuntimeUpdate`.

No field on any of these dataclasses names a credential, token, secret, or
broker adapter (proven by `test_worker_protocol.py`'s source/field scan).
No transport (HTTP endpoint, message queue, socket server) is implemented
in this phase — wiring these dataclasses to a real network boundary is
explicitly out of scope and left to a later phase, since no repository
evidence required one yet.

## 7. Strategy-to-worker assignment

`WorkerRegistry.assign_strategy(strategy_id, worker_id)` extends, and does
not replace, Phase 16.2's `StrategyAssignment` (account authorization).
The two are deliberately separate facts:

- `StrategyAssignment` — which **account** a strategy is authorized to
  trade through (Phase 16.2, unchanged).
- `WorkerRegistry` strategy ownership — which **worker process** is
  currently responsible for evaluating that strategy (Phase 16.9, new).

Assigning a strategy to a worker that is already owned by a **different,
currently-`ONLINE`** worker raises `StrategyAlreadyOwnedError` — a strategy
can never be silently double-assigned while its current owner is live.
Reassignment to a new worker is only permitted once the previous owner is
no longer `ONLINE` (e.g., after a disconnect), and is never automatic
(Section 9).

## 8. Central OrderIntent validation boundary

`WorkerCoordinator.submit_order_intent(submission: OrderIntentSubmission)`
is the **only** path by which a worker-originated `OrderIntent` can reach
execution. Before touching the execution engine, it validates, in order:

1. Duplicate `submission_id` (idempotent replay protection at the
   protocol layer, distinct from order-level idempotency).
2. Malformed or missing `generated_at`.
3. Stale submission (`generated_at` older than
   `DEFAULT_MAX_SUBMISSION_AGE_SECONDS = 30.0`).
4. Unknown `worker_id`.
5. Worker not currently `ONLINE`.
6. `session_id` mismatch against the worker's current session.
7. Unknown `strategy_id`.
8. Strategy not owned by the submitting worker (wrong worker/strategy
   pairing).
9. Strategy not currently active (`StrategyRuntime.is_strategy_active()`).
10. `intent.strategy_id` mismatch against the submission's own
    `strategy_id`.
11. Missing account assignment for the strategy (Phase 16.2's gate,
    re-checked here, not duplicated).
12. Missing `idempotency_key` on the intent.
13. Non-positive quantity or empty symbol on the intent.

Any failure returns `OrderIntentResult(accepted=False, reason=...)` — it
never raises out to the caller and never partially executes. Only a
submission that survives all 13 checks is handed to
`StrategyRuntime.execute_worker_intent()`, which re-applies the *existing*
hard shadow boundary and then the *existing* kill-switch → account
authorization → RiskManager → mode gate → broker resolution →
LiveAuthorization → idempotency → broker-call pipeline unchanged from every
prior phase. The worker never sees or supplies the account being routed to
— the account used is always the one from the strategy's own
`StrategyAssignment`, never a worker-supplied value (proven by
`test_worker_coordinator.py::test_worker_supplied_account_id_is_never_trusted_for_routing`
or equivalent — the worker-side `account_id`, if any, is not part of the
protocol's routing decision at all).

## 9. Heartbeat, disconnect, and restart safety

- **Heartbeat**: a worker must periodically call `record_heartbeat` with
  its issued `session_id`; the registry recomputes staleness against
  `heartbeat_timeout_seconds` on every read.
- **Disconnect**: a worker that stops heartbeating is marked `OFFLINE`
  automatically once the timeout elapses — no code path treats a missed
  heartbeat as "still trading."
- **Restart**: `register_worker()` called again with the same `worker_id`
  after the prior session went `OFFLINE` succeeds and issues a **brand-new
  `session_id`**. Critically, restart is proven to imply none of:
  resuming trading, restarting the strategy's lifecycle state, replaying
  the prior evaluation's signal, or replaying a prior `OrderIntent`. The
  registry preserves the strategy **assignment** (so the operator does not
  have to manually re-point the strategy at the same worker) but assigns
  no runtime activity — `StrategyRuntime`'s own lifecycle
  (`STOPPED`/`READY`/`RUNNING`/...) is untouched by a worker restart, and a
  stopped strategy stays stopped. No code path calls `assign_strategy` or
  a start command automatically after a restart or after a strategy's
  owning worker goes offline; both require an explicit, human-driven call.
  This is directly asserted by tests (Section 10).
- **Old-session submission rejection**: an `OrderIntentSubmission` bearing
  a worker's **prior** (now-invalid) `session_id` is rejected with
  `DuplicateWorkerSessionError`/validation failure, which is what prevents
  a resumed or duplicated old worker process from replaying a stale
  signal after a restart.

## 10. Cross-worker isolation

The multi-worker end-to-end test in `test_worker_coordinator.py` registers
three independent `WorkerInfo`s (Worker A/B/C), assigns
`CombinedVwapNiftyStrategy`, `DoubleStraddleStrategy`, and
`VwapAlgoNiftyHedgeStrategy` to them respectively (the same three real
strategies integrated in Phases 16.7/16.8, unmodified), and proves:

- Worker A cannot submit an `OrderIntent` for Worker B's or C's strategy
  (rejected as wrong worker/strategy ownership).
- Each worker's submissions execute independently through the same shadow
  `StrategyExecutionEngine`, with no shared mutable state leaking between
  strategies beyond what already existed (e.g., the shared kill switch and
  RiskManager, which are *intentionally* central and shared).
- A central kill-switch engagement blocks all three workers' submissions
  simultaneously (`test_kill_switch_blocks_all_three_workers_simultaneously`),
  proving the kill switch cannot be bypassed or scoped-around by any
  worker.
- A central `RiskManager` limit breach rejects an over-limit submission
  from any worker the same way it would reject a same-process call — no
  worker-local risk override exists.
- Idempotency is proven across workers: a same-worker retry of an
  identical `idempotency_key` returns the same underlying order/result
  rather than double-executing, and a *different* strategy attempting to
  reuse another strategy's idempotency key is rejected rather than
  silently succeeding.

## 11. Structural safety guarantees (enforced by tests)

- No file under `trading/common/worker_*.py` imports a broker adapter/SDK
  module or class.
- No file under `trading/common/worker_*.py` references or consumes
  `LiveAuthorization`.
- `WorkerCoordinator` never constructs a second `StrategyExecutionEngine`
  or broker instance; its only route to execution is the single
  `execute_worker_intent` seam, never a direct `.execute(` call.
- `ExecutionState`/app startup never auto-registers or auto-starts a
  worker; `worker_registry.list_workers() == []` immediately after
  construction, and every strategy remains in its default disabled
  lifecycle state.
- `WorkerHeartbeat` and other protocol messages carry no
  `live_authorized`/`authorization_state`-shaped field — a worker cannot
  claim or imply live authorization through the protocol.

## 12. Required-failure-test coverage

Implemented and passing (in `test_worker_coordinator.py` and
`test_worker_registry.py`): unknown worker, offline worker, duplicate
worker session (both at registration and at heartbeat), strategy assigned
to a different currently-online worker, worker submitting for a strategy
it does not own, strategy not active, `intent.strategy_id` mismatch,
missing idempotency key, non-positive quantity, empty symbol, missing
account assignment, duplicate `submission_id` replay, stale submission
(`generated_at` too old), malformed `generated_at`, kill switch blocking a
worker submission, RiskManager rejecting an over-limit worker submission,
same-worker idempotent retry, cross-strategy idempotency-key reuse
rejection, cross-worker strategy-ownership violation, worker-restart
preserving assignment without resuming activity, reassignment after owner
goes offline, concurrent submissions from different workers (threading),
and the three-real-strategy multi-worker end-to-end shadow execution flow.

## 13. Test results

- **Targeted Phase 16.9 suite** (`test_worker_registry.py`,
  `test_worker_protocol.py`, `test_worker_coordinator.py`,
  `test_phase_16_9_worker_structural_safety.py`,
  `test_execution_routes.py`, `test_strategy_runtime.py`): all passed,
  `EXIT=0`.
- **Full backend regression** (`python -m pytest -q`): **1893 passed, 6
  skipped, 0 failed, 0 errors**, `PYTEST_EXIT=0`. This is 54 more passing
  tests than the stated pre-Phase-16.9 baseline of 1839 passed, consistent
  with the new worker test files added in this phase; skip count (6)
  matches the baseline unchanged. The ~20
  `PytestUnhandledThreadExceptionWarning` lines from the pre-existing fake
  `AngelOne.orderBook()` polling thread are the same known, pre-existing,
  benign artifact observed in every prior phase's full run — unrelated to
  this phase's changes.
- **Frontend test suite** (`npm test -- --run`): 9 test files, 67 tests,
  all passed, exit 0.
- **`npx tsc --noEmit`**: exit 0, no type errors.
- **`npm run build`**: exit 0, production build succeeded.

## 14. Limitations / explicitly out of scope for this phase

- No real network transport (HTTP endpoint, message queue, or socket
  server) was implemented for the worker protocol; it exists today as
  in-process dataclasses and an in-process coordinator/registry. Adding a
  real transport is Phase 16.10+ work.
- No real worker process (separate Python process, container, or EC2
  instance) was created; "Worker A/B/C" in the tests are in-process test
  doubles registered directly against `WorkerRegistry`.
- No mutation endpoint (`POST /api/workers/...`) was added; all new API
  surface is read-only (`GET`).
- No UI control to assign a strategy to a worker or to start/stop a worker
  was added; the UI change is display-only (worker id/status column).
- Reassignment of a strategy to a new worker after its prior owner goes
  offline requires an explicit `assign_strategy` call by an operator/
  future admin tool — no automatic reassignment or restart logic exists.

## 15. Remaining Phase 16.10+ work (not started)

- A real transport binding for `worker_protocol.py` (e.g., an HTTP
  endpoint under the existing FastAPI app for a worker to call, still
  going through `WorkerCoordinator`).
- An actual separate worker process/entrypoint that imports only
  `trading.common.worker_protocol`/strategy logic and never broker code,
  proving the "worker must not import broker SDKs" rule at the process
  boundary, not only the source-scan boundary.
- Admin-facing mutation endpoints and UI controls for worker/strategy
  assignment, gated by the same permission model as existing mutations.
- Real multi-machine (not just multi-in-process) simulation.

## Safety statement

No live orders were placed. No LiveAuthorization was granted or consumed.
No real broker connection or mutation occurred. No strategy was
automatically started. Nothing was deployed to production. All new
execution paths reuse the existing hard shadow boundary and existing
kill-switch/RiskManager/idempotency mechanisms; no parallel or bypassable
execution path was introduced.
