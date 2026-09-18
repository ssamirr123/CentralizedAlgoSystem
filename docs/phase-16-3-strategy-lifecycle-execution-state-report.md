# Phase 16.3 — Strategy Lifecycle & Execution State

Status: **PASS** (read-only lifecycle visibility; no new mutation path, no
live authorization, no strategy auto-started, no broker mutation).

## 1. Objective

Add a controlled lifecycle/state model (STOPPED / READY / RUNNING / PAUSED /
ERROR) for assigned strategies, sitting between Phase 16.2's
Strategy → Assignment → Account model and the existing execution safety
gates, while keeping three facts strictly separate and never conflated:

```
STRATEGY LIFECYCLE STATE  !=  LIVE AUTHORIZATION  !=  ORDER EXECUTION
```

## 2. Existing lifecycle functionality discovered

A full read of Phase 10 (`trading/common/strategy.py`), Phase 11
(`trading/api/execution_routes.py`/`execution_state.py`), Phase 15B
(`trading/common/trading_account.py`), and Phase 16.2
(`trading/common/assignment_readiness.py`) found:

- **`StrategyStatus`** (Phase 10) already IS a lifecycle vocabulary:
  `DISABLED / ENABLED / STARTING / RUNNING / STOPPED / ERROR / SHADOW`,
  with a full, tested state machine in `BaseStrategy`
  (`enable()/disable()/start()/stop()/mark_error()`), already wired to
  `POST /api/strategies/{id}/start|stop`. This is a **strategy
  configuration/activity** state machine, entirely decoupled from any
  account or broker concept.
- **`AccountAuthorizationState`** (Phase 15B) — `DISABLED / READ_ONLY /
  CANARY_READY / LIVE_AUTHORIZED / KILLED` — is a completely separate
  **account trust** state machine, already hard-gated inside
  `StrategyExecutionEngine.execute()` via `_check_authorization_state()`.
- **`AssignmentReadiness`** (Phase 16.2) already answers "would this
  assignment currently pass execute()'s gates?" by reusing both of the
  above plus kill-switch/broker-availability state.
- No existing "lifecycle" concept combines strategy activity + assignment
  + account authorization into one observable view. That is the one real
  gap this phase closes — as a **read-only projection**, per the brief's
  own "if lifecycle state can safely be derived from existing persisted
  state, prefer deriving it" instruction.
- No `pause()`/`resume()` capability exists anywhere on the `Strategy`
  interface. Per the brief's explicit instruction not to implement
  pause/resume if doing so would require an uncontrolled execution path,
  none was added.
- `POST /api/strategies/{id}/start` already enables-then-starts in one
  call (Phase 11) — there is no standalone "enable only" HTTP action, so
  the ENABLED (`READY`) state is reachable at the Python/`StrategyRegistry`
  level but not independently via the exposed API today (Section 14).

## 3. Architecture

```
Trading Control Center
        |
        v
Strategy Registry (Phase 10, unchanged)
        |
        v
Strategy Assignment (Phase 1/7, unchanged)
        |
        v
Strategy Lifecycle  <-- NEW: trading/common/strategy_lifecycle.py
        |               (pure projection -- no persistence, no mutation)
        v
Execution Engine (Phase 1-9, unchanged)
        |
        v
Existing Safety Gates (Kill Switch, Account Authorization, RiskManager,
Mode Gate, LiveAuthorization, Idempotency, Broker Response Validation) --
byte-for-byte unchanged, not reordered, not touched.
```

`trading/common/strategy_lifecycle.py` sits ALONGSIDE the execution path,
never inside it: it reads `StrategyRegistry`, `StrategyAssignment`,
`BrokerManager`, and `CentralKillSwitch`, and reuses Phase 16.2's
`check_assignment_readiness()` rather than re-implementing any of its
logic. It is never imported by `trading/common/execution.py`, and it never
imports a broker adapter or anything under the live-authorization/canary
modules.

## 4. Lifecycle state model

`LifecycleState`: `STOPPED / READY / RUNNING / PAUSED / ERROR` — a THIRD,
deliberately separate vocabulary from `StrategyStatus` and
`AccountAuthorizationState` (never merged with either).

| LifecycleState | Meaning | Derived from |
|---|---|---|
| `STOPPED` | Not executing; the lifecycle manager will not generate orders. | `StrategyStatus` in `{DISABLED, STOPPED}`, or `ENABLED` without a sound assignment |
| `READY` | Strategy is enabled and its assignment is structurally sound (account exists, enabled, not killed, broker available, authorization_state permits the assigned execution_mode). **Not** live-authorized; **not** execution-permitted. | `StrategyStatus.ENABLED` + assignment "sound" (Section 7) |
| `RUNNING` | The in-memory strategy process/loop is active (`RUNNING`/`SHADOW`/transient `STARTING`) per the existing Phase 10 status. | `StrategyStatus` active statuses |
| `PAUSED` | Reserved vocabulary only — no code path produces it this phase (Section 2). | never produced |
| `ERROR` | The strategy's own status is `ERROR`, **or** an anomalous state was observed (Section 6). | `StrategyStatus.ERROR`, or the anomaly case |

`StrategyLifecycleView` fields: `strategy_id, assignment_id, account_id,
strategy_status, lifecycle_state, account_authorization_state,
live_authorized, execution_active, last_transition_at, last_heartbeat_at,
last_error, assignment_exists, blocking_reasons`. No new persisted table —
every field is computed fresh on each read from state that already exists.

## 5. State transition rules

**No transition function was added** — `LifecycleState` has no `transition()`
method and no persisted "current state" to transition from. It is
recomputed from scratch on every read. This was a deliberate design choice,
not an oversight: the brief itself warns "First inspect existing lifecycle
semantics... do not blindly implement this exact state graph," and the only
real, already-tested transitions in this codebase are `StrategyStatus`'s own
(`BaseStrategy.enable/disable/start/stop`, already validated by
`InvalidStrategyStateError` — e.g. `STOPPED -> RUNNING` is already rejected
today, since `start()` requires `ENABLED` first). Adding a second,
parallel transition-validation layer on top of an already-enforced one
would be exactly the "second state machine" the brief warns against.
Lifecycle transitions are therefore implicit: whenever the underlying
`StrategyStatus` or assignment/account state changes (through the existing,
already-gated mutation paths), the next lifecycle read reflects it
immediately and correctly — proven by
`tests/common/test_strategy_lifecycle.py`'s coverage of every reachable
combination (Section 12).

## 6. Assignment dependency

Uses the existing `Strategy -> StrategyAssignment -> TradingAccount` chain
directly (`state.strategy_registry`, `state.strategy_assignment`,
`state.broker_manager` — the same `ExecutionState` singleton every other
route in `execution_routes.py` already reads). No second
strategy-to-account mapping was created. `StrategyAssignment`'s `Assignment`
dataclass has no separate `assignment_id` field (Phase 1/7: `strategy_id`
is the assignment's own canonical key, a 1:1 map) — `assignment_id` in
`StrategyLifecycleView` is therefore `strategy_id` itself when an
assignment exists, `None` otherwise, not a fabricated second identifier.

**Fail-closed anomaly case**: `StrategyRegistry` and `StrategyAssignment`
are intentionally decoupled (Phase 10 never checks for an assignment before
`start()`), so a strategy can technically be started without ever being
assigned. Per the brief's explicit instruction ("Missing assignment ->
ERROR / NOT READY, not RUNNING"), this case is detected and reported as
`ERROR` with an explicit `"anomalous: ..."` blocking reason — never as
`RUNNING`. Covered by
`test_active_strategy_with_no_assignment_is_anomalous_error_never_running`.

## 7. Readiness behavior

Reuses `trading/common/assignment_readiness.py::check_assignment_readiness()`
without modification or duplication. `READY` requires the assignment to be
**sound** — `readiness.assignment_valid AND readiness.authorization_ok` —
which checks: strategy exists, assignment exists, account exists, account
is not `KILLED`/`DISABLED`, broker relationship exists and is available,
and `authorization_state` permits the assignment's `execution_mode`
(reusing `execution.py`'s own gate function via the Phase 16.2 public
alias). It deliberately does **not** require
`readiness.order_execution_allowed` (which additionally requires the
strategy to already be running and the kill switch to be clear) — those
are EXECUTION-readiness facts, not lifecycle-READY facts, and folding them
in would make `READY` unreachable for any strategy that hasn't already
started (a circular, meaningless definition). `blocking_reasons` filters
out exactly those two execution-specific reasons ("not running/shadow",
"kill switch is engaged") so they don't pollute lifecycle diagnostics,
while everything else (disabled/killed account, broker unavailable,
authorization mismatch, disabled assignment) is preserved verbatim.

## 8. Execution Engine integration

**None, by design.** `strategy_lifecycle.py` is never imported by
`trading/common/execution.py`, never constructs a
`StrategyExecutionEngine`, and the existing gate order (Kill Switch →
Idempotency replay → Account/Authorization → RiskManager → Mode Gate →
Broker Resolution → LiveAuthorization consume → Idempotency claim → Broker
Call → Response Validation → Persistence) is byte-for-byte unchanged. The
only change inside `trading/common/execution.py` across this entire
Phase-16.x arc remains Phase 16.2's one-line public alias
(`check_authorization_state_for_mode`), reused again here — nothing new
was added to that file this phase.

### Start/Stop mutation controls — traced, not changed

`POST /api/strategies/{id}/start|stop` already existed (Phase 11). Traced
per the brief's checklist:
1. Calls `Strategy.enable()`/`start()`/`stop()` — pure in-memory
   `StrategyStatus` flips.
2. Never calls a broker (no `BrokerManager.get_broker()` anywhere in
   `execution_routes.py` — enforced by the pre-existing structural test
   `test_execution_routes_module_never_calls_get_broker`).
3. Cannot trigger a broker mutation — confirmed by (2).
4. Bypasses `StrategyExecutionEngine` entirely — but there is nothing to
   "bypass": these endpoints never claimed to reach it, and no order is
   ever generated as a side effect (`generate_order_intents()` is never
   called from any route, per the module's own SAFETY docstring).
5–7. Cannot bypass RiskManager/kill switch/authorization because it never
   reaches any of them. **Verdict: safe, unchanged, no gap.** This phase
   adds no new start/stop/pause control of any kind.

## 9. API changes

Two new read-only routes, VIEW permission (same as every other read
endpoint in this file):

```
GET /api/strategy-lifecycle              -> list[StrategyLifecycleOut]
GET /api/strategy-lifecycle/{strategy_id} -> StrategyLifecycleOut  (404 if unregistered)
```

`StrategyLifecycleOut` mirrors `StrategyLifecycleView` exactly — no
credential, API key, token, or `credential_reference` field exists on it.
No existing route was modified except the module/route-list docstring.

## 10. Frontend changes

- `frontend/src/pages/TccLifecyclePage.tsx` (new) — a read-only table:
  Strategy | Account | Lifecycle | Authorization | Execution | Last
  transition | Last error. Lifecycle, Authorization, and Execution are
  rendered as three visually separate columns/values (never merged into
  one badge), per the brief's explicit "this distinction must be visually
  obvious" requirement. Contains **no** Start/Stop/Pause/Authorize/Go
  Live/Buy/Sell/Place Order control of any kind.
- New route `/execution/lifecycle` ("Strategy Lifecycle") added to
  `routes.tsx`, VIEW permission, alongside the existing Execution nav
  section.
- New hook `useStrategyLifecycle()` (`api/hooks.ts`) and endpoint
  `listStrategyLifecycle()`/`getStrategyLifecycle()` (`api/endpoints.ts`),
  following the exact existing `useExecutionAssignments`-style pattern.
- New type `StrategyLifecycle`/`LifecycleStateValue` in `api/types.ts`.
  `ExecutionAccount` also gained `authorization_state` (the backend already
  returns it since Phase 16.2; the frontend type had not caught up —
  fixed here since the new lifecycle page needed it, and
  `TccAccountsPage.test.tsx`'s mock fixtures were updated to match the now
  fully-typed interface).

## 11. Security verification

- `trading/common/strategy_lifecycle.py` contains no reference to
  `place_order`/`modify_order`/`cancel_order`, no import of a broker
  adapter, and no import of anything under the live-authorization/canary
  modules — enforced by
  `test_module_never_touches_broker_or_live_authorization` (source scan).
- `trading/api/execution_routes.py`'s existing structural tests
  (`test_execution_routes_module_never_calls_get_broker`,
  `..._never_touches_live_authorization`, `..._never_wires_a_live_authorization_store`)
  still pass unchanged, now also covering the new lifecycle routes (they
  scan the whole file).
- No strategy is auto-started: `test_strategy_lifecycle_reading_never_starts_a_strategy_or_mutates_anything`
  proves a lifecycle read leaves `StrategyStatus`/assignments untouched.
- No account authorization is changed by this phase: nothing in
  `strategy_lifecycle.py` or the new routes calls
  `TradingAccount.set_authorization_state()`/`set_killed()`.
- No credential/token/secret is present in any new schema field.
- Cross-account isolation: `test_two_strategies_on_different_accounts_report_independent_lifecycles`
  (backend) and the frontend test asserting one strategy's row never shows
  another account's ID.

## 12. Tests

### Backend (targeted, Phase 16.3-specific)
- `tests/common/test_strategy_lifecycle.py` — 15 tests: initial STOPPED,
  unknown strategy, ENABLED+no-assignment (STOPPED, not READY),
  ENABLED+valid assignment (READY), disabled-account assignment,
  RUNNING+execution_active, ERROR status, anomalous active-without-
  assignment (ERROR, never RUNNING), READ_ONLY-vs-LIVE_AUTHORIZED
  distinction (two dedicated tests), KILLED account, cross-account
  isolation, structural safety scan, PAUSED-never-produced.
- `tests/api/test_execution_routes.py` — 5 new lifecycle-route tests
  (list, 404, assignment-before-enable is STOPPED, RUNNING after start,
  read-has-no-side-effects) plus the two pre-existing Phase 16.1
  structural safety tests (still pass, now covering the new routes too).

### Backend — full regression
Command: `python -m pytest -q`, captured via redirect + `echo "PYTEST_EXIT=$?"`
(never piped through `tail`).

```
PYTEST_EXIT=0
```
Result tally: **1690 passed**, **6 skipped** (pre-existing/unrelated),
**0 failed**, **0 errored** (`grep -c "^FAILED"` = 0, `grep -c "^ERROR"` = 0).
The ~20 `PytestUnhandledThreadExceptionWarning` lines are the same
pre-existing, documented, benign fake-SmartAPI `orderBook()` polling
artifact noted in every prior phase's regression in this engagement.

### Frontend
- `TccLifecyclePage.test.tsx` (new, 7 tests): loading, error, distinct
  lifecycle/authorization/execution columns, STOPPED-vs-READY display,
  never labels a non-live-authorized/non-executing strategy as such, no
  live-trading or lifecycle-mutation control of any kind, cross-account
  display isolation.
- `TccAccountsPage.test.tsx` updated (added `authorization_state` to its
  mock fixtures to match the now-complete `ExecutionAccount` type).
- Full suite: **55 passed**, 0 failed, 0 errored (9 test files).
- `npx tsc --noEmit`: **0 errors**.
- `npm run build`: succeeded (`BUILD_EXIT=0`).

## 13. Full regression

```
Backend:  1690 passed, 0 failed, 0 errors, 6 skipped   (PYTEST_EXIT=0)
Frontend: 55 passed, 0 failed, 0 errors, 0 skipped      (exit 0)
Frontend build: BUILD_EXIT=0
Frontend typecheck: TSC_EXIT=0
```

One frontend flake was observed and independently resolved: on the first
full-suite run, `TccStrategiesPage.test.tsx`'s pre-existing (unmodified by
this phase) "Start/Stop only ever flip an in-memory process-lifecycle
flag" test timed out under parallel load; re-run in isolation it passed in
474ms, and the full suite's second run (reported above) passed cleanly.
Classified as **ENVIRONMENTAL**, not a regression — the file was not
touched by this phase.

## 14. Git commit SHA

Branch: `web-base-algo-trading-control`. Files staged/committed: this
report, `trading/common/strategy_lifecycle.py`,
`tests/common/test_strategy_lifecycle.py`, `trading/api/execution_routes.py`,
`tests/api/test_execution_routes.py`,
`frontend/src/pages/TccLifecyclePage.tsx`,
`frontend/src/pages/TccLifecyclePage.test.tsx`, `frontend/src/routes.tsx`,
`frontend/src/api/{types,endpoints,hooks}.ts`,
`frontend/src/pages/TccAccountsPage.test.tsx`. Commit SHA and push status
recorded in the final response below.
`docs/phase-15d-11-human-live-canary-review-report.md` was verified
untouched and unstaged before committing (still present, still
uncommitted, byte-identical to Phase 16.2's state).

## 15. Production status

**NOT DEPLOYED.** No production host was touched, no deployment command was
run, no environment/credential/risk-limit change was made.

## 16. Live safety status

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED — no `LiveAuthorization` object was
  created, consumed, or referenced by any code added this phase.
- Strategies: STOPPED — no strategy was started by this work outside of
  isolated, per-test fixtures (identical pattern to every prior Phase
  11+ test in this suite).
- Real broker mutations this phase: **0**.

## Known limitations

- `READY` (StrategyStatus.ENABLED) is a real, fully-tested lifecycle state
  at the Python/`StrategyRegistry` level, but is not independently
  reachable through the current HTTP API, since
  `POST /api/strategies/{id}/start` enables-and-starts in a single call
  and no standalone "enable only" endpoint exists. Adding one was judged
  out of scope for a read-only-visibility phase; this is documented, not
  silently hidden (Section 2, Section 9's test note).
- `PAUSED` exists in the vocabulary only; implementing real pause/resume
  would require a new `Strategy.pause()`/`resume()` capability that does
  not exist today, which the brief explicitly says not to add this phase.
