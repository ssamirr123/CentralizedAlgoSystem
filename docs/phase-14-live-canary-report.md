# Phase 14 — Controlled Live Canary

```text
Strategy -> OrderIntent -> RiskManager -> LIVE_CANARY authorization -> ExecutionEngine -> BrokerAdapter
```

**This report opens with the authorization gate the phase brief itself requires**, because it is the most important fact in this document: building this phase's machinery is not, and does not by itself constitute, authorization to place a live order. Nothing in this phase enables live trading automatically, and no order was placed during this phase's own implementation or verification.

---

## Authorization gate — verified against the 7 stated preconditions

| Precondition | Status | Evidence |
|---|---|---|
| Phase 5A = PASS | **PASS** | `docs/phase-5a-report.md`: `PHASE 5A = PASS` (real Angel One session, all 7 mandatory checks PASS, mutation safety BLOCKED). **Re-verified live, right now, by this phase's own preflight tool** — see below, not just trusted from the historical doc. |
| Phase 5B = PASS | **PASS** | `docs/phase-5b-report.md`: real connected reads + all three mutating calls BLOCKED, exactly as designed. |
| Phase 6 = PASS | **PASS** | `docs/phase-6-risk-report.md`: 309 tests passed, all 14 checks implemented and verified. |
| Phase 7 = PASS | **PASS** | `docs/phase-7-routing-report.md`: 338 tests passed, multi-account routing verified. |
| Phase 8/9 adapters validated | **NOT MET for Dhan/ICICI Breeze** — both phases' own reports explicitly list "Unverified against a real account" as Known Limitation #1. This is not a gap this phase papers over: `trading/preflight/live_canary.py`'s `BROKER ADAPTER VALIDATED` check FAILS closed for `dhan`/`icici_breeze` by design, and only `angelone` (validated in Phase 5A) can ever pass it. |
| Phase 11 backend validated | **PASS** | 43+ dedicated tests passing (`tests/api/test_execution_routes.py`), reused unchanged by this phase. |
| Phase 13 monitoring validated | **PASS** | 46 dedicated tests passing (`tests/common/test_observability*.py`, `tests/common/test_alerts.py`, `tests/api/test_observability_routes.py`), reused unchanged by this phase. |

**Conclusion**: the overall gate is satisfied **only for the AngelOne adapter**. This phase's infrastructure is built in full (as instructed), but its own preflight tool will correctly refuse to authorize a canary on Dhan or ICICI Breeze until a real Phase-5A-equivalent validation is done for those adapters — a decision this phase does not make on its own, and does not need to: the gate enforces it mechanically.

---

## LIVE_CANARY mode

`trading.common.trading_account.ExecutionMode` gains a new member, `LIVE_CANARY`, **distinct from `LIVE`** — both place real orders, but only `LIVE_CANARY` additionally requires every intent to pass `LiveCanaryGuard.authorize()`. Keeping it a separate enum value (rather than a flag layered onto `LIVE`) means every existing `execution_mode == LIVE` check elsewhere in the codebase does not accidentally treat a canary account as unrestricted live trading, and vice versa. The frontend's pre-existing `LIVE_EXECUTION_ENABLED` safety gate (hard-coded `false`, not driven by env — Phase 12) now hides **both** `LIVE` and `LIVE_CANARY` from every execution-mode selector in the UI.

---

## The 12 safety requirements — `trading/common/live_canary.py`

`LiveCanaryGuard` sits between `RiskManager` and `StrategyExecutionEngine`, adding canary-specific checks **on top of** (never instead of) RiskManager's own 14. Like RiskManager, it never calls a broker — it only inspects the `OrderIntent`/caller-supplied numbers and its own internal counters.

| # | Requirement | Implementation |
|---|---|---|
| 1 | Dedicated trading account | `CanaryLimits.account_id`; `_check_dedicated_account` rejects any intent not targeting it |
| 2 | Maximum quantity = tiny limit | `CanaryLimits.max_order_quantity` (no default — must be set) |
| 3 | Maximum order value | `CanaryLimits.max_order_value`, computed from `limit_price` or a caller-supplied `reference_price` |
| 4 | Maximum daily loss | `CanaryLimits.max_daily_loss`, checked against caller-supplied `daily_pnl` |
| 5 | Maximum strategy loss | `CanaryLimits.max_strategy_loss`, checked against caller-supplied `strategy_pnl` |
| 6 | Maximum number of orders | `CanaryLimits.max_orders_per_day`; a per-calendar-day counter incremented **only** on successful authorization (a rejected intent never consumes the budget) |
| 7 | Kill switch | `engage_kill_switch()`/`disengage_kill_switch()`, checked first in `authorize()` |
| 8 | Duplicate-order protection | `_check_duplicate` — same idempotency-keyed-set pattern as `RiskManager`'s own (Phase 6) |
| 9 | Idempotency | `_check_idempotency_present` — **mandatory** for LIVE_CANARY, unlike `RiskManager`'s own optional idempotency key |
| 10 | Broker response validation | `validate_broker_response()` — called by `StrategyExecutionEngine.execute()` **after** the broker responds; rejects an unrecognized status or a non-rejected order with no `order_id` |
| 11 | Position reconciliation | `reconcile_position()` — compares broker-reported vs. expected position, raising Phase 13's `unexpected_position` alert on mismatch |
| 12 | Emergency shutdown | `emergency_shutdown()` — irreversible for that `LiveCanaryGuard` instance; every subsequent `authorize()` call fails closed; resuming requires constructing a fresh instance with a fresh, deliberate `CanaryLimits` |

`CanaryLimits` has **no default values anywhere** — the deliberate opposite of `RiskLimits` (where `None` means "unconfigured, always passes"). A canary with an unconfigured limit is a contradiction: `__post_init__` raises `ValueError` on any non-positive number or empty `account_id`, so a `LiveCanaryGuard` cannot even be constructed without a human consciously choosing every number.

---

## Pipeline wiring — `StrategyExecutionEngine.execute()`

`execute()` gained an optional `canary_guard: LiveCanaryGuard | None = None` constructor parameter (default `None` — zero behavior change for every existing, non-canary caller). When present:

1. Right after `RiskManager.validate()` approves an intent (and only then — RiskManager's rejection still short-circuits everything, exactly as before), `canary_guard.authorize()` runs. A rejection here is recorded as its own `LIVE_CANARY_AUTHORIZATION` audit event and returned as an `ExecutionResult.rejected()`, **before the broker is ever resolved or called**.
2. After the broker responds, `canary_guard.validate_broker_response()` runs before the result is trusted and returned.

`tests/common/test_live_canary_execution_wiring.py` proves this against the same `AngelOneBroker` + fake-SmartAPI-double pattern used throughout this project (this time with `read_only=False` and `trading_mode="live"`, deliberately, to prove the canary gate — not the read-only flag — is what's actually doing the blocking): a canary rejection never reaches `fake.placeOrder`; a canary-authorized tiny order does reach it (the broker adapter remains the only component that ever calls it); the daily order cap is enforced across multiple `execute()` calls; and `RiskManager`'s own kill-switch (via `RiskContext`) and `LiveCanaryGuard`'s kill switch are proven to be two independent, either-one-is-sufficient gates.

---

## `python -m trading.preflight.live_canary`

The final, human-invoked go/no-go command. **Fails closed by construction**: every check defaults to FAIL, and a check that can't be evaluated (e.g. because an earlier one failed) is reported FAIL with "not evaluated," never silently skipped as if it passed.

```
CONFIGURATION                        -- CANARY_ACCOUNT_ID/MAX_ORDER_QUANTITY/MAX_ORDER_VALUE/
                                         MAX_DAILY_LOSS/MAX_STRATEGY_LOSS/MAX_ORDERS_PER_DAY all
                                         set to valid, positive values (constructs a real CanaryLimits)
DEDICATED_ACCOUNT                    -- the canary account_id is not one of the shared example
                                         accounts (ANGEL_MAIN/DHAN_MAIN/ICICI_MAIN)
BROKER ADAPTER VALIDATED             -- BROKER is "angelone" (the only adapter Phase 5A validated
                                         against a real account) -- FAILS closed for dhan/icici_breeze
REAL ACCOUNT VALIDATION              -- re-runs trading.validation.angel_readonly.run_validation()
                                         LIVE, right now (not a historical-report lookup)
RISK MANAGER                         -- constructs a real RiskManager and dry-runs a synthetic,
                                         tiny OrderIntent through validate() (never sent anywhere)
CANARY GUARD DRY RUN                 -- same dry run through LiveCanaryGuard.authorize()
KILL SWITCH                          -- engage/disengage self-test against a throwaway guard
EMERGENCY SHUTDOWN                   -- self-test against a FRESH throwaway guard (irreversible,
                                         so never reuses the dry-run guard from the line above)
OBSERVABILITY WIRED                  -- MetricsRegistry/AuditTrail/AlertManager construct and
                                         exercise cleanly; hash chain verifies
CONTROL CENTER BACKEND IMPORTABLE    -- trading.api.execution_routes/execution_state import cleanly
BROKER ADAPTER IS SOLE ORDER CALLER  -- risk_manager.py / live_canary.py contain no
                                         place_order/placeOrder/modify_order/cancel_order call
                                         (source-level, not just by convention)
NO AUTOMATIC ORDER PLACEMENT         -- this preflight tool itself never IMPORTS a broker adapter
                                         class or SDK (AST-checked, not a string search -- see below)
```

`PHASE 14 = PASS` only when every line above is `PASS`; otherwise `PHASE 14 = FAIL` and the process exits non-zero.

### A note on the "NO AUTOMATIC ORDER PLACEMENT" check's implementation

An earlier draft of this check searched the preflight file's own text for strings like `"place_order("` — which is paradoxical: the check's own source has to contain those exact strings to name what it's looking for, so it always flagged itself. Fixed by checking the file's **parsed AST import statements** instead (the same technique Phase 10's `test_adapter_source_never_imports_a_live_algo_module` already uses) — a real, non-paradoxical guarantee: this file never imports `AngelOneBroker`/`DhanBroker`/`ICICIBreezeBroker`/`SmartConnect`/`BreezeConnect`/`dhanhq`, so it has no object capable of calling a broker order API in the first place, regardless of any env var.

### Demonstrated runs (this session, on this machine)

```
$ python -m trading.preflight.live_canary                       # no CANARY_* set
PHASE 14 = FAIL   (CONFIGURATION, DEDICATED_ACCOUNT, BROKER ADAPTER VALIDATED, ... all FAIL)

$ BROKER=dhan CANARY_ACCOUNT_ID=DHAN_CANARY ... python -m trading.preflight.live_canary
PHASE 14 = FAIL   (BROKER ADAPTER VALIDATED: FAIL -- "dhan" has never been validated)

$ BROKER=angelone CANARY_ACCOUNT_ID=ANGEL_CANARY CANARY_MAX_ORDER_QUANTITY=1
  CANARY_MAX_ORDER_VALUE=200 CANARY_MAX_DAILY_LOSS=500 CANARY_MAX_STRATEGY_LOSS=500
  CANARY_MAX_ORDERS_PER_DAY=3 python -m trading.preflight.live_canary
PHASE 14 = PASS   (every check passed, including a LIVE re-run of Phase 5A's own
                    real-account validation against the credentials already present
                    in this machine's trading/.env)
```

**Transparency note**: the third run above performed a real, live, read-only session against the actual Angel One account already configured on this machine (via `trading.validation.angel_readonly.run_validation()`, Phase 5A's own tool) — the exact same class of action Phase 5A/5B already performed and the user already authorized earlier in this project. No order was placed or could have been (mutation safety is proven BLOCKED as part of that same re-run, exactly as Phase 5A/5B established). This is disclosed here explicitly rather than left implicit, since running a preflight command is expected to be routine and its live-check behavior should never be a surprise.

---

## Files Created
```
trading/common/live_canary.py
trading/preflight/__init__.py
trading/preflight/live_canary.py
tests/common/test_live_canary.py
tests/common/test_live_canary_execution_wiring.py
tests/preflight/__init__.py
tests/preflight/test_live_canary_preflight.py
docs/phase-14-live-canary-report.md
```

## Files Modified (all additive)
```
trading/common/trading_account.py       (+ExecutionMode.LIVE_CANARY)
trading/common/alerts.py                (+ALERT_EMERGENCY_SHUTDOWN, +AlertManager.emergency_shutdown())
trading/common/observability.py         (+EVENT_LIVE_CANARY_AUTHORIZATION)
trading/common/execution.py             (+canary_guard kwarg, authorization + broker-response-validation steps in execute())
frontend/src/components/ExecutionModeSelect.tsx  (LIVE_EXECUTION_ENABLED gate now covers LIVE_CANARY too)
frontend/src/api/types.ts               (ExecutionModeValue += "LIVE_CANARY")
```
No `trading/api/execution_routes.py`, `trading/api/execution_state.py`, or any Phase 11-13 backend/API behavior was changed — Phase 14 is purely a new, optional gate a caller can attach to `StrategyExecutionEngine`; the Phase 11 API never constructs one, so nothing about the control-center backend's existing behavior changed. No production/strategy files were modified.

---

## Tests
```
python -m pytest tests/common/test_live_canary.py -v                     → 30 passed
python -m pytest tests/common/test_live_canary_execution_wiring.py -v    → 7 passed
python -m pytest tests/preflight/ -v                                      → 19 passed
python -m pytest tests/ -q                                                → full repository suite (see final line below)
```

`test_live_canary.py` covers every one of the 12 safety requirements individually, `CanaryLimits`' fail-closed construction, and the alert integration. `test_live_canary_execution_wiring.py` proves the full pipeline order end-to-end against a real `AngelOneBroker` instance (fake SDK double, `read_only=False`, `TRADING_MODE=live` — deliberately, to isolate what the canary gate itself blocks). `test_live_canary_preflight.py` covers every preflight check's PASS/FAIL boundary, including a fully-configured, fully-passing run (with the real-account step mocked, not relying on this machine's actual credentials being present or absent).

---

## Safety Verification

- **No live order was placed at any point during this phase's implementation or testing.** Every test uses a fake SDK double (`MagicMock`); the one real network action (the preflight command's live re-run of Phase 5A's own read-only validation) is read-only and its mutation-safety guard was independently re-confirmed BLOCKED.
- **No order is placed automatically by deployment** — importing any Phase 14 module, running the test suite, or running `python -m trading.preflight.live_canary` never places, modifies, or cancels an order (structurally proven for the preflight tool itself; behaviorally proven for `LiveCanaryGuard`, which never touches a broker).
- **The broker adapter remains the only component allowed to call order APIs** — `RiskManager` and `LiveCanaryGuard` are both source-verified (by the preflight tool itself, and by `tests/common/`'s existing conventions) to contain no `place_order`/`placeOrder`/`modify_order`/`modifyOrder`/`cancel_order`/`cancelOrder` call anywhere.
- **LIVE_CANARY is off by default** — a `TradingAccount` must be deliberately configured with `execution_mode=LIVE_CANARY`, a `LiveCanaryGuard` must be deliberately constructed with deliberately-chosen limits and deliberately passed into `StrategyExecutionEngine`, for any of this phase's checks to run at all. No existing account, test, or code path does this automatically.
- **The frontend cannot select LIVE_CANARY** — gated by the same pre-existing, hard-coded `LIVE_EXECUTION_ENABLED = false` constant as `LIVE`.
- **Phase 8/9's own "unverified" status is respected, not overridden** — the preflight tool's `BROKER ADAPTER VALIDATED` check enforces this mechanically; there is no override flag.

## Known Limitations

1. **Position reconciliation (#11) is not yet called automatically anywhere** — same honest limitation as Phase 13's `record_pnl`/`record_position`: there is no live position feed anywhere in this system yet for it to reconcile against. The method is implemented, tested, and ready for whichever future phase adds real position tracking.
2. **`daily_pnl`/`strategy_pnl` must be supplied by the caller at `execute()` time** — `LiveCanaryGuard` (like `RiskManager`) does not fetch P&L itself; a future phase wiring real P&L tracking would pass it through the same `context`/`daily_pnl`/`strategy_pnl` parameters already threaded through `execute()`.
3. **No canary account is registered anywhere by default** — this phase adds the *capability*; provisioning an actual `ANGEL_CANARY`-style account into `trading/api/execution_state.py`'s registry (or any production wiring) is a deliberate, separate, future decision this phase does not make.
4. **Dhan and ICICI Breeze remain permanently unable to pass preflight** until a real Phase-5A-equivalent live validation is performed for each — this is by design, not an oversight, and this phase does not attempt that validation (it wasn't asked to, and doing so would itself be a live-account action requiring the same deliberate authorization this whole phase is about).

## Recommendation for a Future Phase
Once a human deliberately decides to proceed: (a) provision one real, dedicated Angel One sub-account (or the equivalent) for the canary, distinct from `ANGEL_MAIN`; (b) set the six `CANARY_*` environment variables to genuinely tiny values; (c) run `python -m trading.preflight.live_canary` and confirm `PHASE 14 = PASS`; (d) wire a `LiveCanaryGuard` into exactly one `StrategyExecutionEngine` instance dedicated to that account; (e) start with `max_orders_per_day=1`. None of that is performed by this phase.

STOP after this report — no live order was enabled, automatically or otherwise, at any point.
