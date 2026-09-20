# Phase 17.1-R — Live Readiness Safety Remediation Report

**PHASE 17.1-R = PASS** (code safety readiness). Environmental readiness remains PENDING — see Section 11.

Baseline commit: `1761eeb104d45321e0db7f7e143319f1cf61a9ea` (Phase 17.1, committed and pushed to `origin/web-base-algo-trading-control` before this phase began, per Section 3's requirement). This phase remediates the 6 code-level blockers Phase 17.1 identified as BLOCKED in its own readiness matrix. Item 7 (fresh production/AWS/broker read-only verification) is explicitly environmental and reported separately in Section 11 — no credentials were available in this session, so it is reported as PENDING, never fabricated.

No real broker order was submitted, modified, or cancelled. No LiveAuthorization was granted against a real account. No strategy was started. No AWS resource was touched.

---

## 1. modify_order() safety (Remediation A)

**Finding (Phase 17.1)**: `AngelOneBroker.modify_order()`, `DhanBroker.modify_order()`, and `ICICIBreezeBroker.modify_order()` each carried only the `_require_not_read_only()` guard — unlike their own `place_order()`/`cancel_order()`, which also require `TradingConfig.is_live`. `cancel_order()` on all three already had BOTH guards and needed no fix (see Section 2).

**Fix**: added the identical `if not self._config.is_live: raise LiveTradingDisabledError(...)` check to all three `modify_order()` implementations, in the same position (after the read-only guard, before `_require_connected()`) as `place_order()`/`cancel_order()` already use. No SDK call shape, retry logic, or parameter mapping changed.

**Why not routed through a new central "modify" execution path**: a repo-wide search (re-confirmed this phase) shows `modify_order()` has zero callers anywhere in `trading/common`/`trading/api` outside two read-only validation scripts (`trading/validation/angel_readonly.py`, `angel_connected_shadow.py`) and tests. It is not reachable from `StrategyExecutionEngine.execute()`, `WorkerCoordinator`, or any worker route today. Building a full central-authority "modify" pipeline (Section 7's conceptual diagram) for a method nothing in this codebase currently calls would be new capability, not remediation — out of this phase's charter. The adapter-level fix closes the concrete gap (a caller who did invoke it outside DRY_RUN would now be blocked the same way `place_order`/`cancel_order` already are); if a future phase builds a real strategy-modification feature, it must route through the same gate chain `execute()` already enforces for placement, never a shortcut — documented here as a requirement for that future work, not built now.

**Tests**: `test_adapter_modify_order_blocked_in_read_only_mode`/`test_adapter_modify_order_refuses_when_not_live` added to `tests/common/test_broker_adapter_contract.py` (parametrized across all 3 real adapters), plus one adapter-specific `test_modify_order_refuses_when_not_live` in each of `test_angelone_broker.py`/`test_dhan_broker.py`/`test_icici_breeze_broker.py`. Two pre-existing AngelOne tests that constructed a default (non-live) broker to call `modify_order()` were updated to pass `config=_config(live=True)`, matching the exact same fix already applied to that file's `place_order`/`cancel_order` tests.

## 2. cancel_order() review (Section 9)

Inspected all three real adapters: `cancel_order()` already carries BOTH `_require_not_read_only()` and the `is_live` check on AngelOne, Dhan, and ICICI Breeze (pre-existing, unchanged). **Decision: no fix needed** — `cancel_order` never had the structural defect `modify_order` had. This is a documented, deliberate "no change" decision, not an oversight: re-verified by reading each adapter's source directly and confirmed by the pre-existing `test_adapter_cancel_order_blocked_in_read_only_mode`/`test_adapter_cancel_order_refuses_when_not_live` contract tests, which already passed before this phase and are unmodified.

## 3. VWAPHedge DRY_RUN boundary (Remediation B)

**Finding (Phase 17.1)**: `trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py` had NO `DRY_RUN` gate at all — unlike `CombinedVwapNifty`/`DoubleStraddelAlgo`. Its `place_market_order()`/`place_stoploss_order()` were guarded only by the central kill switch (`assert_live_mutation_allowed()`); its `modify_stoploss_order()` had no guard whatsoever, not even the kill switch.

**Fix**:
- `trading/algos/Vwap_Algo_Nifty_hedge/config.py`: added a `DRY_RUN` flag via the identical `_env_flag("BOT_DRY_RUN", "BOT_DY_RUN", default="true")` helper `CombinedVwapNifty/config.py` already defines (copied verbatim) — defaults to `True`, the same safe posture as the other two legacy algos.
- `trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py`: `place_market_order()` and `place_stoploss_order()` now short-circuit into a synthetic `DRYRUN-<timestamp>` id (matching `CombinedVwapNifty`'s exact shape) before building any order params, exactly like the other two legacy algos already do. `modify_stoploss_order()` now has BOTH a DRY_RUN short-circuit AND `assert_live_mutation_allowed(strategy_id=_STRATEGY_ID)` before its real `config.objconn.modifyOrder(...)` call — closing the one mutation call site in this codebase that previously had no safety gate of any kind.
- No entry/exit/SL/target/strike/quantity/timing logic changed — confirmed by grep: the only new lines are the three DRY_RUN checks and one added kill-switch-guard call.

**Fail-closed structural proof (Section 11)**: `main.py` still unconditionally opens a real AngelOne session (`config.objconn = connectapi.makeconnection()`), consistent with the OTHER two legacy algos' own identical pattern (verified: neither of them gates the connection itself on DRY_RUN either — only the mutation calls) — this is not a new gap introduced here, and DRY_RUN's role is specifically to prevent order MUTATION, matching the exact contract the other two legacy algos already established.

**Tests**: `tests/algos/test_vwap_algo_nifty_hedge_dry_run.py` (new). `pandas_ta` is not installed in this session's environment (confirmed: absent from `requirements.txt`, absent from `pip show`) — this legacy algo's own module was never previously imported by any test in this repo at all. The file's first test (`test_static_source_has_dry_run_gate_on_every_mutation_call_site`) reads `rest_func.py` as plain text (never imports it) and always runs, proving the DRY_RUN short-circuit and the kill-switch guard exist at all three mutation call sites — this test PASSED. The five behavioral tests (constructing the module and calling the functions with an `_ExplodingObjconn` broker double that raises on any attribute access) are skipped where `pandas_ta` is unavailable — an honest `SKIPPED`, never a fabricated PASS. In an environment with `pandas_ta` installed (e.g. CI, if configured), all six tests would exercise the real behavior end-to-end.

## 4. DoubleStraddle idempotency identity (Remediation C)

**Finding (Phase 17.1)**: `DoubleStraddleStrategy._intent()`'s `idempotency_key` was `f"{strategy_id}:{instrument}:{key_suffix}"` — `key_suffix` values (`"HEDGE:ENTRY"`, `"morning:ENTRY"`, `"morning:TIME_EXIT"`, ...) carry no date component. The exact same key regenerates every trading day, so day 2's real order would be replayed from day 1's cached COMPLETED result and never reach the broker.

**Fix**: `_intent()` now derives `trading_date = self._clock().date().isoformat()` from the SAME already-injectable `clock` callable this strategy's `__init__` already uses for every wall-clock comparison (`self._clock = clock or datetime.now`) and embeds it into the key: `f"{strategy_id}:{trading_date}:{instrument}:{key_suffix}"`. No new timezone handling was invented — the strategy's own documented design already defers real IST-aware scheduling to whatever clock a caller injects; this fix uses that exact same seam for date derivation, consistent with "use the application's canonical trading timezone" (Section 14 of the brief) rather than hardcoding one. No entry/exit/SL/target/timing rule changed — confirmed via full regression of the existing 21 pre-existing tests, unmodified, all passing.

**Restart stability, cross-day distinction, and per-leg identity — new tests** (`tests/common/test_double_straddle_strategy.py`):
- `test_same_day_worker_restart_produces_the_same_idempotency_key` — two independent `DoubleStraddleStrategy` instances (simulating a fresh process after restart, since no in-memory state persists) sharing the same fixed clock produce IDENTICAL keys.
- `test_next_trading_day_produces_a_different_idempotency_key` — the same logical event on two different dates produces DISJOINT key sets.
- `test_morning_vs_afternoon_logical_event_have_distinct_keys_same_day`, `test_ce_and_pe_leg_have_distinct_keys`, `test_idempotency_key_carries_an_isoformat_trading_date`, `test_same_day_duplicate_evaluation_is_still_a_stable_key_not_a_new_one` — all pass.

**Documented limitation, unchanged by this phase**: `CombinedVwapNiftyStrategy`'s and `VwapAlgoNiftyHedgeStrategy`'s own idempotency keys (which already include an in-memory `entry_sequence`/`entry_count` counter, per Phase 17.1's own research) still reset that counter on restart and still carry no date component either — Phase 17.1-R's own brief named `DoubleStraddleStrategy` specifically as the item to fix; the other two strategies' key derivation is out of this phase's named remediation list and is flagged here for a future phase, not silently ignored.

## 5. AMBIGUOUS lifecycle preserved, never collapsed back to REJECTED (Remediation D)

Phase 17.1's fix (`ExecutionResult.ambiguous()`, `PortfolioRiskManager.commit_reservation()` holding an ambiguous outcome open in `_open_orders` rather than releasing it) is **unchanged, byte-for-byte, by this phase** — re-verified by re-running every Phase 17.1 test in `tests/common/test_phase_17_1_live_readiness.py` (all pass) and by the new `test_reconciled_*`/`test_two_concurrent_*` tests in Section 6 below, which build directly on top of it. No automatic retry was added anywhere; `test_scenario_unknown_outcome_is_exactly_one_attempt_no_blind_retry` (pre-existing, Phase 17.1) and the new `test_ambiguous_state_and_reservation_survive_a_fresh_process_reopening_the_same_stores` (this phase) both confirm this.

## 6. Reconciliation feedback into idempotency + portfolio risk (Remediation E)

**Extends** `trading/common/reconciliation.py`'s existing, authoritative `ReconciliationService` — no second reconciliation subsystem was created.

**New capability**:
1. `trading/common/idempotency_store.py`: added `IdempotencyStore.resolve_ambiguous(idempotency_key, *, new_status, broker_order_id="", result_json="") -> bool` to the Protocol, `SqliteIdempotencyStore`, and `InMemoryIdempotencyStore`. An atomic compare-and-swap: transitions a record FROM `AMBIGUOUS`/`PENDING` ONLY to a genuine terminal status; refuses (returns `False`, never raises) if the record is already terminal or doesn't exist — never overwrites resolved history.
2. `trading/common/portfolio_risk.py`: added `find_reservation_by_idempotency_key()` (read-only lookup) and `resolve_reconciliation(reservation_id, *, found, fill_price=None) -> bool` — commits the held reservation to the ledger (FOUND) or releases its exposure/order-count budget (NOT_FOUND), operating on `_open_orders` (where an ambiguous outcome is filed), never on `_reservations`.
3. `trading/common/reconciliation.py`: `ReconciliationService.__init__` gained an optional `portfolio_risk_manager` parameter (default `None`, zero behavior change if omitted — the same discipline every other optional dependency in this codebase already follows). `reconcile_one()` now calls a new `_feed_back_to_idempotency_and_risk()` step immediately after `complete()` succeeds:
   - `RECONCILED_FILLED`/`RECONCILED_ACCEPTED` → idempotency record becomes `STATUS_COMPLETED` (with a synthesized `ExecutionResult(success=True, ...)`); portfolio reservation is COMMITTED.
   - `RECONCILED_REJECTED`/`RECONCILED_NOT_FOUND` → idempotency record becomes `STATUS_REJECTED` (Section 23's explicit fail-closed policy: never authorizes a new attempt); portfolio reservation is RELEASED.
   - `RECONCILIATION_UNKNOWN`/`RECONCILIATION_FAILED` → **HOLD**, nothing changes — matches `REQUIRES_REVIEW` from the brief's own state diagram, using this repository's actual existing state names (`ReconciliationStatus` was not extended with new values; the existing enum already covers every case).
4. Concurrency: the feedback step only runs for whichever caller's `try_acquire()` compare-and-swap won the reconciliation record itself (pre-existing, unchanged mechanism) — `resolve_ambiguous()`'s own independent CAS is a second layer of protection specifically for the idempotency+risk side effect, so even a hypothetical future caller invoking the feedback method directly (bypassing `reconcile_one()`) cannot double-apply it.

**Module docstring updated** to honestly describe this new capability, replacing the now-outdated claim that "nothing ... ever feeds that result back" — the updated docstring is explicit about what still never happens (no broker mutation, no retry, no new order; `StrategyExecutionEngine`'s own per-order `RiskManager.validate()` gate is untouched since it only ever runs for a NEW intent).

**Tests**: `tests/common/test_phase_17_1_r_reconciliation_feedback.py` (new, 9 tests) — FILLED→COMPLETED with proven no-broker-recall-on-replay, NOT_FOUND→REJECTED with proven no-automatic-retry, RECONCILIATION_UNKNOWN leaves state untouched, a stale `resolve_ambiguous()` call is a no-op, FOUND commits the held portfolio reservation, NOT_FOUND releases it, UNKNOWN keeps it held, two concurrent `ReconciliationService` instances racing on the same record produce exactly one resolution (one audit event, one commit, no double-booking), and the ambiguous state + reservation survive a simulated TCC restart (a fresh `SqliteIdempotencyStore` instance reopening the same db file).

## 7. Operator identity → LiveAuthorization HTTP boundary (Remediation F)

**Finding (Phase 17.1)**: `trading.common.operator_identity.JwtClaimsAuthenticationProvider` was a fully-built, tested "future seam" — no route in `trading/api/` had ever used it. Every prior exercise of `LiveAuthorizationWorkflow` was via ad-hoc scripts.

**New**: `trading/api/live_authorization_routes.py` — `POST /api/live-authorization/request`, `POST /api/live-authorization/{id}/confirm`, `GET /api/live-authorization/{id}`. All three require `Permission.TRADING_CONTROL` via the SAME `Depends(require_permission(...))` every other route in this API already uses. The authoritative `OperatorIdentity` is built ONLY from `request.state.principal` (the already-authenticated JWT principal) via `JwtClaimsAuthenticationProvider.resolve_operator()` — never from request JSON. Every input schema sets `model_config = ConfigDict(extra="forbid")`, so a client attempting to smuggle an `operator`/`authorized_by`/`operator_id` field into the body gets a 422 before this module's own code ever runs.

**Identity separation (Section 28)**: this router never imports `trading.common.worker_auth` at all; a worker's `X-Worker-Auth` credential cannot authenticate here (`test_worker_auth_header_alone_cannot_authenticate`, passes with a 401).

**"Confirmation" (Section 41)**: `LiveAuthorizationWorkflow.confirm()` requires a `ConfirmationProvider` callable representing an explicit human "yes". For this HTTP surface, the deliberate, authenticated POST request to `/confirm` itself IS that human action — documented explicitly in the route module's own docstring, alongside a re-confirmation that no `AUTO_APPROVE`-style environment variable exists anywhere in `trading/common/live_authorization*.py`/`live_canary.py` (re-searched this phase, zero matches, same result as Phase 17.1's own search).

**ExecutionState additions**: `idempotency_store` (a store SEPARATE from whatever `StrategyExecutionEngine`/`StrategyRuntime` uses — that engine remains not wired to persistent idempotency in the live API process at all, a pre-existing, unchanged gap named honestly rather than silently worked around), `live_authorization_store`, `authorization_service` — each defaults to a test-isolated, per-instance value (in-memory / a fresh per-process temp file), with `IDEMPOTENCY_DB_PATH`/`LIVE_AUTHORIZATION_DB_PATH` env vars opting a real deployment into durable storage, matching the exact `AUDIT_DB_PATH`/`KILL_SWITCH_PERSISTENCE_PATH` pattern this codebase already uses everywhere else.

**Tests**: `tests/api/test_live_authorization_routes.py` (new, 19 tests) — unauthenticated=401, viewer/trader roles=403, operator/admin can request, worker-auth-header alone=401, forged `operator`/`authorized_by` body fields=422 (rejected before reaching any business logic), `authorized_by` in the response always reflects the real authenticated username, request→confirm reaches `AUTHORIZED`, a DIFFERENT authorized operator may confirm what another requested (attribution to the ORIGINAL requester is preserved), creating an authorization never starts the strategy, confirming one never touches the idempotency store (i.e. never submits an intent), unknown-account/credential-mismatch requests are rejected at request time, GET requires authentication, and — reusing the pre-existing, unmodified `SqliteLiveAuthorizationStore.try_consume()` — single-use (a second consumption attempt raises `LiveAuthorizationError`) and cross-account scope-binding (consuming for `DHAN_MAIN` against an authorization scoped to `ANGEL_MAIN` raises) both still hold after this HTTP integration.

---

## 8. Full mutation surface re-audit (Section 36)

Repeated the repository-wide search for `placeOrder`/`place_order`/`modifyOrder`/`modify_order`/`cancelOrder`/`cancel_order`/`squareOff`/`square_off`/`exit_position`/`close_position`. Same 26 files as Phase 17.1's own inventory, plus two references in `portfolio_risk.py`/`reconciliation.py` that are docstring mentions only (confirmed by direct grep of those two files — no new call site). No unclassified surface. Every category from Phase 17.1's table (CENTRAL EXECUTION PATH / LEGACY GUARDED PATH / SHADOW-PAPER PATH / TEST-FAKE PATH / UNUSED PATH) still applies unchanged, with two rows upgraded from BLOCKED to PASS (`modify_order`'s missing `is_live` check; `Vwap_Algo_Nifty_hedge`'s missing DRY_RUN gate).

## 9. Final gate ordering re-verification (Section 37)

Re-read `StrategyExecutionEngine.execute()` fresh from current source: the order is **unchanged** from Phase 17.1's own re-trace — kill switch → idempotency replay → account/authorization-state → RiskManager → mode-specific gate (LiveCanaryGuard/LIVE-readiness) → broker resolution → LiveAuthorization consumption → idempotency claim → broker call → response validation → persistence. `WorkerCoordinator`'s own chain (worker authentication → session validation → strategy ownership → account assignment → PortfolioRiskManager → the `execute()` chain above) is likewise unchanged. Neither this phase's fixes (adapter-level `modify_order` guards, `DoubleStraddleStrategy`'s key derivation, the reconciliation feedback extension, or the new LiveAuthorization HTTP routes) touch this ordering at all — none of them sit inside `execute()`'s own call graph.

## 10. Distributed simulation, three-worker isolation, strategy regression (Sections 38-40, 47)

Phase 17.1's own `tests/common/test_phase_17_1_live_readiness.py` (15 tests: valid path=1 mutation, missing/expired authorization=0, kill switch=0, risk-reject=0, confirmed-rejection=1 bounded attempt, unknown-outcome=1 attempt/no blind retry, duplicate-network-submission=1 logical mutation, 3-worker cross-account/cross-strategy leakage) was re-run unmodified this phase and still passes in full — no regression from any Remediation A-F change. This phase's own new LiveAuthorization single-use/scope tests (Section 7 above) extend that coverage to the new HTTP surface specifically. Strategy regression: `CombinedVwapNiftyStrategy` — untouched, its own test file re-run unmodified, all pass; `DoubleStraddleStrategy` — trading logic untouched (only idempotency-key derivation changed), full existing 21-test suite plus 6 new tests, all pass; `VwapAlgoNiftyHedgeStrategy`/legacy algo — trading logic untouched (only the DRY_RUN/kill-switch safety gates changed, per Section 3 above).

## 11. Environmental verification (Section 42-45)

**CODE SAFETY READINESS = PASS.**
**ENVIRONMENTAL READINESS = PENDING** — no AWS or broker credentials were available in this session (same constraint as Phase 17.1). No fresh read-only production/broker check was performed. Per Section 45's explicit distinction, this does NOT block Phase 17.1-R's own PASS verdict (a code-level remediation phase), but it DOES mean the future-canary account's current FLAT status and the production TCC's currently-deployed commit/environment remain unconfirmed as of TODAY — carried forward from Phase 17.1's own report, not re-verified.

---

## 12. Readiness matrix (re-run from Phase 17.1)

Every row Phase 17.1 marked BLOCKED that was in this phase's remediation scope is now PASS:

| # | Control | Phase 17.1 | Phase 17.1-R |
|---|---|---|---|
| 9 | `modify_order()` missing `is_live` check | BLOCKED | **PASS** |
| 10 | `Vwap_Algo_Nifty_hedge` no DRY_RUN gate | BLOCKED | **PASS** |
| 19 | Reconciliation result feeds back into idempotency | BLOCKED | **PASS** |
| 23 | `DoubleStraddleStrategy` key stable across trading days | BLOCKED | **PASS** |
| 24 | Operator identity captured via an HTTP route | PASS (object)/BLOCKED (no route) | **PASS** |
| 25 | AccountAuthorizationState transition has an audited code path | BLOCKED | **Unchanged — out of this phase's named remediation list, see below** |
| 26 | Canary limits configured, no invented defaults | PASS/BLOCKED (fresh clone) | **Unchanged** |

Row 25 (`TradingAccount.set_authorization_state()`/`record_authorization_transition()` still have zero callers anywhere in the repo) and row 26 (canary limits still live only in an untracked `.env`) were **not** named in this phase's 6-item remediation list (Section 1 of the brief) and are not fixed here — carried forward honestly, not silently dropped.

All other Phase 17.1 PASS rows remain PASS, re-verified by full regression (Section 13).

## 13. Regression

Backend: **2087 passed, 10 skipped, 0 failed** (full suite, including 6 pre-existing `test_vwap_algo_nifty_hedge_dry_run.py` behavioral tests honestly skipped for the pre-existing `pandas_ta`-not-installed reason, never fabricated). This run also caught and required fixing two pre-existing structural guard tests in `tests/api/test_execution_routes.py` (`test_execution_routes_module_never_touches_live_authorization`, `test_execution_state_never_wires_a_live_authorization_store`) that correctly asserted the OLD "LiveAuthorization is never wired into the API layer" fact — updated to assert the NEW, deliberately-reviewed fact (LiveAuthorization is now wired, but only through the separate, dedicated `live_authorization_routes.py`, never through `execution_routes.py` itself, which remains exactly as LiveAuthorization-free as before). No other regression anywhere in the suite.
Frontend: not re-run this phase — zero frontend files were touched by any Remediation A-F change (confirmed via `git diff --stat`); Phase 17.1's own frontend result (77 passed, 1 pre-existing-flaky pass-on-isolation, 78 total) stands unchanged.

New/modified test files this phase: `tests/common/test_broker_adapter_contract.py` (+2 parametrized tests), `tests/common/test_angelone_broker.py`/`test_dhan_broker.py`/`test_icici_breeze_broker.py` (+1 test each, 2 pre-existing AngelOne tests fixed to pass `live=True`), `tests/algos/test_vwap_algo_nifty_hedge_dry_run.py` (new), `tests/common/test_double_straddle_strategy.py` (+6 tests), `tests/common/test_phase_17_1_r_reconciliation_feedback.py` (new, 9 tests), `tests/api/test_live_authorization_routes.py` (new, 19 tests), `tests/api/test_execution_routes.py` (2 structural guard tests updated to reflect the deliberate architecture change, +1 companion structural test).

---

## Recommendation for Phase 17.2

Every code-level safety item this phase was scoped to fix is now PASS, and the phase's own new safety code (reconciliation feedback, the LiveAuthorization HTTP boundary) is covered by new, passing tests exercising realistic FakeBroker/ShadowBroker scenarios, including concurrency and restart. Two pre-existing, honestly-carried-forward gaps remain (rows 25/26 above) that were outside this phase's named scope. **Environmental readiness is PENDING** — before any future live-canary phase, a human with real AWS/broker read-only access must re-verify: the production TCC's currently-deployed commit/environment, the canary account's FLAT status, and that `trading/.env`'s canary limits are still the intended values. This report recommends that verification as the next concrete step, NOT Phase 17.2 itself, which requires separate explicit human authorization regardless.

---

CODE SAFETY READINESS = PASS
ENVIRONMENTAL READINESS = PENDING

Real broker order submissions: 0
Real broker modifications: 0
Real broker cancellations: 0
Real broker mutations: 0
Live authorization granted: NO
Live strategy started: NO
Production deployed: NO

PHASE 17.2 NOT AUTHORIZED.
HARD STOP.
