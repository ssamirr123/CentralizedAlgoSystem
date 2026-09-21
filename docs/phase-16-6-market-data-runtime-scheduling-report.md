# Phase 16.6 — Market Data Integration & Controlled Runtime Scheduling

Status: **PASS** (PAPER/SHADOW-only; fail-closed on missing/stale/invalid/
partial data; no external real-market-data call was made this phase — see
Section 7).

## 1. Objective

Connect the existing, already-built market-data provider layer
(`trading/market_data/`) to Phase 16.5's `StrategyRuntime`, so a strategy
CAN receive normalized, fresh market data before generating an
`OrderIntent`, without weakening any existing safety gate and without
creating a second execution engine, risk engine, broker abstraction, or
market-data abstraction.

## 2. Discovery

A dedicated research pass (mirroring every prior phase's READ step) found
that almost everything Part 5/6/7/12 of this phase's brief describes
**already exists, fully built, and well-tested**:

| Concept the brief asks for | Already implemented as |
|---|---|
| Broker-independent `MarketDataProvider` interface | `trading/market_data/providers/base.py::MarketDataProvider` (ABC) — its own docstring already bans any order-management method |
| A real, working provider | `trading/market_data/providers/icici_breeze.py::ICICIBreezeProvider` — auth/session handling, `get_index_quote`/`get_option_quote`/`get_option_chain`/`get_historical_candles`, symbol normalization, rate-limit (`ProviderRateLimitError`) and error classification |
| Normalized market-data types | `trading/market_data/schemas.py` — `IndexQuote`, `OptionQuote`, `OptionChain`, `Candle` (all frozen dataclasses, provider-agnostic, `received_at`/`provider_timestamp` fields already present) |
| Freshness/staleness concept | `trading/market_data/cache.py::LiveCache` (`is_stale()`, `age_seconds()`, `stale_seconds` config) and `trading/market_data/status.py`'s `STALE` feed state |
| Scheduler | `trading/market_data/scheduler.py::MarketDataScheduler` — polls market hours every 30s by default, **opt-in only** (`market_data_enabled` env flag in `trading/api/app.py:106`), never automatic |
| Persistence | `trading/market_data/models.py` (`MarketCandle`, `OptionCandle`, `OptionContract`, `DailySession`, `ExpiryCycle`, `OISnapshot`) — plain SQLAlchemy, not TimescaleDB |
| Tests | `tests/market_data/test_provider_breeze.py`, `test_provider_breeze_sensex_master.py` — auth, quotes, candles, option-chain/ATM, streaming, error/rate-limit handling all already covered |

None of this was duplicated. The one real, confirmed gap: **nothing
connects this provider layer to a `Strategy`.** Grepping every call site
of `generate_order_intents()` found none that fetches market data first;
none of the three registered production strategies
(`DoubleStraddleStrategy`, `CombinedVwapNiftyStrategy`,
`VwapAlgoNiftyHedgeStrategy`) accept a market-data-shaped argument or
import anything under `trading/market_data/`. This is the exact same
"the pipe doesn't exist yet, the pieces on both ends do" pattern found in
Phase 16.5 — confirmed by reading the three strategies again for this
phase: their `_on_generate_order_intents()` still returns `[]`
unconditionally (Phase 10 scope, unchanged), so there is genuinely no
strategy decision logic yet for market data to influence. This phase does
not rewrite them; it builds the missing seam and proves it end-to-end
with a test-only strategy, exactly as Phase 16.5 did for the execution
side.

The three LEGACY algo processes (`trading/algos/*`) were also re-checked:
they use Angel-style websocket/REST feeds (`connectapi.py`,
`websocket_feed.py`/`Websocket.py`), not Breeze, and remain completely
out of scope for this phase (same rationale as Phase 16.5 Section 3 —
rewiring them would be a large, risky, unrelated change).

## 3. Market-data architecture

No new abstraction layer was introduced on the provider side — `MarketDataProvider`,
`ICICIBreezeProvider`, and the `IndexQuote`/`OptionQuote` schemas are
reused completely unmodified. One new, deliberately minimal module was
added: `trading/common/market_data_gateway.py`, which defines:

- **`MarketDataSource`** — a one-method `Protocol` (`get_quote(instrument)`),
  much smaller than the full `MarketDataProvider` interface, so the
  runtime depends on the least it possibly can.
- **`ProviderMarketDataSource`** — adapts an existing `MarketDataProvider`
  to that protocol (calls only `get_index_quote()`; read-only by
  construction, since the provider interface has no mutating method to
  call by mistake in the first place).
- **`FixedMarketDataSource`** — a trivial in-memory implementation for
  tests/fixtures. Not a second abstraction: it implements the exact same
  one-method protocol.
- **`check_market_data()`/`gather_market_data()`** — the fail-closed gate:
  returns a usable snapshot **only** when every required instrument's
  quote is present, has a valid positive last-traded price, and is fresher
  than `max_age_seconds` (default `DEFAULT_MAX_DATA_AGE_SECONDS = 60.0`,
  documented in the module, not silently hardcoded elsewhere, and always
  overridable per call/per `StrategyRuntime` instance). Partial data (e.g.
  spot present but not CE) always resolves to `None` — never a partial
  snapshot.

A strategy never sees `IndexQuote`/`OptionQuote` from anywhere but this
gateway, and never imports `trading.market_data.providers.*` directly.

## 4. Runtime architecture

```
MarketDataProvider (trading/market_data/providers/*.py -- UNCHANGED)
        |
        v
MarketDataSource (protocol, trading/common/market_data_gateway.py -- NEW)
        |
        v
gather_market_data()  -- fail-closed: NO_DATA/STALE/INVALID/PROVIDER_ERROR -> None
        |
        v
StrategyRuntime.run_once()  (Phase 16.5, EXTENDED, gate order below)
        |
        v
Strategy.generate_order_intents(market_data)  (Phase 10 interface, extended -- see Section 6)
        |
        v
OrderIntent  (Phase 1, UNCHANGED)
        |
        v
StrategyExecutionEngine.execute()  (UNCHANGED gate order -- Section 6)
        |
        v
PAPER / SHADOW broker (structurally non-networked -- Phase 16.5's hard boundary, unchanged)
```

`run_once()`'s new sequence: resolve strategy → if not active, no-op
(unchanged) → **if `strategy.required_instruments()` is non-empty, gather
market data; if unavailable for any reason, record why and return without
ever calling `generate_order_intents()`** → otherwise call
`generate_order_intents(market_data)` → the rest of Phase 16.5's flow
(intent validation, hard shadow boundary, `execute()`) is byte-for-byte
unchanged.

## 5. Scheduling

**No scheduler was added.** The brief's own Part 8 instruction — "First
determine whether the repository already has a scheduler... if one
already exists, evaluate whether it can safely be reused" — was followed
to its natural conclusion: `MarketDataScheduler` already exists, already
polls safely, and already defaults to disabled (opt-in via
`market_data_enabled`). It has nothing to do with STRATEGY scheduling
(it polls market data for the Straddle Pulse/candle-persistence feature,
unrelated to strategy evaluation), so extending it would conflate two
different concerns. Phase 16.5's `run_once(strategy_id)` — a single,
explicit, synchronous call — remains the only strategy-scheduling
mechanism in this phase, exactly as the brief's own Part 8 anticipates
("Do NOT immediately create a permanent background scheduler"). No
`start()`/`stop()`/background loop was added to `StrategyRuntime`. No
automatic restart of a failed runtime exists or was added.

## 6. Safety boundaries

- **Interface extension, not a rewrite**: `Strategy.generate_order_intents()`
  gained one optional parameter (`market_data: MarketDataInput | None = None`,
  default preserves every existing zero-arg call site exactly).
  `BaseStrategy` stashes it as `self._last_market_data` and calls the
  existing, unmodified `_on_generate_order_intents()` hook with **no
  signature change** — so all 7 existing override sites (3 production
  strategies + 4 test doubles across Phase 10/16.5 tests) needed zero
  changes. A concrete strategy that wants market data reads
  `self.get_market_data()` from inside its own hook.
- **`required_instruments()`** defaults to `()` on `BaseStrategy` — a
  strategy that doesn't override it (all three production strategies
  today) never triggers a market-data fetch or gate at all, preserving
  Phase 16.5's exact behavior byte-for-byte. Verified by
  `test_registered_strategies_declare_no_required_instruments_today` and
  `test_strategy_with_no_required_instruments_is_unaffected_by_market_data`.
- **Fail-closed on every failure mode**: missing (`NO_DATA`), stale
  (`STALE`), invalid/non-positive LTP or missing timestamp (`INVALID`),
  provider exception (`PROVIDER_ERROR`, caught, never raised into the
  runtime), and partial data (any one of several required instruments
  missing) — all result in `generate_order_intents()` never being called
  that cycle. This is reported via a new `market_data_status` field, and
  is explicitly **not** treated as a runtime/strategy failure
  (`RuntimeState` stays `HEALTHY`) — a quiet market (e.g. outside trading
  hours) is normal, not an error.
- **Phase 16.5's triple-layered hard shadow boundary is completely
  untouched**: `ExecutionConfig(dry_run=True)` hard-coded, the
  `PaperBroker`/`ShadowBroker`/`ConnectedShadowBroker` allowlist check,
  and `execution_state.py`'s `ShadowBroker()` wiring. Market data flowing
  into a strategy changes nothing about how an `OrderIntent` reaches
  (or fails to reach) a broker.
- **`market_data_gateway.py` cannot switch execution mode**: it has no
  `ExecutionMode` import or reference at all (`test_market_data_gateway_has_no_configuration_path_to_execution_mode`),
  and `strategy_runtime.py` never hardcodes `ExecutionMode.LIVE`/`LIVE_CANARY`
  anywhere (`test_strategy_runtime_never_hardcodes_execution_mode_live`).
- **No automatic strategy start**: `build_execution_state()` registers
  every strategy `DISABLED` (Phase 10 default) and never calls
  `run_once()`/`enable()`/`start()` on any of them
  (`test_execution_state_never_auto_starts_a_strategy`); `trading/api/app.py`
  never references `strategy_runtime` or `.run_once(` at all
  (`test_app_startup_never_references_the_strategy_runtime`).
- **A genuine, small, additional hardening found and fixed while testing**:
  a test that intentionally reused one idempotency key across two
  genuinely different intents revealed that `run_once()`'s intent loop
  only caught `ShadowBoundaryViolation`, letting `IdempotencyKeyReuseError`
  (deliberately *raised*, not returned, by `execute()` for a caller-side
  key-reuse bug) escape uncaught. Fixed by widening the catch to any
  `Exception` from that loop, reported as a `SHADOW_EXECUTION_FAILED`
  cycle failure like any other — never left to crash the runtime.

## 7. Real market-data validation

**Not performed.** `trading/.env` was checked for Breeze credentials;
none are configured in this environment. Per this phase's own explicit
instruction ("If real credentials are unavailable: DO NOT INVENT SUCCESS.
Use fake provider/fixture... and clearly report that real external
validation was not performed"), all Phase 16.6 tests use
`FixedMarketDataSource` (an in-memory fixture) or a fake/asserting
provider double — never a real network call, never real credentials.
**No external, live Breeze API call was made this phase.**

## 8. Tests

### Backend (targeted, Phase 16.6-specific)
- `tests/common/test_market_data_gateway.py` — 15 tests: no-data, fresh/
  available, stale, invalid (`None`/non-positive LTP), provider-error
  caught, `gather_market_data` (no instruments, no source, all-available,
  partial-data fail-closed, one-of-several-stale fail-closed),
  `ProviderMarketDataSource` delegation + never-calls-a-mutation-method,
  structural safety scan.
- `tests/common/test_strategy_runtime.py` — 11 new tests: strategy with no
  required instruments unaffected, missing/stale/partial/provider-error
  instrument blocks evaluation entirely (never calls
  `generate_order_intents()`), available fresh data reaches the strategy
  and a simulated execution, repeated evaluation with fresh data stays
  idempotent, two-strategy market-data isolation, concurrent evaluation
  across strategies.
- `tests/common/test_phase_16_6_structural_safety.py` — 8 new tests
  (Section 6's structural proofs).
- `tests/api/test_execution_routes.py` — extended the existing lifecycle
  list test with `market_data_status`/`last_market_data_at` assertions;
  no new endpoint was created (Phase 16.5's `GET /api/strategy-lifecycle`
  and `POST .../evaluate` were reused and extended, per this phase's own
  "prefer extending an existing endpoint" instruction).
- `tests/common/test_strategy.py` — unaffected; re-run to confirm the
  `Strategy`/`BaseStrategy` interface extension is fully backward
  compatible.

### Backend — full regression
```
PYTEST_EXIT=0
```
Result tally: **1776 passed**, **6 skipped** (pre-existing/unrelated),
**0 failed**, **0 errored**. The ~20 `PytestUnhandledThreadExceptionWarning`
lines are the same pre-existing, documented, benign fake-SmartAPI
`orderBook()` polling artifact noted in every prior phase's regression in
this engagement.

## 9. Frontend

`TccLifecyclePage` (Phase 16.3/16.4/16.5) extended with two new columns:
**Market Data** (status badge: AVAILABLE/NO_DATA/STALE/INVALID/
PROVIDER_ERROR/—) and **Last market data** (timestamp). No new page, no
new frontend route — the existing lifecycle table was extended, per this
phase's own "extend the existing lifecycle page rather than creating
unnecessary duplicate pages" instruction. No "Run"/"Evaluate" trigger
button was added (same deliberate, conservative scoping decision as Phase
16.5 — the underlying `POST .../evaluate` endpoint exists and is tested
at the API level only). No `BUY`/`SELL`/`PLACE ORDER`/`CLOSE POSITION`/
`GO LIVE`/`AUTHORIZE LIVE`/`LIVE EXECUTION` control exists anywhere on
this page (explicitly re-verified by a new test scanning for all of them
plus market-data-specific phrasing).

- `TccLifecyclePage.test.tsx`: **17 tests** (was 15), 2 new (market-data
  display, no live-trading implication).
- Full frontend suite: **65 passed, 1 failed** on the first run — the
  failure was `TccStrategiesPage.test.tsx`'s pre-existing (unmodified by
  this phase) "Start/Stop only ever flip an in-memory process-lifecycle
  flag" test timing out under parallel load; re-run in isolation it
  passed cleanly in 490ms. Classified **ENVIRONMENTAL**, not a
  regression — the same flake class already documented in Phase 16.3's
  and 16.4's reports for this same file.
- `npx tsc --noEmit`: **0 errors**.
- `npm run build`: succeeded.

## 10. Git

Branch: `web-base-algo-trading-control`. Files: this report,
`trading/common/market_data_gateway.py`, `tests/common/test_market_data_gateway.py`,
`tests/common/test_phase_16_6_structural_safety.py`, `trading/common/strategy.py`,
`trading/common/strategy_runtime.py`, `tests/common/test_strategy_runtime.py`,
`trading/api/execution_routes.py`, `tests/api/test_execution_routes.py`,
`frontend/src/pages/TccLifecyclePage.tsx`, `frontend/src/pages/TccLifecyclePage.test.tsx`,
`frontend/src/api/types.ts`. Commit SHA and push status recorded in the
final response below. `docs/phase-15d-11-human-live-canary-review-report.md`
verified untouched and unstaged before committing.

## 11. Production

**NOT DEPLOYED.**

## 12. Live safety

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged).
- Live authorization: NOT GRANTED — no `LiveAuthorization` object was
  created, consumed, or referenced by any code added this phase.
- Strategies: STOPPED — no strategy was started by this work outside of
  isolated, per-test fixtures.
- Real broker mutations this phase: **0**.
