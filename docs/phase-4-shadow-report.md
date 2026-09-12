# Phase 4 — Shadow Execution Report

Objective: execute the broker-agnostic architecture end-to-end — `OrderIntent → RiskManager → ExecutionEngine → ShadowBroker → Simulated OrderResult` — without ever being able to reach a real broker API, and compare the result against DoubleStraddelAlgo's existing execution decisions.

**The live trading path was not started, not modified, and not touched at any point.** `broker/orders.py`, `main.py`, `strategy/*`, `risk/guard.py`, `monitor.py`, `config.py`, `connectapi.py`, `websocket_feed.py`, `token_file.py`, and both other algos (`CombinedVwapNifty`, `Vwap_Algo_Nifty_hedge`) have zero diff for this phase (verified via `git diff --stat`).

---

## Architecture Implemented

```text
DoubleStraddelAlgo's actual decisions (representative reconstruction,
process never started)
      ↓
OrderIntent               trading/common/order_intent.py (existing, unchanged)
      ↓
RiskManager                trading/common/risk_manager.py (existing, unchanged)
      ↓
ExecutionEngine            trading/common/execution.py — StrategyExecutionEngine
      ↓                    (extended: ExecutionResult now carries
      ↓                     correlation_id/strategy_id/created_at)
ShadowBroker               trading/common/brokers/shadow_broker.py — NEW
      ↓                    (structurally safe: no SDK import, no network
      ↓                     call, no credential handling anywhere in the file)
Simulated OrderResult      status COMPLETE|OPEN|REJECTED|CANCELLED,
                           positions, average price, realized P&L
```

This is a **separate, additive demonstration harness** (`tests/algos/test_shadow_execution_demo.py`), independent of Phase 3's existing `broker/execution_bridge.py` automatic mirror (which stays on its hard-coded-unconnectable `AngelOneBroker`, completely unchanged — its own 40-test suite passes unmodified). Two different shadow mechanisms now coexist deliberately:
- **Phase 3's mirror** (`execution_bridge.py`): automatic, fires on every real order `orders.py` places, proves wiring, can never connect (empty-credential `AngelOneBroker`).
- **Phase 4's `ShadowBroker`**: a general-purpose, reusable `BrokerClient` implementation for realistic simulation — used here in a standalone demo, available for any future algo/test that needs deterministic fill/reject/partial-fill/P&L simulation.

---

## ShadowBroker

`trading/common/brokers/shadow_broker.py` — implements `BrokerClient` fully:

| Capability | Mechanism |
|---|---|
| Order acceptance | `place_order()` default outcome — instant full fill |
| Order rejection | `configure_next_order(symbol, "REJECTED", reason=...)` — deterministic, test-controlled |
| Fills | Full fill at the limit price (LIMIT) or synthetic quote (MARKET) |
| Partial fills | `configure_next_order(symbol, "PARTIAL", fill_ratio=...)` — leaves the order `OPEN` with a real remainder; `fill_pending_order()` advances it further |
| Order status | `get_order()` — `OPEN`/`COMPLETE`/`CANCELLED`/`REJECTED`, with `filled_quantity`/`remaining_quantity` |
| Positions | `get_positions()` — running weighted-average-price accounting per symbol |
| Average price | Recomputed on every same-direction fill; preserved across a partial close |
| P&L | Realized P&L booked on the closing portion of an opposite-direction fill; `last_price` updated per fill for unrealized tracking |
| Simulated order ID / correlation ID / timestamp / strategy ID / account ID | `order_id` on every `OrderResult`; `correlation_id`/`strategy_id`/`created_at`/`account_id` attached at the `ExecutionResult` level (see "Design Decision" below) |
| Duplicate intent (idempotency) | `simulate_intent(intent)` — a second call with the same non-empty `intent.idempotency_key` returns the cached result instead of creating a second order/position update |

**Safety model — structural, not configuration-based**: unlike `AngelOneBroker` (safe because credentials are empty / `read_only=True` / `TRADING_MODE≠live`), `ShadowBroker` contains **zero** SDK imports, network calls, or credential handling anywhere in the file. There is no configuration that could ever make it reach a real broker — confirmed by a structural test (`test_shadow_broker_has_no_broker_sdk_imports`) that scans the module source for `SmartApi`/`SmartConnect`/`breeze_connect`/`requests`/`socket`/`urllib` and asserts none are present.

### Design decision: where correlation_id/strategy_id/timestamp live

`BrokerClient.place_order()`'s ABC signature (`symbol, side, quantity, order_type, limit_price`) has no parameter for these — changing it would touch every existing adapter's signature (`PaperBroker`, `AngelOneBroker`, the stubs), which is out of scope and unnecessary. Instead, `ExecutionResult` (in `trading/common/execution.py`) — the object `execute()` already returns, and which already has the full `OrderIntent` in scope — was extended with `correlation_id`, `strategy_id`, `created_at` (additive, defaulted, zero impact on existing callers; `account_id` already existed). Every simulated order's `ExecutionResult`, reached through the real `execute()` pipeline, therefore does carry all five required identifiers. `ShadowBroker` additionally exposes `simulate_intent(intent)` for callers who want idempotency-aware simulation directly against the intent.

---

## Run: DoubleStraddelAlgo Through the Architecture in Shadow Mode

`tests/algos/test_shadow_execution_demo.py` runs **8 representative decisions**, reconstructed from `strategy/hedge.py`/`strategy/straddle.py`'s actual logic per the architecture-baseline audit (not a live capture — the process was never started):

| # | Decision | Symbol | Side | Qty | Reason |
|---|---|---|---|---|---|
| 1 | Hedge entry CE | NIFTY19MAY2624700CE | BUY | 65 | HEDGE_ENTRY |
| 2 | Hedge entry PE | NIFTY19MAY2622700PE | BUY | 65 | HEDGE_ENTRY |
| 3 | Straddle entry CE | NIFTY19MAY2623700CE | SELL | 65 | MORNING_ENTRY |
| 4 | Straddle entry PE | NIFTY19MAY2623700PE | SELL | 65 | MORNING_ENTRY |
| 5 | Straddle exit CE | NIFTY19MAY2623700CE | BUY | 65 | SL |
| 6 | Straddle exit PE | NIFTY19MAY2623700PE | BUY | 65 | Target |
| 7 | Hedge exit CE | NIFTY19MAY2624700CE | SELL | 65 | FINAL_EXIT |
| 8 | Hedge exit PE | NIFTY19MAY2622700PE | SELL | 65 | FINAL_EXIT |

### Counts
- **Strategy decisions captured: 8**
- **OrderIntents generated: 8** (1:1, no duplication, no drops — verified by distinct `client_order_id`s)
- **Simulated executions: 8/8 successful** (`ExecutionResult.success == True`, `status == "COMPLETE"`)
- **End-of-day positions: flat** (every symbol's net quantity is 0 after its matching entry+exit — the same invariant the real EOD square-off targets)

---

## Comparison: Existing Execution Decision vs New OrderIntent

Every decision was cross-checked with Phase 3's `compare_with_legacy()` tool (reused, not duplicated) — reconstructing the legacy Angel `placeOrder` params shape from the same raw inputs and diffing against the `OrderIntent`'s own fields:

**Result: 8/8 MATCH.** Symbol, side, order type, and quantity are identical between the legacy representation and the new `OrderIntent` for every decision — the new layer would generate the same trading instruction as the existing Angel-specific implementation, at the *intent construction* level.

### Differences Found (genuine, not cosmetic)

1. **`execute()` does not honor `OrderIntent.limit_price` when actually simulating/placing the order.** `StrategyExecutionEngine.place_limit()` — the method `execute()` calls internally — always computes its own price from `broker.get_quote()` plus configured slippage; it never reads `intent.limit_price` at all. This was *discovered*, not assumed: decision #3 requested `price=125.5`, but the simulated fill used `ShadowBroker`'s synthetic quote (~581) instead. `compare_with_legacy()` still reports a match because it validates the **intent's own fields** (correctly `125.5`), not what the execution engine did with them afterward — these are two different, both-valid checks, and the gap between them is exactly this finding. This is a pre-existing Phase 1 characteristic (`execute()`'s own docstring already flagged it: "does not yet thread intent.limit_price/trigger_price into pricing"), now concretely demonstrated with real numbers for the first time.
2. **Instrument identity is shallow.** `OrderIntent.instrument` (Phase 3) only derives `option_type` from the symbol suffix — no expiry/strike/underlying — a documented, deliberate limitation (avoiding duplicating `strategy/expiry.py`'s positional parsing a second time).
3. **The Angel `symboltoken` survives only as `metadata`**, never a first-class `OrderIntent` field (Phase 2/3 finding, unchanged).
4. **Correlation IDs are per-leg, not per-batch** — the two hedge legs (or two straddle legs) placed "together" don't share a `correlation_id` in this demo, since nothing threads a shared identifier across the two separate `OrderIntent` constructions (a caller could set this explicitly; the demo's representative reconstruction did not).

---

## Tests

```
python -m pytest tests/common/test_shadow_broker.py -v
→ 26 passed

python -m pytest tests/algos/test_shadow_execution_demo.py -v
→ 9 passed

python -m pytest tests/common/ tests/algos/ tests/tools/ -v
→ 231 passed (196 pre-existing + 26 ShadowBroker + 9 demo)
```

Categories covered exactly as requested: BUY, SELL, CE/PE symbol independence, quantity preservation, duplicate intent (idempotency — same key ⇒ one order; distinct keys ⇒ distinct orders; empty key ⇒ never dedupes), rejected order (configured + zero-quantity), simulated fill (MARKET at quote, LIMIT at requested price), partial fill (configured ratio, remainder still `OPEN`, `fill_pending_order()` completes it), and position update (average price on same-direction adds, realized P&L on an opposite-direction close, `last_price` tracking).

---

## Safety Verification

- **No real order was placed, modified, or cancelled.** `ShadowBroker` contains no code path capable of it — confirmed structurally, not just behaviorally.
- **No broker order-placement API was called.** No `SmartApi`, no `SmartConnect`, no network library appears anywhere in `shadow_broker.py` (verified by a dedicated source-inspection test).
- **DoubleStraddelAlgo's live process was never started** — `main.py` was not invoked; the "run through shadow mode" requirement was satisfied via a representative decision set matching the strategy's known logic, not a live capture.
- **No files under `trading/algos/DoubleStraddelAlgo/*` (besides the already-existing, Phase-3-owned `broker/execution_bridge.py`, itself untouched this phase), `CombinedVwapNifty/*`, or `Vwap_Algo_Nifty_hedge/*` were modified** — confirmed via `git diff --stat`.
- **`ExecutionResult`'s extension is additive** — all 196 pre-existing tests across `tests/common/`, `tests/algos/`, and `tests/tools/` pass unmodified.

## Files Created
```
trading/common/brokers/shadow_broker.py
tests/common/test_shadow_broker.py
tests/algos/test_shadow_execution_demo.py
docs/phase-4-shadow-report.md
```

## Files Modified
```
trading/common/execution.py   (+correlation_id, +strategy_id, +created_at on ExecutionResult; additive)
```

## Recommendation for a Future Phase
Thread `OrderIntent.limit_price`/`trigger_price` through `StrategyExecutionEngine.place_limit()`/`place_market_emergency()` so a simulated (and eventually live) execution actually respects the price the strategy/intent specified, rather than always re-deriving one from the live quote. This is the single most consequential gap this phase surfaced — not a ShadowBroker defect, a pre-existing core `execute()` characteristic now concretely evidenced.
