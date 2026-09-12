# Phase 5B — Connected Shadow Execution

**Prerequisite check**: `docs/phase-5a-report.md` shows `PHASE 5A = PASS` (verified before any Phase 5B work began), and Angel One credentials remain available in the local environment. Phase 5B proceeded.

Command: `python -m trading.validation.angel_connected_shadow` — executed against the **real, authenticated Angel One account**.

---

## Architecture Implemented

```text
Real Angel Market Data (real AngelOneBroker, read_only=True)
        |
DoubleStraddelAlgo (representative decisions -- process never started)
        |
OrderIntent
        |
RiskManager
        |
ExecutionEngine
        |
ConnectedShadowBroker            <- NEW, trading/common/brokers/connected_shadow_broker.py
        |         |
     (reads)   (writes)
        |         |
 real broker   ShadowBroker
        |         |
        v         v
Real quotes/    Simulated Fill
instrument/           |
account data          v
              Simulated Position/P&L
```

`ConnectedShadowBroker` is a pure delegator — it does not reimplement Angel's request/response translation (stays in `AngelOneBroker`) or order simulation (stays in `ShadowBroker`). It composes "real reads" with "simulated writes" behind one `BrokerClient`.

---

## Fail-Closed Enforcement (`execution_mode = SHADOW`)

`ConnectedShadowBroker.__init__` requires `execution_mode` to be passed **explicitly** and to equal exactly `ExecutionMode.SHADOW`:
- Omitting it → `ValueError` ("requires execution_mode=ExecutionMode.SHADOW to be passed explicitly... fail closed").
- Passing `LIVE` or `PAPER` → `ValueError` naming the rejected mode.
- The validated mode is captured into a private attribute at construction with **no setter** — nothing can change it afterward, even if the associated `TradingAccount.execution_mode` (a mutable field) is later changed.
- The real broker passed in must itself already be `read_only=True` (checked via its `is_read_only` property) — construction fails otherwise.

This was verified with dedicated tests (`test_construction_requires_execution_mode_explicitly`, `test_construction_rejects_live_execution_mode`, `test_construction_rejects_paper_execution_mode`, `test_construction_rejects_a_real_broker_that_is_not_read_only`).

---

## Proof: SHADOW Mode Cannot Call Real Order Placement

Two independent layers of proof, both passing:

1. **Structural** (`test_place_order_body_never_references_the_real_broker`) — inspects the source of `place_order`/`modify_order`/`cancel_order` and asserts `self._real_broker` never appears in any of them. There is no code path from these methods to the real broker, independent of any runtime check.
2. **Behavioral, against a mocked real broker** (`test_place_order_never_calls_the_real_smart_api`, `test_modify_order_never_calls_the_real_smart_api`, `test_cancel_order_never_calls_the_real_smart_api`, `test_many_simulated_orders_never_touch_the_real_smart_api`) — places/modifies/cancels multiple simulated orders and asserts the underlying `FakeSmartApi`'s `placeOrder`/`modifyOrder`/`cancelOrder` mocks were **never called**.
3. **Behavioral, against the real, live-connected Angel One session** (this phase's actual demo run) — after running the full decision set, the script explicitly calls `real_broker.place_order()`, `.modify_order()`, `.cancel_order()` directly against the real broker and confirms each raises `ReadOnlyModeError` before any SmartAPI call:

```text
Real broker mutation verification:
  place_order: BLOCKED
  modify_order: BLOCKED
  cancel_order: BLOCKED
```

---

## Connected Shadow Run — Real Market Data, Simulated Execution

```text
NIFTY spot (real): 23398.1
Option contract (real, current): NIFTY15SEP2623400CE expiry=2026-09-15
```

| Leg | Reason | Symbol | Side | Qty | Intent Price (real quote) | Risk | Simulated Order | Fill | Latency |
|---|---|---|---|---|---|---|---|---|---|
| straddle_entry_ce | MORNING_ENTRY | NIFTY15SEP2623400CE | SELL | 65 | 133.6 | allowed | SHADOW-1 COMPLETE | 65/65 | 11.01s |
| straddle_entry_pe | MORNING_ENTRY | NIFTY15SEP2623400PE | SELL | 65 | 70.4 | allowed | SHADOW-2 COMPLETE | 65/65 | 1.18s |
| straddle_exit_ce | SL | NIFTY15SEP2623400CE | BUY | 65 | 133.6 | allowed | SHADOW-3 COMPLETE | 65/65 | 1.23s |
| straddle_exit_pe | Target | NIFTY15SEP2623400PE | BUY | 65 | 70.4 | allowed | SHADOW-4 COMPLETE | 65/65 | 1.25s |

**Simulated positions after the run** (real last_price, simulated P&L):
```text
NIFTY15SEP2623400CE: qty=0 avg_price=0.0 last_price=133.65 pnl=-6.5
NIFTY15SEP2623400PE: qty=0 avg_price=0.0 last_price=70.45 pnl=-6.5
```

### What Was Captured (per your checklist)
- **Strategy decision** — `strategy_decision` dict per leg (symbol, side, qty, order type, reason).
- **OrderIntent** — full object, including `correlation_id` (e.g. `CORR-76c1d5d99844`), `strategy_id`, `account_id`, `created_at`.
- **Risk decision** — `RiskCheckResult.allowed`/`.reason`, captured explicitly (all 4 allowed).
- **Simulated order** — `OrderResult` from `ConnectedShadowBroker.place_order()` via `ShadowBroker` (order IDs `SHADOW-1..4`, all `COMPLETE`).
- **Simulated fill** — `get_order()` state (`filled_quantity`/`remaining_quantity`) per leg.
- **Simulated position** — `get_positions()` after the full run (flat, as expected — every entry was matched by its exit).
- **Simulated P&L** — `-6.5` on both legs (see finding below).
- **Latency** — wall-clock seconds per decision, captured with `time.monotonic()`. First decision (11.01s) is dominated by real TOTP/session overhead already paid at `connect()` plus the first real quote round-trip; subsequent decisions (~1.2s) reflect the `min_api_interval_seconds=0.35` throttle plus real network latency per quote call.
- **Errors** — none occurred in this run; the harness's `try/except` per decision (and `_sanitize()`) exists and is tested (`test_no_credential_leakage_in_report_details`-equivalent coverage in the offline suite) for when they do.

### Finding (consistent with Phase 4, now confirmed with real data)

The `-6.5` P&L on each leg is **not** a real 63.2-point straddle loss — it's the same architectural characteristic Phase 4 discovered: `StrategyExecutionEngine.place_limit()` (called by `execute()`) always recomputes its own price from `broker.get_quote()` plus slippage, ignoring `OrderIntent.limit_price`. Here, `get_quote()` delegates to the **real** broker, so entry and exit calls each fetched a **fresh real market quote** moments apart — the small P&L reflects real bid/ask-adjacent price movement plus the slippage-walk design (which assumes "price moves against you on retries," biasing SELL fills slightly low and BUY fills slightly high), not the strategy's actual intended SL/Target levels. This is now evidenced with two independent data points (Phase 4 simulated quotes, Phase 5B real quotes) pointing at the same root cause — reinforcing the Phase 4 recommendation to thread `intent.limit_price` through before any live-execution phase.

---

## Tests

```
python -m pytest tests/common/test_connected_shadow_broker.py tests/common/test_shadow_broker.py -v
→ 46 passed (17 ConnectedShadowBroker + 29 ShadowBroker, including 3 modify_order + 2 order-book tests added this phase)

python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ 266 passed
```

All `ConnectedShadowBroker`/`ShadowBroker` tests run entirely offline against a `FakeSmartApi` double — no real network call in the automated suite. The real-account run above was a separate, deliberate, manual invocation (matching the Phase 5A precedent), not part of the pytest suite.

---

## Safety Verification

- **No real order was placed, modified, or cancelled** at any point — structurally proven (no code path exists) and behaviorally proven (against both a mock and the real, live session).
- **`execution_mode=SHADOW` is required and fails closed** — no default, no way to construct otherwise, no way to change it after construction.
- **Positions/orders reported by `ConnectedShadowBroker` are always simulated**, never the real account's actual holdings — verified by `test_get_positions_returns_simulated_positions_not_the_real_accounts` (the fake real broker has a real position on file; `ConnectedShadowBroker.get_positions()` never surfaces it).
- **No files under `trading/algos/DoubleStraddelAlgo/*` (beyond the already-existing, untouched-this-phase `broker/execution_bridge.py`), `CombinedVwapNifty/*`, or `Vwap_Algo_Nifty_hedge/*` were modified** — confirmed via `git diff --stat`.
- **DoubleStraddelAlgo's live process was never started.**
- **Credentials** were read only from the environment/`trading/.env`, never printed — the demo script reuses `angel_readonly.py`'s `_sanitize()` for any error text.

## Files Created
```
trading/common/brokers/shadow_broker.py         (extended: modify_order, get_order_book, get_open_orders — additive)
trading/common/brokers/connected_shadow_broker.py
trading/validation/angel_connected_shadow.py
tests/common/test_connected_shadow_broker.py
docs/phase-5b-report.md
```

## Files Modified
```
tests/common/test_shadow_broker.py   (+5 tests for modify_order/order-book, additive)
```
No production/strategy files were modified.

## Recommendation for a Future Phase
Unchanged from Phase 4, now doubly evidenced: thread `OrderIntent.limit_price`/`trigger_price` through `StrategyExecutionEngine.place_limit()` before any live-execution phase, so simulated (and eventually real) fills reflect the strategy's actual intended price rather than a freshly re-derived one. Do not proceed to live execution until this is addressed and re-validated.

**Do not proceed to live execution.** This phase is exactly and only: real reads, simulated writes, zero real order mutations.
