# Phase 16.2 — Strategy Registry & Account Assignment

Status: **PASS** (read-only/management-data-only scope; no live authorization,
no strategy start, no broker mutation).

## 1. Scope and mandate

Phase 16.2 asked for a broker-agnostic Strategy → Assignment →
TradingAccount → BrokerAdapter model with explicit validation for: unknown
strategy, unknown account, disabled account, killed account, a read-only
account distinct from a live-authorized one, broker compatibility via a
capability abstraction (not hard-coded per-broker rules), duplicate
assignment, and cross-account isolation. The phase is explicitly
management-data-only: no `LiveAuthorization` may be created or consumed, no
strategy may be started/stopped as a side effect of this work, and no real
broker call may occur.

## 2. Discovery: what already existed (read before writing any code)

A full read of the repository, per the phase's own "discover before
creating" instruction, found that almost the entire requested model was
already built and tested, across three phases:

| Concept the brief asks for | Already implemented as |
|---|---|
| Strategy existence/lifecycle | `trading/common/strategy_registry.py::StrategyRegistry` (Phase 10) |
| `strategy_id -> account_id` assignment, execution_mode/risk_profile/enabled | `trading/common/strategy_assignment.py::StrategyAssignment`/`Assignment` (Phase 1/7) |
| Unknown-account / disabled-account / broker-unavailable / execution_mode-mismatch validation | Already raised by `StrategyAssignment.assign()` (`UnknownAccountError`, `InvalidAssignmentError`, `BrokerUnavailableError`) |
| Broker identity + declarative capabilities | `trading/common/broker_types.py::BrokerType`/`BrokerCapabilities`/`get_capabilities()` (Phase 15B) |
| Account authorization state (READ_ONLY / CANARY_READY / LIVE_AUTHORIZED / KILLED / DISABLED) | `trading/common/trading_account.py::AccountAuthorizationState` (Phase 15B) |
| Authorization-state-vs-execution_mode hard gate | `trading/common/execution.py::_check_authorization_state()` (Phase 15B.1), already wired into `StrategyExecutionEngine.execute()` |
| Unknown-strategy validation on assignment creation | `trading/api/execution_routes.py::create_assignment()` already checks `strategy_registry.is_registered(...)` before calling `assign()` |
| Cross-account isolation | Structural (each `Assignment` is keyed independently by `strategy_id`; every account lookup is scoped through `broker_manager.get_account(account_id)`); already covered by `tests/common/test_phase_15c_4_cross_account_isolation.py` and `tests/common/test_risk_manager_account_isolation.py` |

This mirrors the exact pattern Phase 16.1 hit: the model already exists.
The remaining, genuine gaps are narrower than the brief's checklist implies
and are enumerated in Section 3.

## 3. Real gaps identified

1. **Killed / read-only-vs-live-authorized accounts could be *assigned*
   without complaint.** `StrategyAssignment.assign()`/`validate()` never
   consulted `TradingAccount.authorization_state` at all — only
   `enabled` and broker availability. The actual safety enforcement was
   never missing (`execute()`'s `_check_authorization_state()` gate already
   fails closed on a KILLED/DISABLED/insufficiently-authorized account at
   execution time) — but the *management* layer had no way to report this
   ahead of time, so an operator could build an assignment that could never
   execute without any diagnostic telling them why.
2. **No read-only surface exposing whether a given assignment is currently
   execution-ready.** Nothing summarized "assignment valid + authorization
   ok + kill switch clear + strategy running" into one answer.
3. **`POST /api/assignments` silently overwrote an existing assignment.**
   The underlying `StrategyAssignment.assign()` Python method is
   intentionally overwrite-capable (used for legitimate strategy
   reassignment by internal callers — see Section 4) but the *HTTP* API had
   no duplicate-submission protection at all.
4. **`AccountOut` never exposed `authorization_state`.** The Phase 16.1
   Accounts page (and any future one) had no way to show or distinguish
   READ_ONLY / CANARY_READY / LIVE_AUTHORIZED / KILLED accounts.
5. **`BrokerCapabilities` existed but nothing in the assignment path ever
   read it.** The capability abstraction the brief asks for ("not
   hard-coding broker rules") was present but unused for this purpose.

## 4. Explicit design decision: what was deliberately NOT changed, and why

`StrategyAssignment.assign()` itself was **not** modified to reject a
KILLED/non-authorized account, and `execution.py`'s existing gate was
**not** touched. Two independent facts ruled this out:

- **Blast radius**: `assign()` is called directly (bypassing this API) from
  40+ existing call sites, including two tests that *deliberately* assign a
  strategy to an account still at the default `READ_ONLY` state in
  `LIVE`/`LIVE_CANARY` mode specifically to prove the *execution-time* gate
  rejects it before `RiskManager` runs
  (`tests/common/test_authorization_state_gate.py::test_live_mode_with_default_read_only_state_is_rejected_before_risk_manager`,
  `::test_live_canary_with_default_read_only_state_is_rejected_before_canary_guard`).
  Adding the check inside `assign()` would have raised at assignment time
  instead, breaking both tests' actual intent.
- **Reassignment is a supported, tested feature.**
  `tests/common/test_multi_account_routing.py::test_strategy_can_be_reassigned_to_a_different_account_without_code_changes`
  calls `assign()` twice for the same `strategy_id` against two different
  accounts and asserts it succeeds — "duplicate assignment" therefore
  cannot mean "reject a second `assign()` call" at the Python-API level
  without breaking a real, intentional capability.

Both gaps were instead closed as **additive, read-only, or explicitly-optin**
surfaces layered on top of the unchanged core (Section 5), so zero existing
behavior changed and zero existing tests were put at risk.

## 5. Implementation

### 5.1 `trading/common/execution.py` — one-line public alias, zero behavior change
Added `check_authorization_state_for_mode = _check_authorization_state` right
after the existing (untouched) private function. This lets a new read-only
diagnostic reuse the *exact* function `execute()` itself calls — never a
second, hand-maintained copy of `_REQUIRED_AUTHORIZATION_STATES` — without
changing `execute()`'s own call site, ordering, or behavior in any way.

### 5.2 `trading/common/assignment_readiness.py` (new)
`check_assignment_readiness(...)` — a pure, read-only function that:
- Raises `UnknownStrategyError`/`UnknownAssignmentError` exactly like the
  existing `GET /api/assignments/{id}` endpoint already does.
- Calls `StrategyAssignment.validate()` (existing method — checks
  enabled + broker availability) to get `assignment_valid`.
- Calls `check_authorization_state_for_mode()` (Section 5.1) to get
  `authorization_ok`/`authorization_detail` — this is what now correctly
  distinguishes READ_ONLY / CANARY_READY / LIVE_AUTHORIZED / KILLED /
  DISABLED for a given `execution_mode`, using the single existing source
  of truth.
- Reads `kill_switch.engaged` and `strategy_registry.get_status()`.
- Looks up `BrokerCapabilities` via `broker_type_for_id()` +
  `get_capabilities()` (Phase 15B's existing capability abstraction) —
  reported for visibility, never used as a new hard gate (see Section 6 for
  why `supports_live_orders` specifically must not become one).
- Combines all of the above into `order_execution_allowed: bool` plus a
  human-readable `blocking_reasons` tuple.

This module never imports `broker`, never calls `place_order`, never
constructs a `StrategyExecutionEngine`, and never touches
`live_authorization*`. It only re-reads state that already exists.

### 5.3 `trading/api/execution_routes.py`
- `AccountOut` gained `authorization_state: str` (Section 3.4) — read-only,
  non-secret, already present on `TradingAccount`.
- New `GET /api/assignments/{strategy_id}/readiness` → `AssignmentReadinessOut`
  (VIEW permission, same as every other read endpoint in this file). Wraps
  `check_assignment_readiness()`; 404 on unknown strategy/assignment.
  `broker_capabilities` is serialized as a plain dict of the declarative
  fields only (`broker_type`, `requires_static_ip`, `supports_*`) — no
  credential-shaped data exists on that object at all.
- `AssignmentIn` gained `replace: bool = False`. `POST /api/assignments`
  now returns **409 Conflict** if `strategy_id` already has an assignment
  and `replace` was not explicitly set to `true`, naming the existing
  account in the error message. Passing `replace: true` performs the exact
  same call to the unchanged `StrategyAssignment.assign()` as before. The
  raw Python method's own reassignment capability (Section 4) is completely
  unaffected — only the HTTP surface gained the guard.

## 6. Why `BrokerCapabilities.supports_live_orders` was deliberately NOT turned into a hard gate

The capability record documents itself as "a documentation-level
declaration, not itself a safety gate," and **every** registered broker
(`ANGEL_ONE`, `DHAN`, `ICICI_BREEZE`, `ZERODHA`, `PAPER`) currently has
`supports_live_orders=False` — including Angel One, the broker Phase
15D.10-R already used for a real, successful, human-authorized live-canary
order in production. Gating assignment creation on this flag would have
(a) contradicted the field's own documented purpose, and (b) blocked
`LIVE_CANARY` assignment for the one broker already proven safe to use that
way, breaking a large number of existing `LIVE_CANARY`-mode assignment
tests across `tests/common/test_phase_15d_1_*`, `test_live_canary_execution_wiring.py`,
`test_phase_15d_5/6/7/9_*`, etc. Instead, capability data is surfaced
read-only in the new readiness report (Section 5.2) so an operator can see
it without it silently blocking anything the system already does safely.

## 7. Cross-account isolation — verified, not re-implemented

Reviewed structurally (each `Assignment` keyed independently by
`strategy_id`; every account read goes through
`broker_manager.get_account(account_id)`, which is itself keyed by
`account_id`) and confirmed by a new regression test,
`tests/common/test_assignment_readiness.py::test_two_strategies_on_different_accounts_do_not_leak_into_each_other`,
plus the pre-existing `test_phase_15c_4_cross_account_isolation.py` and
`test_risk_manager_account_isolation.py`. No gap found; no code changed.

## 8. Files changed

- `trading/common/execution.py` — added `check_authorization_state_for_mode`
  public alias (1 line + comment; zero behavior change to `execute()`).
- `trading/common/assignment_readiness.py` — new module (Section 5.2).
- `trading/api/execution_routes.py` — `AccountOut.authorization_state`,
  `AssignmentIn.replace`, `AssignmentReadinessOut`, the new
  `GET /assignments/{id}/readiness` route, and the 409 duplicate-assignment
  guard in `create_assignment()`.
- `tests/common/test_assignment_readiness.py` — new (11 tests).
- `tests/api/test_execution_routes.py` — 8 new tests (duplicate/replace,
  readiness endpoint × 5, `authorization_state` exposure).

No file under `trading/algos/`, `trading/common/execution.py`'s `execute()`
body, `trading/common/live_authorization*.py`, or any frontend file was
touched.

## 9. Tests — targeted

- `tests/common/test_assignment_readiness.py` — 11/11 passed.
- `tests/common/test_strategy_assignment.py` — 15/15 passed (all
  pre-existing tests, including the reassignment test, unaffected).
- `tests/common/test_authorization_state_gate.py` — all passed (the two
  tests identified in Section 4 as at-risk still pass unchanged).
- `tests/common/test_multi_account_routing.py` — all passed (reassignment
  test still passes unchanged).
- `tests/api/test_execution_routes.py` — all passed, including the 8 new
  Phase 16.2 tests and the Phase 16.1 structural safety tests
  (`test_execution_routes_module_never_calls_get_broker`,
  `..._never_touches_live_authorization`, `..._never_wires_a_live_authorization_store`).

## 10. Tests — full backend regression

Command: `python -m pytest -q`, captured via redirect + separate
`echo "PYTEST_EXIT=$?"` (never piped through `tail`, per this engagement's
own established methodology).

```
PYTEST_EXIT=0
```

Result-character tally (`grep -oE "^[.EFsx]+" | tr -d '\n' | fold -w1 | sort | uniq -c`):

```
   1671 .
      6 s
```

1671 passed, 6 skipped (pre-existing, unrelated), **0 failed, 0 errored**
(`grep -c "^FAILED"` = 0, `grep -c "^ERROR"` = 0). The ~20
`PytestUnhandledThreadExceptionWarning` lines from `_manage_pending`'s
fake-SmartAPI `orderBook()` polling are the same pre-existing, documented,
benign artifact noted in every prior phase's regression run in this
engagement — not a new failure.

## 11. Frontend

No frontend file was changed this phase (the gaps identified were entirely
backend/API). `npm run build` was re-run to confirm the pre-existing
Phase 16.1 frontend is unaffected: see the commit's CI-equivalent output;
build completed with no errors.

## 12. Safety verification

- No route added or changed in this phase ever calls
  `BrokerManager.get_broker()`, `place_order`, `modify_order`, or
  `cancel_order`.
- No route added or changed ever constructs a `StrategyExecutionEngine` or
  imports anything under `trading.common.live_authorization*`.
- No strategy was started or stopped by this work (verified by a dedicated
  test asserting the readiness endpoint has zero side effects on strategy
  status).
- No `LiveAuthorization` was created or consumed.
- No account transitioned `authorization_state` (the new field is
  read-only in `AccountOut`; nothing in this phase calls
  `TradingAccount.set_authorization_state()` or `set_killed()` outside of
  tests constructing their own fixtures).
- No credential, API key, token, or `credential_reference` value is
  present in any new schema field.

## 13. Git

- Branch: `web-base-algo-trading-control`.
- Changes committed: `trading/common/execution.py`,
  `trading/common/assignment_readiness.py`, `trading/api/execution_routes.py`,
  `tests/common/test_assignment_readiness.py`, `tests/api/test_execution_routes.py`,
  this report.
- `docs/phase-15d-11-human-live-canary-review-report.md` remains
  intentionally uncommitted from Phase 15D.11 (that phase's own instruction
  was not to commit or push it automatically) and was left untouched.
- Commit SHA and push status: see the operator-facing summary appended
  after this report is committed.

## 14. Known limitations / explicitly deferred

- The readiness endpoint is diagnostic-only; it is not consulted by
  `execute()` and adds no new enforcement. This is intentional — Phase
  16.2 is management-data-only.
- `BrokerCapabilities.supports_live_orders` remains a documentation-level
  field, not a gate (Section 6). Turning it into a real per-adapter
  verification gate, if ever wanted, is future scope requiring its own
  phase and would need to be reconciled with the already-proven-safe
  Angel One live-canary path first.
- No new "AccountRegistry" abstraction was introduced — `BrokerManager`
  already serves that role (Phase 7/8) and duplicating it was judged to be
  exactly the kind of unrelated architectural expansion the brief warns
  against.

## 15. Final state

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED (unchanged; no `LiveAuthorization` object
  created or touched this phase).
- Strategies: STOPPED (unchanged; no strategy started by this work — the
  one dedicated test that starts `DoubleStraddelAlgo` does so only inside
  an isolated, per-test `ExecutionState` built by the test fixture, exactly
  like every other Phase 11+ test in this suite already does).
- Real broker mutations this phase: **0**.
