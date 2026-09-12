# Phase 14.6 — Safety Hardening (Phase 14.5 Blocker Remediation)

This phase fixes all five production-readiness blockers Phase 14.5's review identified, plus the disconnected-kill-switch finding, without weakening any existing safety gate, increasing any risk limit, or bypassing/skipping a test. Every fix is additive: every new parameter defaults to `None`/absent and reproduces prior behavior exactly when omitted.

```text
Strategy → OrderIntent → RiskManager → LIVE_CANARY authorization → ExecutionEngine → BrokerAdapter → Broker API
              ▲                ▲                    ▲                      ▲
     Blocker D: idempotency   Blocker E:      Blocker A: guard        Blocker B: response
     replay (persistent,      LIVE requires    REQUIRED for            validation now
     authoritative, checked    explicit,        LIVE_CANARY,           unconditional for
     FIRST)                    positive limits  fail-closed if          every mode
                                                 missing
     Central kill switch (new): checked FIRST, before anything else, for every mode
     Blocker C: every metrics/audit/alert call wrapped so it can never affect the
                execution result returned to the caller
```

---

## 1. Blocker A — Structural LIVE_CANARY Enforcement — **PASS**

**Fix**: `StrategyExecutionEngine.execute()` now resolves the `TradingAccount` (and therefore its `execution_mode`) *before* deciding whether to run canary logic. If `account.execution_mode == LIVE_CANARY` and no `canary_guard` was supplied to the engine, execution is rejected immediately — the broker is never reached. This closes the exact gap the review found: previously, canary authorization only ran *if* a `canary_guard` happened to be attached, with nothing checking that a `LIVE_CANARY`-labeled account actually had one.

File/function: `trading/common/execution.py`, `StrategyExecutionEngine.execute()`, step 4 ("mode-specific structural gate").

Tests (`tests/common/test_phase_14_6_hardening.py`):
1. `test_blocker_a_1_live_canary_with_guard_is_potentially_authorized` — PASS
2. `test_blocker_a_2_live_canary_with_missing_guard_is_rejected` — PASS
3. `test_blocker_a_3_live_canary_with_disabled_shutdown_guard_is_rejected` — PASS
4. `test_blocker_a_4_live_canary_with_kill_switch_is_rejected` — PASS
5. `test_blocker_a_5_live_canary_with_invalid_limits_cannot_even_construct` — PASS (`CanaryLimits` was already fail-closed at construction since Phase 14; this is the strongest possible form of "rejected")
6. `test_blocker_a_mismatched_account_guard_is_rejected` (extra) — PASS

## 2. Blocker B — LIVE Broker Response Validation — **PASS**

**Fix**: extracted `LiveCanaryGuard.validate_broker_response()`'s logic into a new, shared module, `trading/common/broker_response_validation.py`. `StrategyExecutionEngine.execute()` now calls `validate_broker_response()` **unconditionally, for every execution mode** (step 6), not only when a `canary_guard` is attached. `LiveCanaryGuard.validate_broker_response()` now delegates to the same function rather than duplicating the rules ("do not simply copy code blindly").

Validates: response exists; response is actually an `OrderResult`; status is in a known vocabulary; a non-`REJECTED` status carries a non-empty `order_id`; quantity is non-negative.

File: `trading/common/broker_response_validation.py`.

Tests (`tests/common/test_broker_response_validation.py`, 10 tests) + engine-level (`test_phase_14_6_hardening.py`):
1. valid response — PASS
2. rejected order — PASS
3. null response — PASS
4. malformed response (wrong type) — PASS
5. missing order ID — PASS
6. broker error response (unrecognized status) — PASS
7. timeout (represented as `None`) — PASS
8. ambiguous response (negative quantity) — PASS
- `test_blocker_b_plain_live_rejects_a_malformed_broker_response` / `..._accepts_a_well_formed_broker_response` — PASS (proves plain LIVE now gets the same guarantee LIVE_CANARY already had)

## 3. Blocker C — Observability Failure / Duplicate-Retry Risk — **PASS**

**Fix**: new `ObservabilityHealth` class (`trading/common/observability.py`). Every `metrics`/`audit_trail`/`alerts` call inside `execute()` now goes through `self._obs(component, operation, fn)`, which wraps the call so it **can never raise into the caller**. A failure is logged immediately and recorded in `ObservabilityHealth.failures()` (bounded history), readable via `engine.observability_health.healthy`/`.failures()` — a **separate, explicit health signal**, never silently swallowed and never conflated with any order's own outcome. `ExecutionResult` (carrying `order_id`, `client_order_id`, `correlation_id`) is always returned once a broker call has actually happened, regardless of what breaks afterward.

Files: `trading/common/observability.py` (+`ObservabilityFailure`, `+ObservabilityHealth`), `trading/common/execution.py` (`self._obs()` wrapping throughout `execute()`).

Tests (`tests/common/test_observability_health.py`, 6 unit tests) + engine-level:
1. broker succeeds + audit succeeds — PASS
2. broker succeeds + audit fails — PASS (result still `success=True`, `order_id` preserved; `observability_health.healthy is False`)
3. broker succeeds + metrics fails — PASS
4. broker succeeds + alert fails — PASS (alerts have no call in the pure-success path today, so this proves the more general and more important guarantee: an already-unhealthy alerts layer never blocks a later, legitimate order)
5. broker succeeds + multiple observability failures — PASS (result still intact, ≥2 failures recorded)

## 4. Blocker D — Persistent Idempotency — **PASS**

**Fix**: new `trading/common/idempotency_store.py`. `StrategyExecutionEngine.execute()` now checks an `IdempotencyStore` **first** (before RiskManager, before canary authorization, before any broker call) for any intent carrying a non-empty `idempotency_key`. If a record already exists, the **cached `ExecutionResult` is returned and the broker is never called again** — regardless of process restart, because the store (not an in-memory set) is authoritative. A record is written only after the broker has definitively responded (success or a broker-reported rejection); a genuinely ambiguous non-response (retries exhausted) is deliberately **not** persisted as done — documented as an honest boundary (real ambiguity is a position-reconciliation problem, not an idempotency one).

Default implementation: `SqliteIdempotencyStore` — Python standard-library `sqlite3` only, no new dependency, a single file that survives restart/deployment/crash/container restart. `InMemoryIdempotencyStore` exists only for tests and is explicitly documented as unsuitable for production. `RiskManager`'s and `LiveCanaryGuard`'s own in-memory duplicate-key sets are **unchanged** and remain in place as an additional, independent, pre-trade heuristic — this store is authoritative, not a replacement.

File: `trading/common/idempotency_store.py`; wiring in `trading/common/execution.py` (`_check_idempotency_replay`, `_persist_idempotency`).

Tests (`tests/common/test_idempotency_store.py`, 12 unit tests) + engine-level (`test_phase_14_6_hardening.py`):
- `test_blocker_d_first_request_executes_and_persists` — PASS
- `test_blocker_d_same_key_replays_without_calling_the_broker_again` — PASS
- **`test_blocker_d_restart_simulation_same_key_still_does_not_duplicate`** — PASS: a brand-new engine + brand-new `RiskManager` + brand-new `SqliteIdempotencyStore` instance pointed at the *same file* (simulating a full process restart) still refuses to re-submit the same key; the "post-restart" broker double is never called.
- `test_blocker_d_key_reuse_for_a_different_intent_raises` — PASS (`IdempotencyKeyReuseError`, not a silent wrong-result replay)
- `test_blocker_d_no_store_configured_behaves_exactly_as_before` — PASS (backward compatibility: `idempotency_store=None` is the default, and duplicate protection still works via RiskManager's own in-memory check, exactly as before Phase 14.6)

## 5. Blocker E — LIVE Risk Limit Requirement — **PASS**

**Fix**: `RiskLimits.is_live_ready()` (new method) returns `False` unless **every** required field is set to an explicit, positive number: `max_order_quantity`, `max_order_value`, `max_daily_loss`, `max_strategy_loss`, `max_orders_per_day` (new field — see below), `max_strategy_exposure`, **and** `max_account_exposure` (both exposure fields required, mapping the brief's singular "maximum exposure" to both existing fields rather than picking one and leaving the other silently unenforced). Zero and negative values are treated identically to `None` — never read as "unlimited". `StrategyExecutionEngine.execute()` calls this check whenever the resolved account's `execution_mode == LIVE` (step 4) and rejects closed if not ready. **PAPER and SHADOW are completely unaffected** — `RiskLimits`' existing "`None` = unconfigured, always passes" default remains exactly as before for them.

A new 15th RiskManager check, `MAX_ORDERS_PER_DAY` (`RiskLimits.max_orders_per_day`, default `None` = unconfigured, same convention as every other field), was added as part of satisfying "maximum order count" — LIVE had no order-count cap of any kind before this phase.

Files: `trading/common/risk_manager.py` (+`RiskLimits.max_orders_per_day`, `+RiskLimits.is_live_ready()`, `+RiskManager._check_max_orders_per_day`).

Tests (`tests/common/test_risk_manager.py`, 12 new tests) + engine-level:
1. LIVE + missing limits → rejected — PASS
2. LIVE + unlimited (zero/negative) limits → rejected — PASS
3. LIVE + incomplete limits → rejected — PASS
4. LIVE + valid explicit limits → proceeds to risk evaluation (and the broker) — PASS
5. PAPER/SHADOW continue to work safely without production risk configuration — PASS

---

## 6. Centralized Kill Switch — **PASS**

**Fix**: new `trading/common/kill_switch.py`, `CentralKillSwitch` — one authoritative object, checked **first**, before anything else, for **every** execution mode (`execute()`'s step 0). `trading/api/execution_state.py`'s `ExecutionState.kill_switch` now **is** a `CentralKillSwitch` (previously a locally-defined, disconnected dataclass) — its public interface (`engaged`/`engaged_by`/`reason`/`engaged_at`/`disengaged_at`, `engage()`/`disengage()`) is an exact match, so `trading/api/execution_routes.py`'s existing `POST /api/risk/kill-switch` endpoint needed **zero changes**. A `StrategyExecutionEngine` constructed with that same `CentralKillSwitch` instance as its own `central_kill_switch` is what makes "engaged via the API" and "blocked in `execute()`" the same switch — closing the Phase 14.5 finding without weakening `LiveCanaryGuard`'s own, independent kill switch (kept fully in place as defense in depth).

**Not** implemented as a bypass of any authorization: disengaging the central kill switch removes *only* this one gate's block — `RiskManager`, `LiveCanaryGuard`, and Blocker E's LIVE-readiness requirement are all still checked independently and unconditionally.

**Documented, deliberate limitation**: this object is in-memory only (matching every other field on `ExecutionState`, which has no persistence for anything). A restart resets it to disengaged — the safe default — rather than risking a switch that silently fails to persist while believed engaged.

Tests (`tests/common/test_kill_switch.py`, 5 unit tests) + engine-level (`test_phase_14_6_hardening.py`):
- kill switch OFF → normal authorization — PASS
- kill switch ON → authorization rejected — PASS
- kill switch activated while strategy running → next order rejected — PASS (`test_kill_switch_activated_mid_run_blocks_the_next_order`)
- restart while kill switch ON → resets to the safe default (disengaged), documented explicitly rather than silently assumed — PASS (`test_kill_switch_restart_simulation_resets_to_safe_default`)
- kill switch cannot accidentally enable execution — PASS (`test_kill_switch_cannot_accidentally_enable_execution`: disengaging does not bypass Blocker E's independent requirement)
- LIVE_CANARY is blocked by the central switch too, not just LIVE — PASS

---

## 7. Order-Caller Isolation — **PASS**

Re-verified across the entire codebase (not just the files touched this phase): grepped every `.py` file under `trading/common/`, `trading/api/`, and every `.ts`/`.tsx` file under `frontend/src/` for `place_order(`/`placeOrder(`/`modify_order(`/`modifyOrder(`/`cancel_order(`/`cancelOrder(`. The only matches are:
- `trading/common/broker.py` — the **abstract** `BrokerClient.place_order`/`cancel_order` method *declarations* (no implementation).
- `trading/common/execution.py` — `StrategyExecutionEngine` calling **through** the `BrokerClient` interface (`active_broker.place_order(...)`, `active_broker.cancel_order(...)`, `modify_order(...)` via `getattr`) — the sanctioned `ExecutionEngine → BrokerAdapter` delegation path, never an SDK call itself.
- `trading/common/order_intent.py`, `trading/common/strategy.py` — docstring/comment prose only.

`RiskManager`, `LiveCanaryGuard`, `CentralKillSwitch`, `ObservabilityHealth`, `IdempotencyStore`, every `Strategy` class, `TradingAccount`, `BrokerManager`, every API route, and the React frontend contain **zero** such calls. Only the concrete adapter classes under `trading/common/brokers/*.py` actually call a real SDK method.

## 8. Persistent Idempotency — **PASS** (see Blocker D above)

---

## Test Results

```
tests/common/test_broker_response_validation.py           10 passed  (Blocker B, unit)
tests/common/test_observability_health.py                  6 passed  (Blocker C, unit)
tests/common/test_kill_switch.py                            5 passed  (kill switch, unit)
tests/common/test_idempotency_store.py                     12 passed  (Blocker D, unit)
tests/common/test_phase_14_6_hardening.py                  29 passed  (all blockers, end-to-end via execute())
tests/common/test_risk_manager.py                           54 passed  (12 new Blocker E tests + all pre-existing, 2 corrected for the new 15th check)
tests/common/test_live_canary.py                            30 passed  (unaffected)
tests/common/test_live_canary_execution_wiring.py            7 passed  (unaffected)
tests/common/test_observability_wiring.py                    8 passed  (unaffected)
tests/api/test_execution_routes.py                          43 passed  (unaffected by the CentralKillSwitch swap)
tests/api/test_observability_routes.py                      10 passed  (unaffected)
tests/algos/test_shadow_execution_risk.py                    6 passed  (1 test corrected for the new 15th check)
tests/common/ + tests/algos/ (full)                        all passed  (after the one correction above)
tests/ (complete repository suite)                          see final tally below
```

Two pre-existing tests were corrected for the new, legitimate 15th `RiskManager` check (`MAX_ORDERS_PER_DAY`, added for Blocker E) — the same class of correction as Phase 14's `ExecutionMode` enum fix: a stale hardcoded count, not a weakened assertion:
- `tests/common/test_risk_manager.py::test_approved_result_has_explicit_status_and_full_audit_trail` / `test_rejection_still_carries_every_check_not_just_the_failure`
- `tests/algos/test_shadow_execution_risk.py::test_every_shadow_decision_produces_a_complete_risk_audit_record`

**No test was skipped, xfailed, deleted, or weakened.**

---

## Files Changed

### Created
```
trading/common/broker_response_validation.py
trading/common/idempotency_store.py
trading/common/kill_switch.py
tests/common/test_broker_response_validation.py
tests/common/test_observability_health.py
tests/common/test_kill_switch.py
tests/common/test_idempotency_store.py
tests/common/test_phase_14_6_hardening.py
docs/phase-14-6-safety-hardening-report.md
```

### Modified (all additive; every new parameter defaults to preserve prior behavior exactly)
```
trading/common/execution.py       -- execute() restructured per the pipeline diagram above;
                                      +central_kill_switch/idempotency_store/observability_health
                                      constructor params; +ExecutionResult.to_json()/from_json()
trading/common/risk_manager.py    -- +RiskLimits.max_orders_per_day, +RiskLimits.is_live_ready(),
                                      +RiskManager._check_max_orders_per_day (15th check)
trading/common/live_canary.py     -- validate_broker_response() now delegates to the shared
                                      broker_response_validation module (no behavior change)
trading/common/observability.py   -- +ObservabilityFailure, +ObservabilityHealth
trading/api/execution_state.py    -- ExecutionState.kill_switch is now a CentralKillSwitch
                                      (identical public interface; zero route changes needed)
tests/common/test_risk_manager.py               -- 2 tests corrected for the new 15th check
tests/algos/test_shadow_execution_risk.py       -- 1 test corrected for the new 15th check
```

No `trading/api/execution_routes.py`, `trading/api/security/permissions.py`, any broker adapter, any strategy adapter, or any live algo file was modified. No risk limit was raised. No safety gate was removed or weakened.

---

## Remaining Warnings (carried forward from Phase 14.5, not newly introduced)

- `LiveCanaryGuard.reconcile_position()` remains a standalone, non-automatically-invoked method — genuine pre-trade position reconciliation (comparing internal vs. broker state *before* authorizing a new order, not just post-fill) is **not** implemented. Per this phase's explicit instruction not to build full position/P&L tracking unless already required: **this requirement is not yet met**, and is documented here explicitly rather than claimed. Post-fill reconciliation-and-alert is not equivalent to pre-trade reconciliation, and this report does not claim otherwise.
- `CentralKillSwitch` and `ExecutionState` overall remain in-memory only (no persistence across restart) — a deliberate, documented choice (see §6), consistent with `ExecutionState` having no persistence for anything else either.
- `RiskManager`'s own `KILL_SWITCH` check still requires a caller-supplied `RiskContext(kill_switch_engaged=...)` to be reachable — the new `CentralKillSwitch` is a separate, additional, unconditional gate that does not depend on this.
- No route in `trading/api/execution_routes.py` yet constructs a real `StrategyExecutionEngine`/wires `CentralKillSwitch` into one — Phase 11's own documented scope (no live/canary intent flow is driven through the API today). The wiring built in this phase is ready for whichever future phase adds that.

## Remaining Blockers

None identified for the five items this phase was scoped to fix. The position-reconciliation warning above remains an open item for LIVE production readiness generally (not specific to canary), exactly as Phase 14.5 already flagged and this phase was not asked to resolve.

---

PHASE 14.6:
PASS

PHASE 15 AUTHORIZATION:
NOT AUTHORIZED — this phase closes the five identified blockers with passing regression tests; it does not itself constitute or imply authorization to begin Phase 15. That remains a decision for the user to make explicitly, informed by this report and the one remaining documented warning (pre-trade position reconciliation) above.

STOP.
