# Phase 16.5 — Strategy Runtime Integration & Shadow Execution

Status: **PASS** (PAPER/SHADOW-only; zero real broker mutations; a real,
previously-latent safety gap was found and closed during this phase's own
READ step).

## 1. Objective

Connect a registered Phase-10 `Strategy`'s decision logic to the existing
broker-agnostic execution architecture — `Strategy → OrderIntent →
StrategyExecutionEngine → PAPER/SHADOW broker` — proving the complete flow
end-to-end in a structurally non-mutating environment, without creating a
second execution engine and without weakening any existing gate.

## 2. Existing shadow execution discovered

Phase 4's shadow execution is alive and well, in two classes:

- **`ShadowBroker`** (`trading/common/brokers/shadow_broker.py`) — a pure,
  non-networked `BrokerClient`. Its own docstring states the exact
  distinction this phase leans on: "AngelOneBroker's safety is
  CONFIGURATION-based... ShadowBroker's safety is STRUCTURAL: there is no
  SDK import, no network call, no credential handling anywhere in this
  file at all." It does not accept `OrderIntent` directly (it's a
  `BrokerClient`, fed via `place_order(...)` the same as any adapter,
  typically by `StrategyExecutionEngine.execute()`).
- **`ConnectedShadowBroker`** (`trading/common/brokers/connected_shadow_broker.py`)
  — composes a real READ-ONLY broker for quotes/account data with an
  internal `ShadowBroker` for every write; fail-closed by construction
  (requires `execution_mode=ExecutionMode.SHADOW` and `real_broker.is_read_only`).

Both are still actively used (`trading/validation/angel_connected_shadow.py`,
`trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py`, and a dozen
existing tests) — not dead code. Neither persists results itself
(persistence is whatever `AuditTrail`/idempotency store the caller wires
around them); multi-account attribution comes from whichever
`TradingAccount`/`StrategyAssignment` wraps them, same as any broker. No
second shadow-execution framework was created — both classes are reused
completely unmodified.

## 3. Existing strategy runtime discovered

None. Confirmed by two independent facts found during this phase's READ
step:

- **No existing loop connects a `Strategy` to `StrategyExecutionEngine`.**
  Every call site of `generate_order_intents()` in the repository was
  grepped; none of them then calls `engine.execute(...)`. This is the
  actual Phase 16.5 gap, and it is real.
- **The three registered Phase-10 strategy classes
  (`DoubleStraddleStrategy`, `CombinedVwapNiftyStrategy`,
  `VwapAlgoNiftyHedgeStrategy`) generate zero real intents by design** —
  their `_on_generate_order_intents()` hooks return `[]` unconditionally,
  per their own Phase 10 module docstrings ("does NOT wire any live
  algo's real decision logic in... explicitly out of scope"). This phase
  does not rewrite them (see Section 12's "prioritize architectural
  correctness over rewriting all strategy logic").
- Separately, the three LEGACY algo directories
  (`trading/algos/DoubleStraddelAlgo/`, `CombinedVwapNifty/`,
  `Vwap_Algo_Nifty_hedge/`) are real, currently-running production
  processes with their own market-data polling loops and direct
  `config.objconn.placeOrder(...)` calls (each immediately preceded by
  `assert_live_mutation_allowed()`, Phase 15D.9). Only `DoubleStraddelAlgo`
  has any Phase 1-16 framework awareness at all — its
  `broker/execution_bridge.py` builds an `OrderIntent` and mirrors it
  through a *separate*, comparison-only `StrategyExecutionEngine`/
  `ShadowBroker` pipeline on a background thread purely for logging; it
  never affects the real order's outcome, and the real placement path is
  untouched, ad-hoc SDK code. `CombinedVwapNifty` and
  `Vwap_Algo_Nifty_hedge` have no framework references at all. **These
  three legacy processes are explicitly OUT OF SCOPE for this phase** —
  rewriting them into the new architecture would be exactly the kind of
  large, risky, "unrelated" change this phase's own brief warns against
  ("do not silently rewrite the entire strategy").

## 4. Runtime architecture

```
Trading Control Center
        |
        v
Strategy Control (Phase 16.4 START/STOP -- UNCHANGED)
        |
        v
StrategyRuntime (NEW -- trading/common/strategy_runtime.py)
        |
        v
Strategy.generate_order_intents()   (Phase 10 interface, UNCHANGED)
        |
        v
OrderIntent                          (Phase 1, UNCHANGED)
        |
        v
StrategyExecutionEngine.execute()    (UNCHANGED gate order, see Section 7)
        |
        v
PAPER / SHADOW broker (structurally non-networked)
```

`StrategyRuntime.run_once(strategy_id)` is a single, **explicit,
synchronous** evaluation cycle — not a thread, not a scheduler, not a
loop. Nothing in this phase spawns a background process; a caller (an
operator action via the new `POST .../evaluate` endpoint, or a test)
decides when to call it, and only ever once per call. No automatic
restart of a failed strategy exists or was added.

### A latent safety gap found and closed

The READ step traced exactly what would happen if something DID call
`StrategyExecutionEngine.execute()` against the Control Center's own
`ExecutionState` (`trading/api/execution_state.py`) — which nothing did
before this phase, since no route ever called
`BrokerManager.get_broker()`. Tracing `execute()` itself: it
unconditionally calls `get_broker(account_id)` at its broker-resolution
step, for PAPER/SHADOW/LIVE/LIVE_CANARY alike (no special case exists).
The three demo accounts (`ANGEL_MAIN`/`DHAN_MAIN`/`ICICI_MAIN`) were
registered with a **lazy factory** — `create_broker(TradingConfig(broker_name=...,
trading_mode="paper"))` — that would have constructed a **real
`AngelOneBroker`/`DhanBroker`/`ICICIBreezeBroker` instance** (safe only by
a *configuration* setting, "paper" `trading_mode`, per that adapter's own
documented safety model) the first time anything called `get_broker()` on
it. Since this phase's runtime is exactly the first thing that ever does,
this was a real, previously-harmless-only-by-omission gap.

**Fix**: `trading/api/execution_state.py` now registers each demo account
with an eager `broker_client=ShadowBroker()` instead — structurally
incapable of a real network call, matching each account's own
`execution_mode=SHADOW`. Verified to have **zero effect on any
pre-existing test** (nothing called `get_broker()` on these specific
accounts before), except one test whose assertion described the old,
now-superseded state (`test_ready_endpoint_reports_not_configured_by_default`
→ renamed and updated to `test_ready_endpoint_reports_not_connected_by_default`,
since a `ShadowBroker()` is now attached but `connect()` is never called
by the read-only readiness probe — see Section 14).

## 5. Strategy → OrderIntent flow

`StrategyRuntime.run_once(strategy_id)`:
1. Resolve the strategy (`UnknownStrategyError` if unregistered).
2. If not `RUNNING`/`SHADOW` (i.e. not started via Phase 16.4 START) →
   pure no-op (`ticked=False`), nothing evaluated.
3. Call `strategy.generate_order_intents()` — the exact, unmodified Phase
   10 interface method. An exception here marks the strategy `ERROR`
   (never left to look like it's still running) and is reported, never
   raised to the caller.
4. For each returned `OrderIntent`: verify `intent.strategy_id` matches
   the strategy being run (defense against a buggy strategy), assert the
   resolved broker is a known-simulated type (Section 6), then call
   `StrategyExecutionEngine.execute(intent)` — the exact same call any
   other caller of that engine makes.
5. Record a heartbeat (`MetricsRegistry.record_strategy_heartbeat`,
   Phase 13, reused unchanged), the cycle timestamp, and the last N
   `ExecutionResult`s for display — no new persistence system; this is an
   in-memory convenience mirroring `StrategyMetrics`'s existing pattern.

The three real registered strategies generate zero intents (Section 3),
so `run_once()` against them ticks cleanly with zero executions — proving
the pipe is wired without fabricating activity. A test-only
`_OneShotStrategy` stub (in `tests/common/test_strategy_runtime.py`) is
used to prove that an intent which IS generated flows all the way through
to a simulated fill.

## 6. Shadow execution boundary

Three independent, layered guarantees — any one alone would already
prevent a real broker mutation:

1. **`ExecutionConfig(dry_run=True)`, hard-coded**, not a constructor
   parameter of `StrategyRuntime` (no caller can turn it off). Traced
   `execute()`: it calls `self.place_limit()`/`place_market_emergency()`/
   `cancel()`, each of which checks `self._config.dry_run` **before**
   ever calling `active_broker.place_order()`/`cancel_order()` — so no
   broker method is reached regardless of which broker class is resolved.
2. **`_assert_simulated_broker()`** — before handing any intent to the
   engine, the runtime independently resolves the account's broker and
   requires `isinstance(broker, (PaperBroker, ShadowBroker,
   ConnectedShadowBroker))`; anything else is refused
   (`ShadowBoundaryViolation`), the strategy is marked `ERROR`, and
   `execute()` is never called. Proven by
   `test_non_simulated_broker_is_refused_never_executed` (a broker whose
   `place_order` would raise `AssertionError` if ever reached).
3. **`execution_state.py`'s fix** (Section 4) — the accounts this runtime
   is actually wired to in the Control Center can now never resolve to a
   real adapter in the first place.

`test_dry_run_prevents_place_order_even_if_boundary_check_were_bypassed`
independently proves layer 1 alone is sufficient, using a `PaperBroker`
subclass whose `place_order` raises if called.

## 7. Execution Engine integration

`StrategyExecutionEngine` is reused completely unmodified — no
`StrategyExecutionEngineV2`, no forked copy. `StrategyRuntime` owns
exactly one instance, constructed once, wired to the SAME
`broker_manager`/`strategy_assignment`/`risk_manager`/`kill_switch` every
other Control Center component already shares (via `ExecutionState`), so
"assigned via the API" and "executed by the runtime" can never silently
diverge. The gate order inside `execute()` — kill switch → idempotency
replay → account/authorization → RiskManager → mode gate → broker
resolution → LiveAuthorization consume → idempotency claim → broker call
→ response validation → persistence — is untouched; `dry_run=True`
intercepts only the broker-call step itself, after every other gate has
already run in full. RiskManager is never bypassed: every intent this
runtime submits passes through it exactly as any other caller's would.
Idempotency reuses the existing abstraction
(`trading.common.idempotency_store.InMemoryIdempotencyStore`, explicitly
documented as non-production — acceptable here since this entire runtime
is PAPER/SHADOW-only) rather than inventing a second mechanism; a caller
wanting durable shadow-run idempotency may pass a `SqliteIdempotencyStore`
instead (constructor parameter).

## 8. Lifecycle integration

A **fourth**, deliberately separate vocabulary, `RuntimeState` (`INACTIVE`
/ `HEALTHY` / `FAILED`), distinct from `StrategyStatus` (Phase 10),
`AccountAuthorizationState` (Phase 15B), and `LifecycleState` (Phase
16.3). Never merged into any of them. `GET /api/strategy-lifecycle(/{id})`
now also reports `runtime_state`, `last_cycle_at`, `last_runtime_error`,
`last_result_summary`, and a real `last_heartbeat_at` (previously a
pseudo-heartbeat derived from `last_intent_at`; now sourced from the
runtime's actual `MetricsRegistry.record_strategy_heartbeat()` call when a
cycle has run). `execution_mode` was also added to `StrategyLifecycleView`/
`StrategyLifecycleOut` (sourced from the existing `AssignmentReadiness.execution_mode`,
Phase 16.2 — not duplicated) so the TCC can show a "Mode" column. A
strategy that crashes mid-cycle is marked `ERROR`/`FAILED` — never
falsely reported `RUNNING`
(`test_generate_order_intents_crash_marks_error_never_running`).

## 9. Account/assignment routing

`StrategyRuntime` never holds a broker credential and never imports a
broker SDK. Account resolution goes exclusively through the existing
`StrategyAssignment` (`get_account_id(intent.strategy_id)`) — never
through `OrderIntent.account_id`, which is informational/audit metadata
only (by the existing, unmodified `OrderIntent` docstring's own design:
"a strategy can't route around its assignment by setting account_id
itself"). This means broker selection is entirely determined by the
existing assignment, exactly as the target architecture requires.

## 10. Multi-account isolation

`test_two_strategies_on_different_accounts_never_cross_execute` assigns
Strategy A → Account A and Strategy B → Account B, runs both, and asserts
each execution's `account_id` matches its own assignment and that the two
accounts resolve to genuinely different broker instances
(`manager.get_broker("ACC_A") is not manager.get_broker("ACC_B")`).

## 11. Concurrency behavior

- `test_concurrent_run_once_on_the_same_strategy_is_serialized_safely` —
  6 threads calling `run_once()` on the same strategy concurrently; a
  per-strategy `threading.Lock` inside `StrategyRuntime` serializes them,
  and the strategy ends in a single well-defined status with no
  exception.
- `test_concurrent_run_once_on_different_strategies_is_independent` — two
  strategies on two different accounts ticked concurrently from separate
  threads never cross-contaminate each other's execution result.
- The deeper safety net for "same signal submitted twice" is the
  pre-existing, unmodified idempotency store's atomic `claim()` — proven
  by `test_repeated_run_once_with_the_same_signal_does_not_double_execute`
  (a second cycle with the same `idempotency_key` returns the exact same
  cached `order_id`, not a new one).

## 12. Frontend changes

`TccLifecyclePage` (Phase 16.3/16.4) extended with **Mode**, **Runtime**,
and **Last heartbeat** columns, sourced from the extended
`GET /api/strategy-lifecycle` response. Runtime state is rendered as a
distinct badge (`HEALTHY`/`INACTIVE`/`FAILED`) with its own error text
shown separately from the lifecycle/authorization columns — never merged
into one value. **Deliberate, conservative scoping decision**: no
"Run"/"Evaluate" button was added to the frontend this phase — the new
`POST /api/strategy-lifecycle/{id}/evaluate` endpoint exists and is
tested at the API level, but triggering a runtime cycle from the UI is
left for a future phase to consider explicitly, keeping this page's
mutation surface exactly what Phase 16.4 already established (Start/Stop
only). No `BUY`/`SELL`/`PLACE ORDER`/`CLOSE POSITION`/`EXIT POSITION`/
`AUTHORIZE LIVE`/`GO LIVE`/`LIVE EXECUTION` control exists anywhere on
this page, proven by the existing and extended structural test scanning
for all of them.

## 13. Security verification

- `trading/common/strategy_runtime.py` contains no broker SDK import, no
  `smart_api`/`SmartConnect`/`dhanhq`/`breeze_connect` reference, and no
  direct `.place_order(`/`.modify_order(`/`.cancel_order(` call anywhere
  — the only broker-facing call in the entire file is
  `StrategyExecutionEngine.execute()`, structurally verified by
  `test_module_contains_no_broker_sdk_import_or_direct_mutation_call`.
- The module never changes an account's authorization/kill state
  (`test_module_never_sets_authorization_state_or_kills_an_account`,
  `test_no_command_ever_authorizes_an_account_end_to_end`).
- `test_real_broker_place_order_call_count_is_zero_with_a_recording_broker`
  — a recording spy broker proves `place_order`/`modify_order`/
  `cancel_order` call counts are all `0` after a full, successful,
  simulated cycle (dry_run intercepts before the spy's own overrides are
  ever reached).
- No credential, token, or secret is present in any new schema field or
  HTTP response (`test_evaluate_response_never_contains_a_credential_field`).
- No `LiveAuthorization` is created or consumed anywhere in this phase's
  new code.

## 14. Tests

### Backend (targeted, Phase 16.5-specific)
- `tests/common/test_strategy_runtime.py` — 16 tests: unknown strategy,
  inactive no-op, production-strategy-generates-zero-intents, one-shot
  intent reaches a simulated execution, crash → ERROR, non-simulated
  broker refused, mismatched `strategy_id` refused, dry-run boundary
  proof, repeated-signal idempotency, two-account isolation, same-strategy
  and cross-strategy concurrency, 3 structural safety scans, real-broker
  recording-spy test.
- `tests/api/test_execution_routes.py` — 7 new tests for `/evaluate` and
  the extended lifecycle response, plus one pre-existing Phase 16.1
  structural test updated to include the new, legitimate
  `strategy_runtime` field on `ExecutionState`.

### Backend — full regression
Command: `python -m pytest -q`, captured via redirect + `echo "PYTEST_EXIT=$?"`.

First run surfaced exactly one failure:
`test_ready_endpoint_reports_not_configured_by_default` — a **direct,
correct consequence** of the Section 4 fix (the demo accounts now have a
`ShadowBroker()` attached, so the readiness probe reports `"not_connected"`
instead of the old `"not_configured"`, which meant "no broker attached at
all" — no longer true). Renamed and updated the test's assertion and
added an explanatory comment; not a regression, a stale expectation.

```
PYTEST_EXIT=0
```
Result tally after the fix: **1745 passed**, **6 skipped**
(pre-existing/unrelated), **0 failed**, **0 errored**. The ~20
`PytestUnhandledThreadExceptionWarning` lines are the same pre-existing,
documented, benign fake-SmartAPI `orderBook()` polling artifact noted in
every prior phase's regression in this engagement.

### Frontend
- `TccLifecyclePage.test.tsx` — 15 tests (was 13 in Phase 16.4): added
  runtime-state/mode/heartbeat/last-result display, and a FAILED-runtime
  display test; existing fixtures updated for the new required fields.
- Full suite: **63 passed**, 0 failed, 0 errored (9 files).
- `npx tsc --noEmit`: **0 errors**.
- `npm run build`: succeeded.

## 15. Full regression

```
Backend:  1745 passed, 0 failed, 0 errors, 6 skipped   (PYTEST_EXIT=0)
Frontend: 63 passed, 0 failed, 0 errors, 0 skipped      (exit 0)
Frontend build: BUILD_EXIT=0
Frontend typecheck: TSC_EXIT=0
```

## 16. Git commit SHA

Branch: `web-base-algo-trading-control`. Files: this report,
`trading/common/strategy_runtime.py`, `tests/common/test_strategy_runtime.py`,
`trading/api/execution_state.py`, `trading/api/execution_routes.py`,
`trading/api/security/audit.py`, `trading/common/strategy_lifecycle.py`,
`tests/api/test_execution_routes.py`, `tests/test_phase_15d_8_production_hardening.py`,
`frontend/src/pages/TccLifecyclePage.tsx`, `frontend/src/pages/TccLifecyclePage.test.tsx`,
`frontend/src/api/types.ts`. Commit SHA and push status recorded in the
final response below. `docs/phase-15d-11-human-live-canary-review-report.md`
verified untouched and unstaged before committing.

## 17. Production status

**NOT DEPLOYED.** No production host was touched, no deployment command
was run, no environment/credential/risk-limit change was made.

## 18. Live safety status

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED — no `LiveAuthorization` object was
  created, consumed, or referenced by any code added this phase.
- Strategies: STOPPED — no strategy was started by this work outside of
  isolated, per-test fixtures.
- Real broker mutations this phase: **0** (proven structurally, not just
  by absence of a real broker in the test environment — see Section 6).
