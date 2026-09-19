# Phase 16.10 — Central Portfolio Risk, Exposure & Strategy Conflict Management

## 1. Objective

Add a centralized portfolio-risk layer so OrderIntents from multiple
independent strategy workers (Phase 16.9) are evaluated against
strategy-level, account-level, and portfolio-level exposure, daily-loss,
open-order, and order-count limits, plus conflict metadata — as an
**additive** gate in front of the existing, unmodified RiskManager. This
phase remains PAPER/SHADOW only: no LiveAuthorization, no real broker
mutation, no strategy auto-started, nothing deployed.

## 2. Existing architecture (read before implementing)

- **RiskManager** (`trading/common/risk_manager.py`, 486 lines) already
  runs 15 named checks per intent (module docstring is stale at
  "fourteen" — Phase 14.6 added a 15th). It is strictly single-intent,
  single-account-limits scoped: `RiskContext`'s `strategy_exposure`/
  `account_exposure`/`daily_pnl`/`strategy_pnl` are caller-supplied
  point-in-time facts; RiskManager never aggregates across strategies or
  accounts, and has no portfolio concept at all. It already supports
  per-account `RiskLimits` overrides (`set_account_limits`), but no
  per-strategy limits and no cross-account rollup.
- **Gate order inside `StrategyExecutionEngine.execute()`** (confirmed
  unchanged, `trading/common/execution.py`): central kill switch (step 0)
  → idempotency replay check (step 1) → account/authorization resolution
  (step 2) → `RiskManager.validate()` (step 3) → mode gate (step 4) →
  broker resolution (step 5a) → LiveAuthorization (step 5b) → atomic
  idempotency claim (step 5c) → broker call (step 5d) → response
  validation → persistence. **Not one line of this file changed.**
- **Phase 15D.6 bug** (`docs/phase-15d-6-controlled-live-authorization-workflow-report.md`):
  a "preview" call to `RiskManager.validate()` used to permanently mark an
  idempotency key as seen and increment the day's order count — poisoning
  the real call that followed. Fixed there via a `dry_run` flag that skips
  only those two mutations. **Lesson applied here**: `PortfolioRiskManager
  .get_snapshot()` (used by the read-only API) is a pure read with zero
  side effects — it never reserves, never increments a counter. Only
  `evaluate_and_reserve()` (called once per real worker submission) has
  any side effect, and it is symmetric: every reservation it creates is
  later either committed or released, never left dangling.
- **WorkerCoordinator** (`trading/common/worker_coordinator.py`, Phase
  16.9) already runs 10 checks before ever calling
  `StrategyRuntime.execute_worker_intent()` — including resolving the
  strategy's real, centrally-assigned `account_id` via `StrategyAssignment
  .get_account_id()`, never trusting the intent's own `account_id`.
- **P&L/exposure/position data**: confirmed there is currently **no**
  real P&L, exposure, or position computation anywhere in the runtime.
  `/api/execution/orders`, `/api/execution/positions`, `/api/execution/pnl`
  are hardcoded empty/zero stubs (Phase 11, honestly documented as such).
  `ShadowBroker` (Phase 16.5) DOES track a real per-symbol, average-cost
  position/realized-P&L book via `_update_position()` — but it is scoped
  to one broker instance (one account) and carries no `strategy_id` at
  all (`BrokerClient.place_order()`'s ABC signature has no channel for
  one), so it cannot answer "what is CombinedVWAP's own exposure" when
  DoubleStraddle shares the same account.
- **`ExecutionResult.average_price` is always `None`** today —
  `execution.py`'s `from_order_result()` hardcodes it, because `OrderResult`
  carries no fill price yet (a pre-existing gap, not introduced by this
  phase). This directly shaped Section 6's design (see below).
- **No Greeks/delta anywhere** in `trading/market_data/` — confirmed by
  source search; the only matches for delta/gamma/theta/vega-shaped
  strings are unrelated (timing/aggregation code).
- **Idempotency** (`trading/common/idempotency_store.py`): `claim()` is an
  atomic bare `INSERT` against a `PRIMARY KEY`, guarded by a
  `threading.Lock` plus SQLite's own file locking. This precedent (a plain
  lock around an atomic in-memory structure) is what Section 20's
  reservation mechanism reuses — no new locking primitive was invented.
- **Existing risk-adjacent tests** (not duplicated): `test_risk_manager.py`,
  `test_risk_manager_account_isolation.py`, `test_kill_switch.py`,
  `test_idempotency_store.py`, `test_worker_coordinator.py`,
  `test_worker_registry.py`, `test_phase_16_9_worker_structural_safety.py`,
  `test_multi_account_routing.py`, and others — all left unmodified except
  one pre-existing structural test in `tests/api/test_execution_routes.py`
  (the `ExecutionState.__dataclass_fields__` set), updated the same way
  every phase since 16.5 has updated it when adding a new field.
- **`/api/risk/*`**: only `/api/risk/status`, `/api/risk/limits`, and
  `POST /api/risk/kill-switch` existed — no collision with this phase's
  new `/api/risk/portfolio`, `/api/risk/accounts[...]`,
  `/api/risk/strategies[...]` paths.

## 3. Architecture delivered

```
Worker OrderIntent
      |
      v
WorkerCoordinator's own 10 existing checks (Phase 16.9, UNCHANGED)
      |
      v
PortfolioRiskManager.evaluate_and_reserve()   <-- NEW, Phase 16.10
      |  (skipped entirely for an idempotent retry -- see Section 9)
      v
StrategyRuntime.execute_worker_intent()
      -> kill switch -> idempotency replay -> authorization ->
         RiskManager.validate() (UNCHANGED, 15 checks) -> mode gate ->
         broker resolution -> LiveAuthorization -> idempotency claim ->
         broker call (PAPER/SHADOW only)
      |
      v
WorkerCoordinator commits or releases the reservation based on the outcome
```

`PortfolioRiskManager` is a new, separate object
(`trading/common/portfolio_risk.py`) rather than a change to
`RiskManager`, for the reasons in that module's own docstring: rewriting a
486-line, already-audited, 15-check gate that every existing caller
depends on staying byte-for-byte the same would be riskier than adding a
new, additive gate in front of it. A portfolio-wide rejection and a
single-intent rejection are visibly different decisions with different
reason codes, never conflated.

## 4. Risk-state source of truth

`PortfolioRiskManager` keeps its own ledger, keyed by
`(strategy_id, account_id, symbol)`, rather than querying
`ShadowBroker.get_positions()`, because the latter has no `strategy_id`
dimension at all. This is not a second, competing source of truth for
account/portfolio numbers — it is the **only** source that can answer a
strategy-scoped question, and account/portfolio numbers are derived by
summing the exact same ledger, so all three levels are always internally
consistent (one ledger, never two independently-computed numbers that
could drift). `ShadowBroker`'s own bookkeeping is untouched and continues
unchanged.

## 5. Supported vs. unsupported metrics

**Supported** (computed from real, observed shadow fills or real,
already-risk-checked pre-trade values):
- Gross notional exposure per strategy/account/portfolio (`|quantity ×
  price|`, summed across open positions **and** currently-outstanding
  reservations).
- Realized P&L per strategy/account/portfolio (average-cost, booked when
  a position is reduced/closed by an opposite-side fill).
- Order counts per calendar day, per strategy/account/portfolio.
- Open-order counts (best-effort — see Section 11 limitations).
- Conflict classification (NONE/DUPLICATE/OPPOSING/CONCENTRATION/
  SELF_OFFSETTING/UNKNOWN) by exact instrument symbol.

**Unsupported, explicitly** (never fabricated):
- **Unrealized (mark-to-market) P&L** — no independent live market mark
  is reliably attached to every worker submission; only realized P&L
  backs the daily-loss checks. Reporting `0.0` for "nothing has closed
  yet" is honest; a live mark price is not invented.
- **Delta-adjusted exposure** — no Greeks exist anywhere in the repository
  (Section 2). Every exposure number here is notional/quantity based.
- **Concentration by "underlying"** — `OrderIntent.instrument.underlying`
  is optional and not populated by any of the three integrated strategies
  today; concentration is grouped by exact symbol instead.

## 6. Exposure model, and the `average_price=None` gap

Exposure is `abs(quantity × price)`, gross, additive across positions —
the exact same convention `RiskManager._order_value()` already uses
(`intent.limit_price` if present, else an optional caller-supplied
`reference_price`; if neither is available and an exposure limit is
configured, the decision is `RISK_DATA_UNAVAILABLE`, never a silent zero).

Because `ExecutionResult.average_price` is always `None` today (Section
2), `commit_reservation()` cannot read a real fill price from the
execution result. Rather than fabricate one, it books the **reservation's
own** price — the same `intent.limit_price`/`reference_price` already used
and already risk-checked at `evaluate_and_reserve()` time — onto the
ledger when the execution succeeds. This is honest (no invented number)
and internally consistent (the ledger's price basis is always a price
that was actually risk-evaluated, never re-derived from a field that
doesn't exist).

## 7. Pre-trade snapshot and projected risk

`PortfolioRiskSnapshot` is an immutable read (`get_snapshot()`, side-effect
free — see Section 2's Phase 15D.6 lesson). Every exposure/loss/order-count
field already includes outstanding reservations, not just committed
fills, so a second concurrent evaluation sees the first's reservation
(Section 9). `evaluate_and_reserve()` computes the **projected** state
(current + this proposed order) and rejects only when the projection, not
just the current value, breaches a configured limit — matching the
brief's own example (400,000 current + 150,000 proposed = 550,000
projected > 500,000 limit → REJECT).

## 8. Limit model

`PortfolioRiskLimits` (`trading/common/portfolio_risk.py`) has the 12
fields from the brief (`max_{strategy,account,portfolio}_daily_loss`,
`_exposure`, `_open_orders`, `_orders_per_day`), every field defaulting to
`None` = "not configured, not enforced" — the exact same convention
`RiskLimits` already uses, for the same documented reason (a hard-0
default would reject every order the instant this module is wired in).
**No production value is invented**: `execution_state.py` constructs
`PortfolioRiskManager()` with every limit unconfigured; an operator
configures real values via `set_strategy_limits()`/`set_account_limits()`/
`set_portfolio_limits()` (no settable API route was added in this phase —
see Section 15).

Loss/exposure checks use `>`/`<=` (inclusive at the limit, matching
`RiskManager`'s own `MAX_DAILY_LOSS` check exactly); order-count and
open-order checks use `>=` on the *current* value (matching
`RiskManager`'s own `MAX_ORDERS_PER_DAY`: once the count reaches the
limit, the next one is rejected) rather than a projected `+1`, since
whether a brand-new order will end up "open" is not knowable in advance.

## 9. Idempotency ordering

`WorkerCoordinator._reserve_portfolio_risk()` checks
`StrategyRuntime.idempotency_store.get(intent.idempotency_key)` **before**
calling `evaluate_and_reserve()` (a new, read-only `idempotency_store`
property was added to `StrategyRuntime` for exactly this). If a record
already exists for that key (COMPLETED/REJECTED/FAILED/AMBIGUOUS/PENDING),
portfolio risk is skipped entirely and the submission passes straight to
`execute_worker_intent()`, which replays the existing execution exactly as
it already would have (Phase 14.6/15D-DR, unchanged) — the SAME behavior
`RiskManager.validate()` itself gets on a replay (it is never called
again either, since `execute()`'s own replay check short-circuits before
step 3). This means an idempotent retry never reserves exposure/order-count
budget twice, and a genuine cross-strategy idempotency-key-reuse bug still
surfaces as `IdempotencyKeyReuseError`, caught by the coordinator exactly
as in Phase 16.9, with the portfolio-risk state for the second (wrongly
reusing) strategy left untouched (proven by
`test_cross_strategy_idempotency_key_reuse_does_not_corrupt_portfolio_state`).

## 10. Concurrency / reservation design

`PortfolioRiskManager.evaluate_and_reserve()` runs its entire
check-then-reserve sequence inside one `threading.RLock` — CHECK, RESERVE,
and the order-count increment all happen atomically. A second concurrent
call blocks until the first releases the lock, and then sees the first's
reservation already counted in its own snapshot. This is the minimum
mechanism needed (a plain lock around an in-process object) — no
distributed lock, no new persistence layer, matching the brief's own
"do not create unnecessary complexity" instruction and reusing the same
"atomic operation guarded by a lock" shape `IdempotencyStore.claim()`
already uses.

`commit_reservation()`/`release_reservation()` resolve a reservation
exactly once: on success with a terminal status (COMPLETE/FILLED), the
reservation becomes a permanent ledger fill; on any downstream rejection,
exception, or failure, `WorkerCoordinator` calls `release_reservation()`,
which gives back both the exposure reservation and the order-count budget
— mirroring `RiskManager`'s own "a rejected intent must never consume the
day's order-count budget" discipline. A still-`OPEN` (non-terminal) result
keeps its reservation's footprint counted (via `_open_orders`) until an
explicit `resolve_open_order()` call — this module does not poll broker
state to auto-resolve it (see Section 11).

## 11. Conflict classification and hedge awareness

`ConflictType` = `NONE | DUPLICATE | OPPOSING | CONCENTRATION |
SELF_OFFSETTING | UNKNOWN`, computed from the same ledger plus outstanding
reservations, by exact symbol. **None of these block a submission by
default** — `PortfolioRiskManager(hard_block_conflicts=frozenset())` is
the default, matching the explicit instruction not to invent trading
policy ("some strategies legitimately hedge each other"). A caller may
opt a specific type in (e.g. `frozenset({ConflictType.OPPOSING})`) if a
real business rule justifies it; Phase 16.10 configures none.
`OPPOSING` metadata explicitly notes "gross exposure preserved, not
netted; may be a legitimate hedge" — DoubleStraddle/VWAPHedge's own hedge
legs are never treated as risk-free, and never treated as equivalent risk
to a naked position, but no options-margin-offset engine was built (none
exists in the repository).

## 12. Worker boundary

A worker cannot calculate or influence authoritative portfolio risk:
`PortfolioRiskLimits` is never exposed through `worker_protocol.py` (no
message type carries a limit field — proven structurally), and
`WorkerCoordinator` never calls `set_strategy_limits`/`set_account_limits`/
`set_portfolio_limits` in response to any worker message (proven
structurally by source-scanning `worker_coordinator.py`). A worker-supplied
`account_id` on its `OrderIntent` is never used for portfolio-risk
routing — the same centrally-resolved `StrategyAssignment.get_account_id()`
value Phase 16.9 already uses is what `PortfolioRiskManager` reserves
against (proven by
`test_worker_supplied_account_id_is_never_used_for_portfolio_risk_routing`).

## 13. Gate ordering (final, confirmed unchanged where it matters)

1. Central kill switch — still step 0 inside `execute()`, still checked
   again by `WorkerCoordinator`'s existing checks before that. Portfolio
   risk sits AFTER `WorkerCoordinator`'s own 10 checks and BEFORE
   `execute_worker_intent()`, so a kill-switch-engaged submission never
   even reaches portfolio risk's reservation logic in the sense that it
   is irrelevant — but if it somehow did, the kill switch is re-checked
   again downstream regardless. Verified by
   `test_kill_switch_blocks_worker_submissions_even_with_portfolio_risk_wired`.
2. `PortfolioRiskManager.evaluate_and_reserve()` (new).
3. `StrategyExecutionEngine.execute()`'s own unchanged 8-step pipeline,
   including the full, unmodified 15-check `RiskManager.validate()`.

`PortfolioRiskDecision.allowed=True` means **RISK ACCEPTABLE ONLY** — never
"authorized to trade live". `AccountAuthorizationState`/`LiveAuthorization`
remain fully independent, evaluated only inside `execute()`, completely
unaware of this new module's existence.

## 14. Audit

Portfolio-risk rejections are surfaced through the existing
`OrderIntentResult.reason` string (`"{REASON_CODE}: {message}"`), which
already flows back to whatever calls `WorkerCoordinator.submit_order_intent()`.
A dedicated `AuditTrail.append()` event for portfolio-risk decisions (e.g.
`EVENT_PORTFOLIO_RISK_DECISION`, alongside the existing `EVENT_RISK_DECISION`)
was **not** added in this phase to keep the change additive and minimal;
this is listed as remaining Phase 16.11 work (Section 18). No credential,
secret, or token is logged anywhere in this module (it holds none).

## 15. APIs

Five new, read-only routes in `trading/api/execution_routes.py`, all VIEW
permission, none accepting a body or capable of executing/placing/
modifying/cancelling an order:

```
GET /api/risk/portfolio
GET /api/risk/accounts
GET /api/risk/accounts/{account_id}
GET /api/risk/strategies
GET /api/risk/strategies/{strategy_id}
```

Each calls `PortfolioRiskManager.get_snapshot()` — the pure read from
Section 2 — never `evaluate_and_reserve()`. No settable-limits endpoint
was added (an operator configures limits by calling the manager's
setters directly today, e.g. from a startup script) — the brief's own
suggested API list was read-only, and adding a mutation endpoint for
limits was not required by it.

## 16. Frontend

`frontend/src/pages/TccRiskPage.tsx` (the existing Risk page, not a new
page — it already covers "RiskManager status + kill switch", and the
portfolio-risk view is a natural, related section, not a separate
concept) gained a new "Portfolio risk (Phase 16.10)" card with three
tables: Portfolio (one row), Accounts (one row per account), Strategies
(one row per strategy) — each showing Daily P&L / Gross exposure / Open
orders / Orders today / Status, with CURRENT values only (no BUY/SELL/
PLACE ORDER/AUTHORIZE LIVE/GO LIVE control anywhere on the page, verified
by an existing structural test that now also covers the new section).

## 17. Tests

- `tests/common/test_portfolio_risk.py` (30 tests): strategy/account/
  portfolio exposure at below/exactly-at/above limits, daily-loss
  hierarchy (strategy → account → portfolio) with exact realized-P&L
  math, order-count and open-order limits, independence between the three
  scope levels, missing-price → `RISK_DATA_UNAVAILABLE`, internal-exception
  → `INVALID_RISK_STATE` (fail closed, never a silent zero), a
  never-traded strategy legitimately reporting `0.0` P&L (distinct from
  "unavailable"), `get_snapshot()` proven side-effect free, all six
  conflict classifications (including the "very first order in an empty
  portfolio is not '100% concentrated'" edge case), opt-in
  `hard_block_conflicts`, reservation double-commit/release-of-unknown
  safety, and a genuine multi-threaded concurrency test proving two
  simultaneous 75,000 reservations against 400,000 committed + a 500,000
  limit accept exactly one and reject exactly one.
- `tests/common/test_phase_16_10_worker_portfolio_risk.py` (14 tests):
  full `WorkerCoordinator` + `PortfolioRiskManager` integration — within-
  limits acceptance updates the ledger; portfolio risk rejects before
  `execute_worker_intent()` is ever called; the existing `RiskManager`
  still rejects (and the reservation is released) even when portfolio
  risk itself would have allowed; kill switch still blocks all
  submissions; idempotent retry does not double-reserve; cross-strategy
  idempotency-key misuse does not corrupt portfolio state; worker-protocol
  structural proof of no limit-setting message; coordinator structural
  proof it never calls a limit setter; worker-supplied `account_id` is
  never used for routing/reservation; module-level structural proof of no
  broker-adapter import and no `LiveAuthorization` import; a
  recording-broker spy proving zero `place_order`/`modify_order`/
  `cancel_order` calls through the full portfolio-risk-gated path; and a
  three-real-strategy (`CombinedVwapNiftyStrategy`, `DoubleStraddleStrategy`,
  `VwapAlgoNiftyHedgeStrategy`), three-worker end-to-end scenario proving
  at least one submission accepted and at least one rejected purely by
  `PortfolioRiskManager`'s portfolio-wide order-count limit, with zero
  real broker calls.
- `tests/api/test_execution_routes.py`: 6 new tests for the read-only
  portfolio-risk endpoints (healthy zeroed default, per-account/per-strategy
  listing, 404 on unknown id, no order-mutation verb ever appears in a
  response body) plus an update to the existing
  `ExecutionState.__dataclass_fields__` structural test.
- `frontend/src/pages/TccRiskPage.test.tsx`: 2 new tests (portfolio-wide
  P&L/exposure/status rendering; per-account/per-strategy row rendering),
  plus a `beforeEach` default mock for the three new hooks so every
  pre-existing test in the file continues to render without needing to
  know about this phase's addition.

## 18. Limitations / remaining Phase 16.11+ work

- No settable `/api/risk/portfolio-limits` (or similar) mutation endpoint
  exists yet — limits are configured only by calling the manager's Python
  setters directly.
- No dedicated `AuditTrail` event for a portfolio-risk decision was added
  (Section 14) — only the existing `OrderIntentResult.reason` string
  carries it today.
- Open-order tracking (Section 10) is best-effort and not auto-resolved
  by polling broker state — a still-open shadow order stays "open" in
  this manager until `resolve_open_order()` is called explicitly.
- Concentration groups by exact symbol, not by a shared "underlying"
  (Section 5) — no strategy today populates `OrderIntent.instrument
  .underlying`.
- Unrealized P&L remains fully unsupported (Section 5) — only realized
  P&L from actual closed shadow fills backs any loss check.
- Portfolio risk is wired only into the worker (`WorkerCoordinator`) path,
  matching this phase's own scope ("OrderIntents generated by multiple
  independent strategy workers") and the brief's own architecture diagram
  — the local, non-distributed `StrategyRuntime.run_once()` path is
  unchanged and does not consult `PortfolioRiskManager`.

## 19. Regression

- **Targeted Phase 16.10 suite**: all new/updated files, `EXIT=0`.
- **Full backend regression**: **1943 passed, 6 skipped, 0 failed, 0
  errors**, `PYTEST_EXIT=0` — 50 more passing tests than the stated
  pre-Phase-16.10 baseline of 1893, consistent with the new test files;
  skip count (6) unchanged. The recurring
  `PytestUnhandledThreadExceptionWarning` lines from the pre-existing fake
  `AngelOne.orderBook()` polling thread are the same known, benign,
  pre-existing artifact seen in every prior phase's full run.
- **Frontend**: 69 tests across 9 files pass; one test
  (`TccStrategiesPage.test.tsx`'s "Start/Stop only ever flip an in-memory..."
  test) intermittently timed out under the full parallel run and was
  confirmed to pass cleanly (450ms) in isolation — the same pre-existing,
  documented environmental flake observed in prior phases, unrelated to
  any code changed here.
- `npx tsc --noEmit`: clean, exit 0.
- `npm run build`: succeeded, exit 0.

## Safety statement

No live order was placed. No LiveAuthorization was granted or consumed.
No real broker connection or mutation occurred (proven by a recording-spy
broker showing zero `place_order`/`modify_order`/`cancel_order` calls).
No strategy was automatically started. Nothing was deployed to
production, and no AWS/EC2 resource was created. `PortfolioRiskManager`
can only ever make an OrderIntent submission MORE restricted (add a
rejection reason), never grant an authorization, bypass the kill switch,
or bypass the existing RiskManager — every existing safety gate remains
in its original position, unmodified.
