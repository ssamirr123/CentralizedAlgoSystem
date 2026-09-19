# Phase 16.4 — Strategy Control Plane & Safe Lifecycle Commands

Status: **PASS** (control-plane-only START/STOP; no execution engine, no
LiveAuthorization, no broker mutation, no automatic restart).

## 1. Objective

Add explicit, auditable, idempotent lifecycle commands (START/STOP) on top
of Phase 16.2 (assignment readiness) and Phase 16.3 (lifecycle projection),
while proving:

```
Strategy Lifecycle  !=  Account Authorization  !=  Live Authorization  !=  Order Execution
```

and that the control plane creates no second execution engine.

## 2. Existing START/STOP call graph

Traced exactly, per the brief's own checklist, before writing any code:

```
HTTP POST /api/strategies/{id}/start          (trading/api/execution_routes.py::start_strategy)
  -> state.strategy_registry.get(strategy_id)         [StrategyRegistry, Phase 10]
  -> if status in {disabled, stopped, error}: strategy.enable()
       BaseStrategy.enable() -> self._status = ENABLED   (pure in-memory flag flip)
  -> strategy.start()
       BaseStrategy.start():
         requires status == ENABLED (else InvalidStrategyStateError -> route catches -> HTTP 409)
         self._status = STARTING
         self._on_start()             -- no-op for every existing concrete strategy adapter
         self._status = SHADOW or RUNNING (per execution_mode)
         records started_at, a metrics-registry heartbeat, an EVENT_STRATEGY_STARTED audit-trail entry
  -> route writes ONE audit.record(STRATEGY_STARTED) row
  -> returns StrategyOut (status/execution_mode/metrics only)

HTTP POST /api/strategies/{id}/stop           (::stop_strategy) -- symmetric:
  -> strategy.stop(): requires status in {RUNNING, SHADOW} (else 409)
       self._on_stop() (no-op) -> self._status = STOPPED, stopped_at recorded,
       EVENT_STRATEGY_STOPPED audit-trail entry, AlertManager.strategy_stopped()
  -> route writes ONE audit.record(STRATEGY_STOPPED) row
```

Answers to the brief's 11 questions:

| # | Question | Answer |
|---|---|---|
| 1 | Does START mutate state? | Yes -- exactly one field, `Strategy._status` (in-memory). |
| 2 | Does START start a real process? | No -- no thread, subprocess, or scheduler is created anywhere in this call graph. |
| 3 | Does START invoke a strategy loop? | No -- there is no loop; `generate_order_intents()` is a separate method never called here. |
| 4 | Does START create an `OrderIntent`? | No. |
| 5 | Does START invoke `StrategyExecutionEngine`? | No -- `execution_routes.py` never imports or constructs it (Phase 16.1's own structural test proves this for the whole module). |
| 6 | Does START invoke broker APIs? | No -- `BrokerManager.get_broker()` is never called from this file (Phase 16.1 structural test). |
| 7 | Does STOP terminate a real process? | No real process exists to terminate; it flips the same in-memory flag back. |
| 8 | Can START bypass `RiskManager`? | No -- START never reaches `RiskManager`; there is nothing to bypass. |
| 9 | Can START bypass the kill switch? | Same as above -- START never reaches the kill switch gate inside `execute()`. Phase 16.4 additionally makes START itself *respect* the kill switch pre-emptively (Section 5), which is new and stricter, not a bypass. |
| 10 | Can START bypass `LiveAuthorization`? | No -- no code path from START reaches `trading/common/live_authorization*.py`. |
| 11 | Can START result in an order without explicit authorization? | No -- an order can only be placed by `StrategyExecutionEngine.execute()`, which is never constructed or called anywhere in this call graph. |

**Conclusion: START already means "activate strategy process/configuration," never "begin actual order execution."** It was already safe. Phase
16.4 therefore implements it, per the brief's own guidance, as an explicit
**CONTROL-PLANE START** — hardened with pre-validation, idempotency, and
audit — without needing to withhold it from the UI.

## 3. Existing control-plane functionality discovered

No `StrategyController`/`StrategyControlService`/`StrategyManager`/
`StrategyRunner`/`Command` abstraction exists anywhere in the repository
(confirmed by search). The closest things are: `StrategyRegistry` (Phase
10, lifecycle mutation primitives), `StrategyAssignment`/
`check_assignment_readiness` (Phase 16.2), and `check_lifecycle` (Phase
16.3, reporting). None of them is a "command service" — none is idempotent
against repeated calls (the existing route raises 409 on a repeat START,
proven by the pre-existing, still-passing test
`test_starting_an_already_running_strategy_is_409`), and none performs a
Phase-16.2/16.3-aware pre-check before mutating. This is Phase 16.4's one
real, scoped gap.

**Decision on the existing 409 contract**: `test_starting_an_already_running_strategy_is_409`
and `test_stopping_a_disabled_strategy_is_409` are pre-existing, deliberate
Phase 11 tests pinning `POST /api/strategies/{id}/start|stop`'s behavior.
Changing that endpoint's response to be idempotent-success (as this
phase's own idempotency requirement demands) would break them. Per this
engagement's established precedent (Phase 16.2's identical situation with
`StrategyAssignment.assign()`'s reassignment capability), the existing
endpoint was left **completely unchanged**, and the new idempotent,
readiness-validated, auditable command surface was added as one
**additional** endpoint reusing the same underlying primitives — not a
second, duplicate control plane, and not a behavior change to code with an
established, tested contract.

## 4. Control command model

`trading/common/strategy_control.py` (new):

```python
class ControlCommand(str, Enum):      # exactly these two, nothing else
    START = "START"
    STOP = "STOP"

class CommandResult(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NOOP = "NOOP"
    FAILED = "FAILED"

@dataclass(frozen=True)
class StrategyControlOutcome:
    command_id: str            # uuid4, generated per call -- no persisted command store
    strategy_id: str
    assignment_id: str | None  # = strategy_id when an assignment exists (Phase 16.3's own convention)
    account_id: str | None
    command: ControlCommand
    result: CommandResult
    previous_state: LifecycleState   # reused from Phase 16.3, not redefined
    new_state: LifecycleState
    accepted: bool
    live_authorized: bool
    execution_started: bool    # unconditionally False -- see Section 8
    reason: str
```

No `BUY`/`SELL`/`PLACE_ORDER`/`AUTHORIZE_LIVE`/`GO_LIVE` member exists on
`ControlCommand`, enforced by `test_control_command_only_has_start_and_stop`.

`command_id` is generated fresh per call (`uuid.uuid4()`), not persisted in
a new store: idempotency is achieved by inspecting the strategy's own
authoritative status before acting (Section 9), which needs no
command-history table at all — deliberately avoiding "a second unrelated
idempotency implementation" alongside the existing order-level
`trading/common/idempotency_store.py` (a different concern: broker-call
replay, not lifecycle commands).

## 5. START semantics

1. If the strategy's raw status is already `RUNNING`/`SHADOW` -> **NOOP**
   (idempotent success, no mutation).
2. If the kill switch is engaged -> **REJECTED**.
3. If no assignment exists -> **REJECTED**.
4. Otherwise, reuse `check_assignment_readiness()` (Phase 16.2, unmodified)
   and require `assignment_valid AND authorization_ok` -> if either is
   false, **REJECTED** with the underlying reasons (disabled/killed
   account, broker unavailable, authorization_state doesn't permit the
   assigned execution_mode, etc.).
5. Only if all of the above pass: call the exact same
   `Strategy.enable()`/`start()` the pre-existing route already calls.
   Any `InvalidStrategyStateError` (should be unreachable given the
   pre-checks, but handled fail-closed) -> **FAILED**.
6. Recompute lifecycle via `check_lifecycle()` (Phase 16.3, unmodified) and
   return **ACCEPTED**.

`execution_started` is **unconditionally `False`** in every branch — this
service never calls `generate_order_intents()` and never constructs an
execution engine, so there is no code path by which it could become true.
Proven by `test_execution_started_is_always_false_regardless_of_result`.

## 6. STOP semantics

1. If the strategy's raw status is **not** `RUNNING`/`SHADOW` (i.e.
   `DISABLED`/`ENABLED`/`STOPPED`/`ERROR`) -> **NOOP**.
2. Otherwise call `Strategy.stop()` (unmodified); `InvalidStrategyStateError`
   (unreachable given the check above, handled fail-closed) -> **FAILED**.
3. Recompute lifecycle -> **ACCEPTED**.

STOP never calls a broker mutation API, never cancels or closes a
position, and never submits an exit order — it flips exactly the same
in-memory `StrategyStatus` flag START does, in reverse. `STOP STRATEGY` is
never interpreted as `CLOSE ALL POSITIONS`; no code path in
`strategy_control.py` references positions at all.

**Deliberate design correction during implementation**: an early draft
decided NOOP-vs-act by comparing the Phase 16.3 `lifecycle_state`
(`STOPPED`/`ERROR` -> NOOP). This was wrong for one real edge case: Phase
16.3's own anomaly rule reports `lifecycle_state == ERROR` for a strategy
that is raw-status `RUNNING` but has no assignment (Phase 16.3 Section 6).
Under the lifecycle-state-based rule, STOP on that strategy would have been
a NOOP, silently leaving a genuinely active, unassigned strategy running
forever with no way to stop it. Fixed by deciding NOOP-vs-act from the
strategy's own **authoritative** `StrategyStatus` (the same set
`BaseStrategy.stop()` itself already requires) instead of the derived
lifecycle report — a control decision must never be made from a reporting
projection. Covered by
`test_stop_actually_stops_the_anomalous_active_with_no_assignment_case`.

## 7. Readiness validation

Reuses `trading/common/assignment_readiness.py::check_assignment_readiness()`
(Phase 16.2) and `trading/common/strategy_lifecycle.py::check_lifecycle()`
(Phase 16.3) directly — neither function's logic is duplicated anywhere in
`strategy_control.py`. START's readiness gate checks: strategy exists,
assignment exists, account exists/enabled/not killed, broker relationship
exists and is available, and `authorization_state` permits the assignment's
`execution_mode` — exactly Phase 16.2's existing "sound assignment"
definition, unchanged.

## 8. Authorization separation

START **never** calls `TradingAccount.set_authorization_state()` or
`set_killed()` — proven by
`test_no_command_ever_authorizes_an_account` (asserts the account's
`authorization_state` is unchanged after a full START+STOP cycle) and by
the structural source scan. A `LIVE`-mode assignment on a `READ_ONLY`
account is explicitly tested and **REJECTED**
(`test_start_from_live_mode_read_only_account_is_rejected`), and a
dedicated execution-engine test
(`test_execution_gates_remain_authoritative_after_control_plane_start`)
proves that even if an intent were somehow generated afterwards, `execute()`'s
own, completely untouched authorization gate independently rejects it —
the control plane adds no alternate path around it.

## 9. Idempotency

`START, START, START` on the same strategy: first call -> `ACCEPTED`;
second and third -> `NOOP`, with **zero** additional calls to
`enable()`/`start()` (verified by asserting `metrics.started_at` and
`StrategyStatus` are unaffected by the repeat calls). Symmetric for
`STOP, STOP`. At the API layer, `test_repeated_start_command_is_idempotent_noop_not_409`
proves the new endpoint returns HTTP 200/NOOP on repeat, never 409 — the
exact idempotency guarantee the brief requires — while
`test_pre_existing_start_stop_endpoints_are_unchanged_and_still_409_on_repeat`
proves the OLD endpoint's contract is completely untouched.

## 10. Concurrency behavior

`tests/common/test_strategy_control.py` fires 8 concurrent `START` calls
at the same strategy from separate threads
(`test_concurrent_start_calls_never_produce_two_active_strategies`) and a
concurrent `START`+`STOP` pair
(`test_concurrent_start_and_stop_leave_a_consistent_final_state`). Both
assert the strategy ends in exactly one well-defined, valid status with no
exception raised. This relies on `BaseStrategy`'s own state transitions
being simple, synchronous attribute writes guarded by the existing
`InvalidStrategyStateError` checks — `strategy_control.py` introduces no
new locking because it introduces no new mutable state of its own (it only
reads/writes through the pre-existing `Strategy` object, whose own
transition checks are what actually prevent an inconsistent result).

## 11. Process management

No process, thread, or subprocess is created by this phase (Section 2)
— there is nothing to track a PID for, restart, or detect as stale.
**No automatic restart was implemented or exists.** A strategy that enters
`ERROR` stays in `ERROR` until an operator explicitly sends a new `START`
command (a manual, auditable, human-triggered action) — never
automatically. `test_stop_noop_from_error_state` confirms STOP does not
fabricate a fake recovery, and no code path in this phase polls or times
out toward re-starting anything on its own.

## 12. Audit

Four new audit actions added to `trading/api/security/audit.py`:
`STRATEGY_COMMAND_ACCEPTED`, `STRATEGY_COMMAND_REJECTED`,
`STRATEGY_COMMAND_NOOP`, `STRATEGY_COMMAND_FAILED` — deliberately distinct
from the pre-existing `STRATEGY_STARTED`/`STRATEGY_STOPPED` (which record
only an actual raw lifecycle mutation, not a command's full outcome
including rejections/no-ops). Every call to the new endpoint writes
exactly one audit row (`test_command_is_audited`), recording
`command_id, command, previous_state, new_state, reason`, the operator
(`principal.actor`/`label`, reused from the existing Phase 11 RBAC
`Principal`), and an `outcome` of `success`/`denied`/`failed` matching the
command result. No credential, token, or secret is ever included —
`test_command_response_never_contains_a_credential_field` scans the full
HTTP response text for `api_key`/`api_secret`/`access_token`/`password`/
`credential`.

## 13. API

One new endpoint, additive, reusing the existing `ExecutionState`/
`Principal`/RBAC/audit infrastructure:

```
POST /api/strategy-lifecycle/{strategy_id}/command
     body: {"command": "START" | "STOP", "reason": ""}
     -> {command_id, strategy_id, assignment_id, account_id, command,
         result, previous_state, new_state, accepted, live_authorized,
         execution_started, reason}
```

Permission: `START` for a `START` command, `STOP` for a `STOP` command
(the existing `Permission` enum, unchanged — no new permission was
invented). `POST /api/strategies/{id}/start|stop` (Phase 11) is untouched,
byte-for-byte, including its response shape and its existing 409 contract.

## 14. Frontend

`TccLifecyclePage.tsx` (Phase 16.3) extended with a "Control" column: a
Start and a Stop button per row, backed by the new `useSendStrategyCommand()`
mutation hook. Start is disabled once a strategy is `RUNNING`; Stop is
disabled once a strategy is `STOPPED`; both are disabled for an operator
lacking the corresponding `START`/`STOP` permission. The page header now
reads *"Strategy control plane — lifecycle control only. No order
execution."* — explicitly not "Go Live"/"Start Trading"/"Enable Live
Trading." A command's result (including a rejection's reason) is shown
inline per row. No `BUY`/`SELL`/`PLACE ORDER`/`CLOSE POSITION`/`EXIT POSITION`/
`AUTHORIZE LIVE`/`GO LIVE` control exists anywhere on this page, proven by
a dedicated test scanning for all of them.

## 15. Security verification

- `trading/common/strategy_control.py` contains no broker SDK import, no
  reference to `place_order`/`modify_order`/`cancel_order`/`PlaceOrder`/
  `BrokerClient`/`smart_api`, and no `StrategyExecutionEngine(` construction
  (structural source scan, `test_module_never_touches_broker_or_live_authorization`).
- Neither START nor STOP calls `TradingAccount.set_authorization_state()`/
  `set_killed()` (`test_no_command_ever_authorizes_an_account`).
- `execution_started` is unconditionally `False` in every outcome
  (`test_execution_started_is_always_false_regardless_of_result`).
- The existing execution-safety gate independently rejects a
  READ_ONLY-account LIVE-mode intent even after a control-plane START
  (`test_execution_gates_remain_authoritative_after_control_plane_start`)
  — proof the control plane is not an alternate path to broker execution.
- No credential/token/secret is present in any new schema field or HTTP
  response.
- Cross-account isolation preserved: the control endpoint only ever acts
  on the single `strategy_id` in the URL, resolved through the existing,
  unmodified `StrategyAssignment`/`BrokerManager` account-scoping.

## 16. Tests

### Backend (targeted)
- `tests/common/test_strategy_control.py` — 20 tests: unknown strategy,
  START rejected (no assignment / disabled account / killed account /
  kill switch engaged / READ_ONLY+LIVE), START accepted, STOP noop
  (stopped / error), STOP accepted, STOP on the anomalous active-
  unassigned case, idempotent repeated START/STOP (x3), concurrent START
  (8 threads) and START+STOP, structural safety scan, "never authorizes
  an account", `execution_started` always False, `ControlCommand` has
  exactly two members, and the dedicated execution-engine-authoritative
  test.
- `tests/api/test_execution_routes.py` — 12 new tests: permission
  enforcement (both directions), unknown command -> 422, unknown strategy
  -> 404, REJECTED/ACCEPTED responses, idempotent-NOOP-not-409, audit row
  written, credential-free response, and confirmation the pre-existing
  `/start`/`/stop` 409 contract is unchanged.

### Backend — full regression
```
PYTEST_EXIT=0
```
Result tally: **1722 passed**, **6 skipped** (pre-existing/unrelated),
**0 failed**, **0 errored**. The ~20 `PytestUnhandledThreadExceptionWarning`
lines are the same pre-existing, documented, benign fake-SmartAPI
`orderBook()` polling artifact noted in every prior phase's regression in
this engagement.

### Frontend
- `TccLifecyclePage.test.tsx` — 13 tests (was 7 in Phase 16.3): loading,
  error, safety label, distinct columns, Start-enabled-when-READY,
  Stop-disabled-when-STOPPED, both-disabled-without-permission, mutate
  called with exact `{strategyId, command}`, command result display
  (including rejection reason), no `execution_started` leak, no
  live-trading/order/authorization control, cross-account display
  isolation.
- `test/utils.tsx`'s `makeMutationResult` gained an optional `variables`
  field (needed to mock React Query's `mutation.variables`, used by the
  page to know which row's button is mid-command).
- Full suite: **61 passed**, 0 failed, 0 errored (9 files).
- `npx tsc --noEmit`: **0 errors**.
- `npm run build`: succeeded.

## 17. Full regression

```
Backend:  1722 passed, 0 failed, 0 errors, 6 skipped   (PYTEST_EXIT=0)
Frontend: 61 passed, 0 failed, 0 errors, 0 skipped      (exit 0)
Frontend build: BUILD_EXIT=0
Frontend typecheck: TSC_EXIT=0
```

One frontend test-writing iteration is worth recording: the first draft of
two new lifecycle-page tests made incorrect assumptions (expecting `STOP`
disabled for a `READY` row, and asserting `getByText("READ_ONLY")` when
the fixture had two matching rows) — both were test bugs, not product
bugs, found and fixed before the final passing run recorded above.

## 18. Git commit SHA

Branch: `web-base-algo-trading-control`. Files: this report,
`trading/common/strategy_control.py`, `tests/common/test_strategy_control.py`,
`trading/api/execution_routes.py`, `trading/api/security/audit.py`,
`tests/api/test_execution_routes.py`, `frontend/src/pages/TccLifecyclePage.tsx`,
`frontend/src/pages/TccLifecyclePage.test.tsx`, `frontend/src/test/utils.tsx`,
`frontend/src/api/{types,endpoints,hooks}.ts`. Commit SHA and push status
recorded in the final response below.
`docs/phase-15d-11-human-live-canary-review-report.md` verified untouched
and unstaged before committing.

## 19. Production status

**NOT DEPLOYED.** No production host was touched, no deployment command
was run, no environment/credential/risk-limit change was made.

## 20. Live safety status

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED — no `LiveAuthorization` object was
  created, consumed, or referenced by any code added this phase.
- Strategies: STOPPED — no strategy was started by this work outside of
  isolated, per-test fixtures.
- Real broker mutations this phase: **0**.
