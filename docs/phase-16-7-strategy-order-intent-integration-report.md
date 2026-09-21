# Phase 16.7 — Strategy Signal & OrderIntent Integration

Status: **PASS** (exactly one strategy integrated; PAPER/SHADOW-only;
zero real broker mutations; two honest, documented simplifications — no
invented rules).

## 1. Objective

Integrate ONE real existing strategy's actual decision logic with
normalized market data (Phase 16.6) so it produces genuine
broker-independent `OrderIntent`s, flowing through the unmodified Phase
16.5 execution path into PAPER/SHADOW — proving real signal generation,
not just architecture.

## 2. Strategy discovery

A dedicated research pass read the actual decision-logic files (not the
broker/order-placement files) of all three legacy algos — full
line-by-line findings are recorded in the delegated research transcript
this report is drawn from; summarized per strategy:

### DoubleStraddelAlgo
Purpose: time-scheduled hedged short-straddle (buy far-OTM hedge, sell
ATM CE+PE twice daily). Market data: NIFTY spot LTP + specific option
LTPs via tick/REST, no candles. No indicator computed — only ATM
strike-rounding math. Entry: pure wall-clock trigger gated by hedge
success (no price condition). Exit: fixed SL/target points, plus
wall-clock and portfolio-max-loss kill switch. **Integration surface**:
~469-598 lines across 4 files (`engine.py`/`straddle.py`/`hedge.py`/
`expiry.py`), inseparable from wall-clock scheduling, JSON-file
crash-recovery state, and scrip-master-based expiry-day detection.

### CombinedVwapNifty
Purpose: single ATM short-straddle whose entry timing is gated by a
"Combined Premium (CP) vs Combined VWAP (CV)" 2-state arm/fire machine,
with a 3-level combined-loss risk ladder. Market data: 1-minute OHLC
candles for the locked ATM CE/PE, built from live ticks. Indicator: CP =
CE_close + PE_close; CV = cumulative volume-weighted typical price of the
combined series (`make_cp_cv`). Entry: `WAIT_ARM→ARMED` on `cp > cv`;
`ARMED→fire` on `cv > cp`. Exit: combined realized+unrealized loss vs
`[650, 1300, 2000] * num_lots`; levels 0/1 exit the bigger-loser leg,
level 2 exits both and stops the day. **Integration surface**: ~366 lines,
almost entirely centralized in one file (`manager.py`), state is scalar
numbers (entry price, cumulative loss, last LTP) rather than
candle-series objects.

### Vwap_Algo_Nifty_hedge
Purpose: per-strike (CE/PE run as two independent instances) hedged
short-option strategy — VWAP-cross arms a trigger, a subsequent
breakdown below the armed candle's low fires a short entry with a
computed SL. Market data: 1-minute OHLC candles per single option token,
evaluated only on odd minutes. Indicator: VWAP via `pandas_ta`. Entry/exit
logic is one long sequential `if`-chain over ~11 local mutable flags, an
odd-minute evaluation cadence, and a broker-side (not client-computed)
stop-loss whose fill is detected only via a separate order-book-polling
thread writing into a global dict. **Integration surface**: nominally one
function (~109 lines) but the least deterministic/cleanest of the three
in practice.

No rule for any of the three was invented or guessed — everything above
is drawn directly from the actual source files' comparison operators and
formulas.

## 3. Selected strategy

**Selected: CombinedVwapNifty.**

**Reason, based strictly on repository evidence**: its entry rule is an
explicit, named 2-state machine driven by two plain scalar numbers per
cycle (`cp`, `cv`), and its exit rule is a small set of scalar comparisons
against three fixed thresholds — both require only `entry_price`,
`last_ltp`, and a running `cum_loss` per leg, none of which need a
candle-series object or cross-thread signaling to evaluate. By contrast,
DoubleStraddelAlgo's logic (while individually simple) is scattered
across four files and is inseparable from wall-clock scheduling and
JSON-file crash-recovery state; Vwap_Algo_Nifty_hedge's "one function" is
actually the least deterministic of the three — an ~11-flag sequential
`if`-chain with an odd-minute evaluation cadence and a broker-side
stop-loss whose fill is only known via a separate polling thread writing
into a global dict. CombinedVwapNifty has the smallest, most numeric, most
easily and honestly reproducible integration surface.

**Required market data**: CE and PE last-traded price (normalized as
`OptionQuote`).
**Existing signal logic**: `manager.py:84,88` arm/fire comparisons;
`manager.py:210-246` combined-loss ladder; thresholds `config.py:129`.
**Existing order-generation capability**: `enter_leg`/`exit_leg` call
`rest_func.execute_limit_order` — the SELL/BUY decision points map
directly onto where this phase's ported code now builds an `OrderIntent`
instead.
**Missing integration pieces** (see Section 5 for why, in detail):
tick-volume-weighted VWAP (data-model limitation, not a rule change);
fully independent per-leg re-entry/cooldown state machine (deferred, not
invented).

`DoubleStraddleStrategy` and `VwapAlgoNiftyHedgeStrategy` are completely
untouched this phase and remain registered, safe, and zero-intent.

## 4. Market-data integration

```
MarketDataGateway (Phase 16.6, unchanged)
        |
        v
CombinedVwapNiftyStrategy.required_instruments() -> (ce_instrument, pe_instrument)
        |
        v
StrategyRuntime.run_once() gathers both via gather_market_data() (fail-closed, unchanged)
        |
        v
CombinedVwapNiftyStrategy.generate_order_intents(market_data)
```

`required_instruments()` declares exactly the two option legs it needs —
nothing broader (no full option chain, no historical candle request). The
strategy reads `self.get_market_data()` inside its hook, never imports
`trading.market_data.providers.*` directly, and has no idea whether the
quotes came from a real provider or a test fixture.

## 5. Signal generation

**Ported exactly** (same comparison operators, same threshold values):
- Entry: `cp = ce.ltp + pe.ltp`; `WAIT_ARM → ARMED` when `cp > cv`;
  `ARMED → fire` (enter both legs) when `cv > cp` — identical to
  `manager.py:84,88`.
- Exit: `RISK_LOSS_LEVELS = (650.0, 1300.0, 2000.0)` (identical to
  `config.py:129`); combined loss = sum of per-leg
  `max(0, (ltp - entry_price) * quantity)` plus realized loss from prior
  exits (identical formula to `manager.py:184,187-193`); levels 0/1 exit
  only the bigger-loser leg and advance the ladder by exactly one level
  per breach (matching the legacy's own one-level-per-evaluation
  ratcheting, `manager.py:223-245`); the final level exits both legs and
  sets a permanent `day_stopped` flag (`manager.py:226-236`).

**Two documented, honest simplifications** (not invented rules — data-
model and scope limitations, stated plainly):
1. **CV** is computed here as a running simple average of `cp` across
   evaluation cycles, not the legacy's tick-volume-weighted cumulative
   VWAP (`rest_func.make_cp_cv`). The Phase 16.6 normalized quote
   (`OptionQuote`) carries a last-traded price, not an intraday volume
   series — fabricating a fake volume-weighted computation from data that
   doesn't exist would misrepresent real market behavior. The RULE
   (arm on `cp>cv`, fire on `cv>cp`) is exact; only the CV number's
   derivation is a stated proxy.
2. **Entry acts on both legs together** as one straddle entry. The
   legacy's fully independent per-leg re-entry/cooldown state machine
   (`can_reenter()`, `REENTRY_COOLDOWN_SECONDS`) was not ported —
   deferred, not fabricated. Once entered, a leg only re-enters after the
   caller constructs a fresh strategy instance (no re-entry logic exists
   yet in this integration).

No entry/exit condition, threshold, or sizing value was invented — the
`quantity=65` default is documented as a placeholder matching NIFTY's
real lot size, not the legacy's dynamic lot-size lookup (`rest_func.setup_straddle`),
since strike-locking/lot-size resolution from a live spot LTP was not
part of this integration's scope.

## 6. OrderIntent generation

Uses the existing, unmodified `trading.common.order_intent.OrderIntent` —
no `StrategyOrder`/`BreezeOrder`/etc. was created. Entry intents:
`side=SELL`, `order_type=MARKET`, `quantity` (configured), deterministic
`idempotency_key` (`f"{strategy_id}:{instrument}:ENTRY:{entry_sequence}"`,
where `entry_sequence` increments on every exit so a genuine later
re-entry gets a fresh key rather than colliding with a stale one). Exit
intents: `side=BUY`, similarly keyed
(`f"...:EXIT:{risk_level_index}:{entry_sequence}"`). `account_id` is
supplied by the strategy instance's own constructor parameter (a
documented simplification — real per-assignment account resolution at
intent-construction time is a `StrategyAssignment` concern for a future
phase; `OrderIntent.account_id` remains informational only, per its own
docstring — `execute()` resolves the authoritative account via
`StrategyAssignment`, never trusting this field).

## 7. Shadow execution

```
OrderIntent
    |
    v
StrategyExecutionEngine.execute()  (Phase 1-16.5, byte-for-byte unchanged)
    |
    v
PaperBroker / ShadowBroker (Phase 16.5's hard shadow boundary, unchanged)
```

Proven end-to-end in `tests/common/test_combined_vwap_nifty_strategy.py`:
`test_end_to_end_entry_reaches_a_simulated_shadow_execution` feeds a real
arm→fire price sequence through `StrategyRuntime.run_once()` and asserts
2 successful simulated executions; `test_end_to_end_never_calls_a_real_broker_placeorder`
uses a recording-spy `PaperBroker` subclass and asserts
`place_order_calls == 0` (dry_run intercepts before it, exactly as in
Phase 16.5). No gate order, broker allowlist, or dry-run configuration
was touched — this strategy is simply a new, real caller of the identical
unmodified pipeline.

## 8. Risk / idempotency

**RiskManager** remains fully authoritative and untouched:
`test_risk_manager_rejects_when_quantity_exceeds_configured_limit`
configures `RiskLimits(max_order_quantity=10)` against the strategy's
real `quantity=65` and confirms every execution is rejected;
`test_risk_manager_allows_a_valid_intent_in_shadow_mode` confirms a
correctly-configured limit lets a valid intent through — no risk limit
was loosened to make the strategy pass.

**Idempotency** reuses the existing store unmodified:
`test_repeated_run_once_with_identical_market_data_is_idempotent_end_to_end`
feeds the exact same fire sequence twice — the second evaluation
generates zero new intents (the strategy's own `leg.in_position` state
already prevents re-entry, the same fail-closed behavior a real deployed
instance would need). `test_repeated_identical_evaluation_after_entry_does_not_reenter`
proves this at the strategy level directly.

**Kill switch, account authorization, and every other execution gate**:
unchanged; this strategy's intents pass through exactly the same
`execute()` call every other strategy already used in Phase 16.5/16.6.

## 9. Tests

### Backend (targeted, Phase 16.7-specific)
- `tests/common/test_combined_vwap_nifty_strategy.py` — 20 tests:
  `required_instruments()`, 4 no-trade scenarios (no data, partial data,
  never-armed, armed-but-never-fired), real arm→fire entry (both legs,
  correct fields), no-duplicate-re-entry, 3 exit-ladder tests (below
  threshold, level-0 bigger-loser-leg, final-level both-legs+day-stop),
  legacy-threshold-value proof, structural safety scan, 3 end-to-end
  pipe tests (successful execution, zero-signal zero-execution, real-
  broker-safety recording spy), 2 RiskManager tests, 1 idempotency test,
  2 multi-instance isolation/concurrency tests.
- `tests/common/test_strategy_adapters.py` — updated one existing,
  parametrized test's docstring/name to accurately reflect that
  CombinedVwapNiftyStrategy is no longer "deferred" (behavior itself
  unchanged: a bare call with no market data still returns `[]` for all
  three, correctly).
- `tests/common/test_phase_16_6_structural_safety.py` — updated one test
  to reflect that exactly one of three strategies now declares
  `required_instruments()`.
- `tests/api/test_execution_routes.py` — corrected one Phase 16.6 test's
  incorrect assumption (market_data_status only reflects an actual
  evaluation cycle, not mere registration) and added a new test proving
  CombinedVwapNifty correctly reports `NO_DATA` when evaluated with the
  demo `ExecutionState`'s default (no market-data-source-configured)
  setup — fail-closed, never fabricated.

### Backend — full regression
```
PYTEST_EXIT=0
```
Result tally: **1797 passed**, **6 skipped** (pre-existing/unrelated),
**0 failed**, **0 errored**. The ~20 `PytestUnhandledThreadExceptionWarning`
lines are the same pre-existing, documented, benign fake-SmartAPI
`orderBook()` polling artifact noted in every prior phase's regression in
this engagement.

## 10. Frontend

`TccLifecyclePage` (unchanged structurally from Phase 16.6) gained one
small, valuable clarification: a generated intent's execution result is
now labeled "Shadow result: {result}" rather than a bare result string,
so a viewer can never read a shown result as implying a live order. No
new page, no new control, no `BUY`/`SELL`/`PLACE ORDER`/`GO LIVE` element
was added.

- `TccLifecyclePage.test.tsx`: **18 tests** (was 17), 1 new
  ("Shadow result" labeling, explicit absence of "live order" text).
- Full frontend suite: **66 passed**, 0 failed, 0 errored (9 files).
- `npx tsc --noEmit`: **0 errors**.
- `npm run build`: succeeded.

## 11. Git

Branch: `web-base-algo-trading-control`. Files: this report,
`trading/common/strategies/combined_vwap_nifty.py`,
`tests/common/test_combined_vwap_nifty_strategy.py`,
`tests/common/test_strategy_adapters.py`,
`tests/common/test_phase_16_6_structural_safety.py`,
`tests/api/test_execution_routes.py`,
`frontend/src/pages/TccLifecyclePage.tsx`,
`frontend/src/pages/TccLifecyclePage.test.tsx`. Commit SHA and push
status recorded in the final response below.
`docs/phase-15d-11-human-live-canary-review-report.md` verified untouched
and unstaged before committing.

## 12. Production

**NOT DEPLOYED.**

## 13. Live safety

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED — no `LiveAuthorization` object was
  created, consumed, or referenced by any code added this phase.
- Strategies: STOPPED — no strategy was started by this work outside of
  isolated, per-test fixtures.
- Real broker mutations this phase: **0**.
