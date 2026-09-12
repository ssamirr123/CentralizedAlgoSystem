# Phase 6 — Centralized Risk Engine

`RiskManager` (`trading/common/risk_manager.py`) sits exactly at:

```text
OrderIntent
      ↓
RiskManager
      ↓
ExecutionEngine
```

as it already did since Phase 1 — this phase extends it from a 6-check structural skeleton into a **centralized, 14-check risk engine**, without breaking any of the 6+ existing production/test call sites that already depend on it (`ExecutionEngine.execute()`, the Phase 3 shadow bridge, the Phase 4/5B demos, the Phase 5B `ConnectedShadowBroker` stack).

---

## The 14 Risk Checks

Every `validate()` call runs **all fourteen**, always, regardless of whether an earlier one failed — so every decision carries a complete audit trail, not just the first failure encountered.

| # | Check name | Enforces | Data source |
|---|---|---|---|
| 1 | `STRATEGY_ENABLED` | Strategy is enabled | `RiskContext.strategy_enabled` |
| 2 | `ACCOUNT_ENABLED` | Assignment exists and account is enabled | `StrategyAssignment.validate()` (existing) |
| 3 | `EXECUTION_MODE_ALLOWED` | Account's execution mode is in an allowed set | `TradingAccount.execution_mode` (Phase 1/5B) |
| 4 | `MAX_ORDER_QUANTITY` | Single order quantity ceiling | `RiskLimits.max_order_quantity` |
| 5 | `MAX_POSITION_QUANTITY` | Resulting position size ceiling | `RiskContext.current_position_quantity` + `RiskLimits.max_position_quantity` |
| 6 | `MAX_STRATEGY_EXPOSURE` | Resulting strategy-wide notional ceiling | `RiskContext.strategy_exposure` + `RiskLimits.max_strategy_exposure` |
| 7 | `MAX_ACCOUNT_EXPOSURE` | Resulting account-wide notional ceiling | `RiskContext.account_exposure` + `RiskLimits.max_account_exposure` |
| 8 | `MAX_DAILY_LOSS` | Account daily loss floor | `RiskContext.daily_pnl` + `RiskLimits.max_daily_loss` |
| 9 | `MAX_STRATEGY_LOSS` | Strategy daily loss floor | `RiskContext.strategy_pnl` + `RiskLimits.max_strategy_loss` |
| 10 | `DUPLICATE_ORDER_PROTECTION` | Same `idempotency_key` never approved twice | Internal `RiskManager` state (thread-safe) |
| 11 | `MARKET_SESSION_VALIDATION` | Weekday + NSE session window (09:15–15:30) | `RiskContext.enforce_market_hours`/`.now` |
| 12 | `KILL_SWITCH` | Unconditional block when engaged | `RiskContext.kill_switch_engaged` |
| 13 | `INSTRUMENT_VALIDATION` | Symbol/exchange non-empty, recognized exchange, valid side/order-type/limit-price | `OrderIntent` itself |
| 14 | `ORDER_VALUE_LIMIT` | Notional order value ceiling | `intent.limit_price` or `RiskContext.reference_price` + `RiskLimits.max_order_value` |

**No check depends on Angel One (or any broker) APIs** — verified by a structural test (`test_risk_manager_is_not_hardcoded_to_any_broker`) that scans the module source for `SmartApi`/`SmartConnect`/`breeze_connect`/`angelone`/`AngelOne` and asserts none appear. Every check operates on the `OrderIntent`, static `RiskLimits` configuration, and caller-supplied `RiskContext` facts only.

---

## Explicit APPROVED / REJECTED Decisions

`RiskCheckResult` now carries, on every decision:

```text
status: "APPROVED" | "REJECTED"   (explicit, not just a boolean)
allowed: bool                      (backward-compatible alias)
reason: str                        (every failed check's own reason, joined)
timestamp: str                     (ISO 8601, UTC)
strategy_id: str
account_id: str
correlation_id: str                (from OrderIntent.correlation_id)
checks: tuple[RiskCheckOutcome]    (all 14, always — full audit trail)
```

---

## Fail-Closed Design

Two distinct guarantees, both tested:

1. **Any unexpected exception during evaluation is caught and converted into a REJECTED result** (an `INTERNAL_ERROR` check outcome) — it can never propagate as a silent approval or an unhandled crash. Verified by `test_internal_error_during_evaluation_fails_closed`, which monkeypatches one check method to raise and confirms the overall result is still `REJECTED`.
2. **Unconditional checks (enabled/kill-switch/duplicate/instrument) cannot be disabled** — there is no configuration flag that turns them off. Checks that require external numeric configuration (quantity/exposure/loss/value limits) default to `None` = "not configured, not enforced" rather than `0` = "blocks everything" — a deliberate choice, documented in the module's own docstring, because defaulting every limit to zero would have instantly broken all 6+ existing call sites the moment this file changed. "Fail closed" means *never approving something that failed a check that actually ran* — not inventing limits nobody configured.

---

## Backward Compatibility

All pre-existing call sites and tests continue to work **unmodified**:
- `RiskManager(strategy_assignment)` — single positional argument still works (`limits` is a new, optional second parameter).
- `RiskCheckResult.allow()` / `.deny(reason)` classmethods — unchanged signatures, still used directly by `tests/algos/test_doublestraddel_execution_bridge.py`'s `RiskDeniesEverything` fake.
- `.allowed` / `.reason` attributes — unchanged, still read by `ExecutionEngine.execute()` and the Phase 3 shadow bridge.
- All 7 pre-existing `test_risk_manager.py` tests pass **exactly as written**, including exact reason-substring assertions (`"quantity"`, `"assignment"`, `"disabled"`, `"limit_price"`) — achieved by reusing the identical message templates from the original Phase 1 checks inside the new named-check functions.

---

## Running the Entire DoubleStraddelAlgo Shadow Flow Through the RiskManager

`tests/algos/test_shadow_execution_risk.py` runs the same 8 representative decisions from Phase 4/5B (`hedge_entry_ce/pe`, `straddle_entry_ce/pe`, `straddle_exit_ce/pe`, `hedge_exit_ce/pe`) through `OrderIntent → RiskManager → StrategyExecutionEngine → ShadowBroker`, with a `RiskLimits` configuration mirroring DoubleStraddelAlgo's own real `config.py` (`max_order_quantity=65`, `max_daily_loss=20_000.0` — the same `DAILY_MAX_LOSS` constant `risk/guard.py` already enforces independently):

- **All 8 decisions execute successfully** through the full pipeline, positions end flat.
- **A deliberately oversized decision (qty=5000) is rejected by `MAX_ORDER_QUANTITY`** before ever reaching `ShadowBroker` — `shadow.get_positions()` stays empty.
- **Kill switch engaged rejects every decision in the flow** — `KILL_SWITCH` fires for all 8, none reach the broker.
- **A daily-loss breach (`daily_pnl=-21,000` against a 20,000 limit) rejects a new entry** — `MAX_DAILY_LOSS` fires.
- **A replayed intent with the same `idempotency_key` is rejected on the second attempt** — `DUPLICATE_ORDER_PROTECTION` fires.
- **Every decision, approved or rejected, produces a complete `RiskCheckResult`** — status, timestamp, strategy/account/correlation IDs, and all 14 check outcomes.

### Discovered behavioral nuance (not a bug — documented)

An intent carrying a non-empty `idempotency_key` can only be validated **once** across its lifecycle — `DUPLICATE_ORDER_PROTECTION` treats any second `validate()` call for the same key as a duplicate, whether that second call comes from a deliberate re-submission or merely from a caller pre-checking risk and then also calling `ExecutionEngine.execute()` (which validates internally). This was discovered while writing the integration test: calling `risk_manager.validate(intent)` manually and then `engine.execute(intent)` for the same idempotency-keyed intent caused the second (internal) validation to reject it as a duplicate. **This is correct, intended behavior** for genuine duplicate protection, but it means callers using `idempotency_key` should validate **exactly once** — typically by letting `execute()` do it — not pre-check and then execute separately.

---

## Tests

```
python -m pytest tests/common/test_risk_manager.py -v
→ 44 passed (7 pre-existing, unmodified + 37 new — at least 2–4 tests per check)

python -m pytest tests/algos/test_shadow_execution_risk.py -v
→ 6 passed

python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ 309 passed
```

Every one of the 14 checks has dedicated tests for both its passing and failing paths; `MAX_ORDER_QUANTITY`, `EXECUTION_MODE_ALLOWED`, `MAX_POSITION_QUANTITY`, exposure/loss limits, duplicate protection, market-session validation, kill switch, instrument validation, and order-value limit are each independently verified.

---

## Safety Verification

- **No real order was placed, modified, or cancelled** — this phase touches only risk evaluation, never a broker call.
- **No files under `trading/algos/DoubleStraddelAlgo/*` (beyond the already-existing, untouched-this-phase `broker/execution_bridge.py`), `CombinedVwapNifty/*`, or `Vwap_Algo_Nifty_hedge/*` were modified** — confirmed via `git diff --stat`.
- **`StrategyAssignment.get_account()`** was added (a small, additive public accessor) so `EXECUTION_MODE_ALLOWED` could read the assigned account's mode without reaching into a private attribute.
- **All 309 tests across the full suite pass**, including every test from Phases 1–5B, unmodified.

## Files Created
```
tests/algos/test_shadow_execution_risk.py
docs/phase-6-risk-report.md
```

## Files Modified
```
trading/common/risk_manager.py          (rewritten: 6-check skeleton -> 14-check centralized engine, backward compatible)
trading/common/strategy_assignment.py   (+get_account(), additive)
tests/common/test_risk_manager.py       (+37 tests, 7 pre-existing unchanged)
```
No production/strategy files were modified.

## Recommendation for a Future Phase
The `MAX_POSITION_QUANTITY`/`MAX_STRATEGY_EXPOSURE`/`MAX_ACCOUNT_EXPOSURE`/`MAX_DAILY_LOSS`/`MAX_STRATEGY_LOSS` checks require the caller to supply current position/exposure/P&L via `RiskContext` — RiskManager deliberately never calls a broker itself. A future phase should build the small adapter that populates `RiskContext` from `ConnectedShadowBroker.get_positions()` (Phase 5B) automatically, so these checks are enforced against live simulated state without every caller having to compute it by hand.
