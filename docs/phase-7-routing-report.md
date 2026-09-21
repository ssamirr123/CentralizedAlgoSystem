# Phase 7 — Multi-Account Routing

```text
Strategy
   ↓
StrategyAssignment
   ↓
TradingAccount
   ↓
BrokerManager
   ↓
BrokerAdapter
```

---

## The TradingAccount Registry

**`BrokerManager` already is the TradingAccount registry** (`register_account`, `get_account`, `accounts()` — Phase 1). Phase 7 does not introduce a second, parallel registry class — that would duplicate an existing, already-tested abstraction. Instead, this phase demonstrates the registry holding **three accounts side by side** and adds the missing piece needed for that to be honest: **broker-level availability**, independent of any individual account's own enabled flag.

```text
ANGEL_MAIN   → broker_id="angelone"      (real adapter, mocked SmartAPI in tests)
DHAN_MAIN    → broker_id="dhan"          (no adapter exists — explicitly deferred)
ICICI_MAIN   → broker_id="icici_breeze"  (existing Phase 0/2 stub — not implemented)
```

`DHAN_MAIN` and `ICICI_MAIN`'s broker types are marked **unavailable** via the new `BrokerManager.set_broker_availability("dhan", False, reason=...)` — no Dhan or ICICI broker adapter was implemented this phase, per your instruction. `DHAN_MAIN`'s registered factory is a placeholder that would raise `NotImplementedError` if ever invoked; tests prove it never is, because the broker-unavailable check rejects the assignment first.

---

## StrategyAssignment's New Fields

`Assignment` (`trading/common/strategy_assignment.py`) now carries exactly the five fields requested:

```python
@dataclass
class Assignment:
    strategy_id: str
    account_id: str
    execution_mode: ExecutionMode
    risk_profile: str = "default"
    enabled: bool = True
```

- **`execution_mode`** defaults to **adopting the account's own execution_mode** rather than a hard-coded value — and `assign()` now validates that an explicitly-requested `execution_mode` **matches** the account's actual mode, rejecting the assignment (`InvalidAssignmentError`) otherwise. This is a genuine, tested safety property: you cannot assign a strategy to an account claiming a different execution mode than the account is actually configured for.
- **`risk_profile`** is a plain string label (`"default"`, `"conservative"`, ...). Phase 7 only **defines** it, as asked — wiring a label to a concrete `RiskLimits` preset (Phase 6) is called out below as future work, not implemented here, to avoid re-opening Phase 6's scope.
- **`enabled`** is the assignment's own on/off switch, independent of the account's own `enabled` flag — `validate()` checks both.

The strategy still never selects a broker: `assignment.assign("DoubleStraddelAlgo", "ANGEL_MAIN")` names only an account_id; `BrokerManager` alone resolves `broker_id="angelone" → AngelOneBroker`.

---

## Validation — All Five Scenarios, Each With a Passing Test

| Scenario | Exception | Test |
|---|---|---|
| Unknown account | `UnknownAccountError` (subclass of `KeyError`) | `test_unknown_account_is_rejected` |
| Disabled account | `InvalidAssignmentError` | `test_disabled_account_is_rejected`, `test_account_disabled_after_assignment_is_caught_by_validate` |
| Unknown broker (account has no client/factory registered at all) | `BrokerConnectionError` ("no BrokerClient and no factory") | `test_unknown_broker_when_account_has_no_client_or_factory` |
| Disabled broker (Dhan/ICICI — adapters not implemented) | `BrokerUnavailableError` (new) | `test_dhan_broker_is_unavailable_and_assignment_is_rejected`, `test_icici_broker_is_unavailable_and_assignment_is_rejected` |
| Invalid strategy/account assignment (execution_mode mismatch) | `InvalidAssignmentError` | `test_invalid_assignment_execution_mode_mismatch` |

`BrokerUnavailableError` (`trading/common/broker_manager.py`) is new, purely additive, and distinct from an individual account being disabled — it's a statement about the **broker type**, not any one account.

---

## Full Chain, Proven End-to-End

`test_strategy_never_selects_a_broker_only_names_itself` and `test_angel_main_resolves_to_angel_one` exercise the complete path: a strategy names only `STRATEGY_ID`; `StrategyAssignment` resolves it to `account_id="ANGEL_MAIN"`; `BrokerManager` resolves that to a real `AngelOneBroker` instance (constructed against a `FakeSmartApi` double — same pattern as `test_angelone_broker.py`, no real network call); the broker connects successfully. `test_no_real_order_api_is_ever_reached_through_the_registry` confirms the underlying fake `placeOrder` mock was never called.

`test_strategy_can_be_reassigned_to_a_different_account_without_code_changes` demonstrates the whole point of this architecture: reassigning `DoubleStraddelAlgo` from `ANGEL_MAIN` to `DHAN_MAIN` (once marked available) is a single `assign()` call — no strategy source touched.

---

## Backward Compatibility

Every pre-existing call site continues to work unmodified:
- `assign(strategy_id, account_id)` (2 positional args) — still works; `execution_mode`/`risk_profile`/`enabled` are new keyword-only parameters with defaults chosen specifically so this exact call produces the same result as before (`execution_mode` adopts the account's current mode, so no manufactured mismatch is possible).
- `get_account_id()`, `has_assignment()`, `remove()`, `validate()`, `get_account()` — unchanged signatures and behavior.
- `BrokerManager.register_account/get_account/get_broker/is_available/accounts()` — unchanged; `set_broker_availability`/`is_broker_available`/`require_broker_available` are additive, and every broker_id defaults to *available* unless explicitly marked otherwise.
- All 26 pre-existing tests across `test_broker_manager.py` and `test_strategy_assignment.py` pass **exactly as written**.

---

## All Current Execution Remains Shadow

Every account registered in this phase's demo has `execution_mode=ExecutionMode.SHADOW` explicitly. `ANGEL_MAIN`'s broker is constructed with `read_only=True` and wired to a `FakeSmartApi` double — structurally incapable of a real network call. `DHAN_MAIN` and `ICICI_MAIN` are both marked broker-unavailable, so no code path can even attempt to construct a working broker for them. **No real order was placed, modified, or cancelled anywhere in this phase.**

---

## Tests

```
python -m pytest tests/common/test_multi_account_routing.py -v
→ 15 passed

python -m pytest tests/common/test_broker_manager.py tests/common/test_strategy_assignment.py -v
→ 26 passed (10 pre-existing + 4 new broker-availability + 16 pre-existing + 10 new assignment-record/broker-unavailable)

python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ 338 passed
```

---

## Safety Verification

- **No real order was placed, modified, or cancelled** — `ANGEL_MAIN` uses a pure-Python `FakeSmartApi` double; `DHAN_MAIN`/`ICICI_MAIN` are both broker-unavailable, so their placeholder/stub factories are never reached.
- **No Dhan or ICICI broker adapter was implemented** — `dhan.py` does not exist; `ICICIBreezeBroker` is the same unmodified Phase 0/2 stub, used as-is.
- **No files under `trading/algos/DoubleStraddelAlgo/*` (beyond the already-existing, untouched-this-phase `broker/execution_bridge.py`), `CombinedVwapNifty/*`, or `Vwap_Algo_Nifty_hedge/*` were modified** — confirmed via `git diff --stat`.

## Files Created
```
tests/common/test_multi_account_routing.py
docs/phase-7-routing-report.md
```

## Files Modified
```
trading/common/broker_manager.py        (+BrokerUnavailableError, set/is/require_broker_available — additive)
trading/common/strategy_assignment.py   (Assignment record: execution_mode/risk_profile/enabled — backward compatible)
tests/common/test_broker_manager.py     (+4 tests)
tests/common/test_strategy_assignment.py (+10 tests)
```
No production/strategy files were modified.

## Recommendation for a Future Phase
Wire `Assignment.risk_profile` (currently a plain label) to a lookup table of named `RiskLimits` presets, so `RiskManager` can select per-assignment limits by profile name instead of one global `RiskLimits` per `RiskManager` instance (Phase 6's current model). This is the natural next step once real per-strategy risk differentiation is needed, and was deliberately left out of both Phase 6 and Phase 7 to keep each phase's scope from bleeding into the other's.
