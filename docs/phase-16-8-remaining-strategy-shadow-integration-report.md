# Phase 16.8 — Remaining Strategy Integration & Shadow Execution

Status: **PASS** (both remaining strategies integrated; PAPER/SHADOW-only;
zero real broker mutations; every deviation from the legacy source is
documented, none invented).

## 1. Objective

Bring the remaining two strategies (`DoubleStraddelAlgo`,
`Vwap_Algo_Nifty_hedge`) onto the same broker-independent runtime
architecture Phase 16.7 proved with `CombinedVwapNifty`, so all three
registered strategies now generate genuine `OrderIntent`s from real,
ported decision logic when given normalized market data.

## 2. Discovery

Re-confirmed directly against the actual source files (not re-derived
from memory) before writing any code:

### DoubleStraddelAlgo
`config.py`: `LOT_QTY='65'`, `SL_POINTS=25`, `TARGET_POINTS=50`,
`DAILY_MAX_LOSS=20000`, `HEDGE_ENTRY=(10,20)`, `MORNING_ENTRY=(10,25)`,
`MORNING_EXIT=(14,14)`, `AFT_ENTRY=(14,16)`, `FINAL_EXIT=(15,25)`,
`HEDGE_GAP_EXPIRY=500`/`HEDGE_GAP_NONEXPIRY=1000`. `risk/guard.py:52`:
`if mtm <= -abs(config.DAILY_MAX_LOSS): emergency_square_off`. Entry is
purely wall-clock-scheduled, gated on hedge success, no price condition.
Exit per leg is fixed SL/target points, plus two hard time exits.

### Vwap_Algo_Nifty_hedge
`config.py:86`: `qty='195'`. `manager.py` (full file re-read, 151 lines):
hedge entered unconditionally via `rest_func.place_market_order(...,'BUY')`
at line 19, **before** the trading loop even starts. Inside the loop
(exact per-iteration order preserved in the port): timeup check (line
58, `'14:30:00'`) → trigger-cancel (line 63, `close>vwap`) → stoploss
check (line 70, `config.slhit[token]`) → execution/fire (line 82,
`close<triggerlow`) with the exact SL formula (lines 84-90) and a
per-instance 3-entry cap (lines 94-110) → VWAP arm (line 128,
`close<vwap`) → time-based booking (line 135, `'15:25:00'`, buys back any
open position and unconditionally exits the hedge). VWAP itself is
`pandas_ta.vwap` over a real OHLCV candle series (`rest_func.py:106`).

No rule for either strategy was invented or guessed — every threshold,
time, and comparison operator above is quoted directly from the source.

## 3. Strategy integration

### DoubleStraddelAlgo — **A/B: directly integrable with a minimal adapter.**

Ported into `trading/common/strategies/double_straddle.py`: the exact
wall-clock schedule (hedge 10:20, morning entry 10:25, morning exit
14:14, afternoon entry 14:16, final exit 15:25), the exact SL/target
point values, and the exact portfolio kill-switch threshold. An
injectable `clock: Callable[[], datetime]` parameter makes the wall-clock
comparisons deterministically testable without patching global state.

### Vwap_Algo_Nifty_hedge — **A/B: directly integrable with a minimal adapter, one mechanism substitution.**

Ported into `trading/common/strategies/vwap_algo_nifty_hedge.py`: the
exact arm/cancel/fire comparisons, the exact SL formula, the exact
3-entry cap, and the exact timeup/final-exit times. One instance
represents **one side** (CE or PE), mirroring the legacy's own two fully
independent `trademanager()` instances exactly — a caller wanting both
sides registers two separate `VwapAlgoNiftyHedgeStrategy` instances with
different `option_instrument`/`hedge_instrument` values (proven isolated
in Section 9/10's tests).

Neither strategy was blocked (category D) — both had enough deterministic,
self-contained logic to port faithfully, once strike/instrument selection
is treated as pre-configured (Section 5, simplification already
established as acceptable precedent in Phase 16.7).

`CombinedVwapNiftyStrategy` (Phase 16.7) was **not modified** — its full
existing test suite (`tests/common/test_combined_vwap_nifty_strategy.py`,
20 tests) was re-run unchanged and passes exactly as before (Section 9).

## 4. Preserved logic

**DoubleStraddelAlgo** — preserved exactly: hedge-then-straddle sequencing,
all five wall-clock trigger times, the hedge-active gate on straddle
entry, per-leg SL (`entry+25`)/target (`entry-50`), the two time-based
force-exits (morning-only vs. afternoon+hedge), the portfolio
kill-switch's `mtm <= -20000` threshold and its "close everything, stop
the day" response, and the fixed 65-lot quantity.

**Vwap_Algo_Nifty_hedge** — preserved exactly: the unconditional
up-front hedge entry, the arm (`close<vwap`)/cancel(`close>vwap`)/fire
(`close<triggerlow`) sequence and its exact per-iteration ordering, the
three-branch SL formula (`>20`/`10-20`/else`+10`), the 3-entries-per-side
cap, the 14:30 timeup gate, the 15:25 unconditional hedge exit, and the
fixed 195-lot quantity.

## 5. Simplifications / limitations

Every deviation from the legacy source, stated plainly (none hidden):

1. **Strike/instrument selection is not re-derived dynamically** for
   either strategy — hedge and ATM/traded option instruments are
   constructor parameters. (DoubleStraddelAlgo: ATM-from-spot-LTP
   rounding + expiry-day-dependent hedge gap, `expiry.py:36-99`;
   Vwap_Algo_Nifty_hedge: `rest_func.get_hedge_strike`'s expiry-string
   parsing.) Identical precedent to Phase 16.7's CombinedVwapNifty
   simplification.
2. **Wall-clock comparisons use an injectable, naive-local-time
   `clock` callable** (DoubleStraddelAlgo) rather than the legacy's
   IST-aware scheduling — a real deployment must inject a proper
   IST-aware clock; no timezone handling was invented.
3. **The afternoon straddle session reuses the same ATM instruments** as
   the morning session (DoubleStraddelAlgo) — no intraday re-locking,
   consistent with simplification 1.
4. **VWAP is a running simple average of observed prices**
   (Vwap_Algo_Nifty_hedge), not `pandas_ta.vwap`'s real tick-volume-
   weighted computation over an OHLCV series — the normalized single-quote
   market-data model has no intraday volume series to compute a true
   VWAP from. Identical precedent and identical justification to Phase
   16.7's CV proxy for CombinedVwapNifty.
5. **`triggerlow`/`triggerhigh` are approximated as the single observed
   price at arm time** (Vwap_Algo_Nifty_hedge) — no real candle high/low
   exists in a single-quote snapshot. The SL formula itself is unchanged
   and still produces a meaningful, price-dependent value.
6. **Stop-loss detection is client-computed** (`close >= stoploss`,
   Vwap_Algo_Nifty_hedge) instead of the legacy's broker-side stoploss
   order tracked by a separate order-book-polling thread
   (`rest_func.sltracking`). This is a **mechanism** substitution, not an
   invented rule — the stoploss **price** is computed by the exact same
   formula; only how its breach is detected differs, since the
   broker-order-polling infrastructure this integration deliberately does
   not touch is unavailable here.

## 6. Market data

```
MarketDataGateway (Phase 16.6, unchanged)
        |
        v
required_instruments()
   DoubleStraddleStrategy      -> (hedge_ce, hedge_pe, straddle_ce, straddle_pe)  [4]
   VwapAlgoNiftyHedgeStrategy  -> (option_instrument, hedge_instrument)          [2, per side]
        |
        v
StrategyRuntime.run_once() gathers ALL declared instruments (fail-closed, unchanged)
        |
        v
Strategy.generate_order_intents(market_data)
```

Both strategies read `self.get_market_data()` inside their hook, never
import `trading.market_data.providers.*` directly, and have no idea
whether quotes came from a real provider or a test fixture. Per Phase
16.6's own fail-closed rule (never enforced/duplicated here, simply
inherited), if even ONE of a strategy's declared instruments is missing,
stale, or invalid, `generate_order_intents()` is never called for that
cycle at all — proven for both strategies by dedicated no-market-data and
partial-market-data tests.

## 7. OrderIntent

Both strategies use the existing, unmodified
`trading.common.order_intent.OrderIntent` — no new order type was
created. Every intent carries a deterministic `idempotency_key` scoped to
the specific event that produced it (e.g.
`DoubleStraddelAlgo:{instrument}:morning:SL`,
`Vwap_Algo_Nifty_hedge:{instrument}:ENTRY:{entry_count}`), so a genuine
later re-entry/re-exit never collides with a stale cached result, while a
repeated identical evaluation cycle safely replays instead of duplicating.

## 8. Shadow execution

```
OrderIntent
    |
    v
StrategyExecutionEngine.execute()  (unchanged)
    |
    v
PaperBroker / ShadowBroker (Phase 16.5's hard shadow boundary, unchanged)
```

Both strategies proven end-to-end in their own dedicated test files:
`test_end_to_end_hedge_entry_reaches_a_simulated_execution` (both),
`test_end_to_end_arm_fire_reaches_a_simulated_execution` (VwapHedge),
and `test_end_to_end_never_calls_a_real_broker_placeorder` (both, using a
recording-spy `PaperBroker` subclass asserting `place_order_calls == 0`).
No gate order, broker allowlist, or dry-run configuration was touched.

## 9. Risk / idempotency

**RiskManager** remains authoritative for both:
`test_risk_manager_rejects_when_quantity_exceeds_configured_limit`
configures a `max_order_quantity` below the strategy's real quantity (65
/ 195) and confirms every execution is rejected; no limit was loosened.

**Idempotency** reuses the existing store unmodified:
`test_repeated_identical_evaluation_is_idempotent` (both strategies) and
`test_hedge_entry_does_not_refire`/`test_hedge_does_not_reenter_on_subsequent_calls`
confirm a second identical cycle generates zero new intents (state
already advanced, exactly as a real deployed instance would need).

**Kill switch, account authorization, and every other execution gate**:
unchanged; these strategies' intents pass through exactly the same
`execute()` call every other strategy already used.

`CombinedVwapNiftyStrategy`'s own full Phase 16.7 test suite (20 tests)
was re-run unchanged and still passes in full — no regression.

## 10. Tests

### Backend (targeted, Phase 16.8-specific)
- `tests/common/test_double_straddle_strategy.py` — 21 tests: required
  instruments, no-trade (no data, before hedge time, partial data), hedge
  entry (fires, no-refire), morning entry (fires once hedge active, gate
  proof), SL exit, target exit, no-exit-between, morning time exit,
  final exit (closes afternoon+hedge, stops day), kill switch (fires,
  does-not-fire-within-bounds), structural safety, 2 end-to-end pipe
  tests, RiskManager rejection, idempotency, multi-account isolation.
- `tests/common/test_vwap_algo_nifty_hedge_strategy.py` — 21 tests:
  required instruments, no-trade (no data, partial data), hedge entry
  (fires, no-refire), never-armed, arm→fire (exact SL formula value
  checked), cancel-arm, SL exit, no-exit-below-SL, no-arm-after-timeup,
  final exit (with and without an open position), max-entries cap,
  structural safety, 3 end-to-end pipe tests, RiskManager rejection,
  idempotency, multi-account isolation (independent VWAP accumulators
  proven never shared).
- `tests/common/test_strategy_adapters.py`,
  `tests/common/test_phase_16_6_structural_safety.py`,
  `tests/api/test_execution_routes.py`,
  `tests/common/test_strategy_runtime.py` — updated to reflect that all
  three strategies now declare `required_instruments()` (previously only
  CombinedVwapNifty); one Phase 16.5 test's example strategy was swapped
  from `DoubleStraddleStrategy()` (no longer instrument-free) to the
  existing test-only `_OneShotStrategy` stub to keep testing the
  "no-required-instruments" code path in isolation.

### Backend — full regression
```
PYTEST_EXIT=0
```
Result tally: **1839 passed**, **6 skipped** (pre-existing/unrelated),
**0 failed**, **0 errored**. The ~20 `PytestUnhandledThreadExceptionWarning`
lines are the same pre-existing, documented, benign fake-SmartAPI
`orderBook()` polling artifact noted in every prior phase's regression in
this engagement.

## 11. Frontend

No frontend change was required — `TccLifecyclePage` (Phase 16.3-16.7)
already renders whatever strategies `GET /api/strategy-lifecycle`
returns, and all three strategies were already listed there; the page's
existing Mode/Runtime/Market-Data columns now simply show real data-driven
status for two more strategies. Full suite: **66 passed**, 0 failed, 0
errored (9 files, unchanged from Phase 16.7). `npx tsc --noEmit`: **0
errors**. `npm run build`: succeeded.

## 12. Git

Branch: `web-base-algo-trading-control`. Files: this report,
`trading/common/strategies/double_straddle.py`,
`trading/common/strategies/vwap_algo_nifty_hedge.py`,
`tests/common/test_double_straddle_strategy.py`,
`tests/common/test_vwap_algo_nifty_hedge_strategy.py`,
`tests/common/test_strategy_adapters.py`,
`tests/common/test_phase_16_6_structural_safety.py`,
`tests/api/test_execution_routes.py`,
`tests/common/test_strategy_runtime.py`. Commit SHA and push status
recorded in the final response below.
`docs/phase-15d-11-human-live-canary-review-report.md` verified untouched
and unstaged before committing.

## 13. Production

**NOT DEPLOYED.**

## 14. Live safety

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED — no `LiveAuthorization` object was
  created, consumed, or referenced by any code added this phase.
- Strategies: STOPPED — no strategy was started by this work outside of
  isolated, per-test fixtures.
- Real broker mutations this phase: **0**.
