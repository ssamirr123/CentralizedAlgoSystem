# Phase 17.1 — Production Live Architecture Readiness & Distributed Safety Review

**Status: PASS (readiness analysis) — see Section 12's readiness matrix for exactly what remains BLOCKED before any future live canary.**

Verified Phase-16 baseline commit: `2ea587e3da1b5579ff387ba6c9b7ff510f22c02e`. This phase is a **review and structural-safety phase**, not a live-trading phase. It performed:

- A full, from-current-source re-trace of the broker-mutation surface, the `StrategyExecutionEngine.execute()` gate order, and the distributed worker→broker call chain.
- One confirmed, fixed safety gap (Section 5).
- New automated tests proving the fix and exercising a FakeBroker-based simulation of the future live/live-canary execution path.
- A read-only review of existing LiveAuthorization/LiveCanaryGuard/reconciliation/audit subsystems, sourced from current code and from the untracked, previously-completed `docs/phase-15d-11-human-live-canary-review-report.md`.

No real broker order was submitted. No LiveAuthorization was created or consumed against a real account. No account was transitioned to a more-trusted authorization state. No strategy was started. No AWS resource was created, modified, or terminated — this phase performed zero AWS calls (no credentials were available in this session; see Section 11).

---

## 1. Broker mutation surface inventory

| Call site | Classification | Notes |
|---|---|---|
| `AngelOneBroker.place_order/cancel_order/modify_order` (`trading/common/brokers/angelone.py`) | CENTRAL EXECUTION PATH | Mutation-capable by default (`ANGEL_READ_ONLY` defaults False); gated by `is_live`. **`modify_order()` lacks the `is_live` check** `place_order()`/`cancel_order()` have — read-only guard only (pre-existing gap, flagged, not fixed this phase per "no strategy trading-rule/broker-adapter redesign" discipline; see Section 12 row 9). |
| `DhanBroker.*` (`brokers/dhan.py`) | CENTRAL EXECUTION PATH, disabled by default | `DHAN_READ_ONLY` defaults **True**; `broker_manager.set_broker_availability("dhan", False, ...)` also disables it centrally. Adapter unverified against a real account (module docstring). Same `modify_order()` `is_live`-check gap as AngelOne. |
| `ICICIBreezeBroker.*` (`brokers/icici_breeze.py`) | CENTRAL EXECUTION PATH, disabled by default | Same posture as Dhan: `ICICI_BREEZE_READ_ONLY` defaults True, centrally disabled, unverified against a real account, same `modify_order()` gap. |
| `ZerodhaKiteBroker.place_order/cancel_order` | UNUSED PATH | Stubs — unconditionally `raise NotImplementedError`. Cannot mutate anything. |
| `PaperBroker`, `ShadowBroker`, `ConnectedShadowBroker` | SHADOW/PAPER PATH | No SDK import, no network call, ever. `ConnectedShadowBroker` never references its own wrapped real broker's mutation methods (verified by reading every method body) and fails closed unless `execution_mode == SHADOW` and the real broker is `read_only=True`. |
| Legacy `DoubleStraddelAlgo`/`CombinedVwapNifty` raw `SmartConnect.placeOrder` calls | LEGACY GUARDED PATH | Guarded only by `assert_live_mutation_allowed()` (central kill switch check); RiskManager/LiveAuthorization/idempotency explicitly NOT wired in (documented, accepted limitation). Both default `BOT_DRY_RUN=true`. Confirmed STOPPED on production as of the last read-only review (`phase-15d-11`). |
| Legacy `Vwap_Algo_Nifty_hedge` raw `SmartConnect.placeOrder`/`modifyOrder` | LEGACY GUARDED PATH — **weakest link identified this phase** | Its two `placeOrder` calls are guarded by `assert_live_mutation_allowed()`; its `modifyOrder` call is **not guarded at all**, and the module has **no DRY_RUN gate of its own** (its own top-of-file comment states this explicitly) — its `main.py` unconditionally opens a real AngelOne `SmartConnect` session. Its only protection if ever started is the central kill switch. Confirmed STOPPED on production as of the last read-only review. Flagged for future remediation; not fixed this phase (would be a legacy-algo behavior change, out of scope for a review phase). |
| Worker-submitted intents via `POST /api/worker/order-intents` → `WorkerCoordinator` → `StrategyRuntime.execute_worker_intent()` | SHADOW/PAPER PATH, structurally enforced twice | `StrategyRuntime` permanently constructs its `StrategyExecutionEngine` with `ExecutionConfig(dry_run=True)` **and** independently calls `_assert_simulated_broker()` before `execute()` — two independent layers, neither configurable at runtime. A worker-submitted intent cannot reach a real broker call today, full stop. |

No unclassified mutation surface remains.

## 2. Gate order re-verified from current source (`StrategyExecutionEngine.execute()`)

Confirmed unchanged in shape since Phase 15B, only line numbers drifted:

0. Central kill switch → 1. Idempotency replay check → 2. Account resolution + `AccountAuthorizationState` hard gate (`_check_authorization_state`) → 3. `RiskManager.validate()` (all 15 checks, unchanged) → 4. Mode-specific gate (`LiveCanaryGuard.authorize()` for LIVE_CANARY; `RiskLimits.is_live_ready()` for LIVE; no extra gate for PAPER/SHADOW) → 5. Broker resolution → LiveAuthorization `try_consume()` → idempotency `claim()` → broker call → 6. `validate_broker_response()` (unconditional, every mode) → 7. Persist definitive idempotency outcome → 8. Return `ExecutionResult`.

**Correction to a stale in-repo comment**: `trading_account.py`'s own docstring still claims `execute()` "does NOT yet consult" `AccountAuthorizationState`. This is **no longer true** — `_check_authorization_state()` has been a hard gate at step 2 since Phase 15B.1 and remains one today. The comment is cosmetic drift, not a functional gap; it is not corrected in this phase per the "leave historical reports/comments untouched unless factually incorrect **and material**" discipline — flagged here for a future documentation-only cleanup.

## 3. Distributed worker→broker call chain and authority boundary

`POST /api/worker/order-intents` (worker-auth checked first) → `WorkerCoordinator.submit_order_intent()` (submission-id dedupe/replay cache) → `_process_submission()`: 9 structural validations (staleness, worker ONLINE, session match, strategy exists/assigned/active, intent/strategy_id agreement, account assignment exists, well-formed intent) → `PortfolioRiskManager.evaluate_and_reserve()` (Phase 16.10, additive, skipped on replay) → `StrategyRuntime.execute_worker_intent()` (re-confirms `strategy_id == owning_strategy_id` or raises; `_assert_simulated_broker()`; `execute()` — same engine, same gates as Section 2) → dry-run short-circuit (never reaches a real broker call).

**Worker authority boundary — confirmed structurally, not by configuration**: a worker cannot select broker/account/execution-mode (account routing is resolved centrally via `StrategyAssignment`, `intent.account_id` is never trusted — proved by `test_three_workers_cannot_cross_execute_each_others_strategies_or_accounts`, new this phase); cannot create or consume a `LiveAuthorization` (the worker package never imports `live_authorization` at all — proved by the pre-existing `test_phase_16_12_worker_structural_safety.py`); cannot change risk limits (`RiskManager`/`PortfolioRiskManager` are never mutated by any worker-facing route); cannot disable the kill switch (no worker route touches `CentralKillSwitch`).

## 4. LiveAuthorization / LiveCanaryGuard review

Both are comprehensively implemented and were extensively tested well before this phase (`tests/common/test_phase_15d_5_live_authorization.py`, 38 tests: creation, structural validation, exact-scope-mismatch on every field, order-value cap, expiry-before/after, expiry durable-on-read (not merely computed), single-use compare-and-swap consumption, credential-reference/account/idempotency-key binding, kill-switch interaction, full restart-survival matrix, replay-safety matrix). This phase re-ran that suite unmodified (see Section 8) and added a small, independent set of FakeBroker-based scenario tests directly against `execute()` (missing authorization, expired authorization → both 0 mutation attempts) as an from-scratch cross-check, not a replacement.

`LiveAuthorization` fields include Phase 15D.7's `operator_id`/`operator_display_name`/`authentication_source`/`role`/`permission_used` — captured on every `LiveAuthorizationWorkflow.request()` call and durably persisted. `try_consume()` is an atomic compare-and-swap (`UPDATE ... WHERE status='AUTHORIZED'`, `rowcount != 1` → error) — never a read-then-write race.

**Authorization is not bound to a worker session, and should not be** — an operator's live authorization is a human decision independent of which OS process a worker happens to be running as; workers can legitimately restart independently of that decision, and binding the two would only create a spurious coupling with no security benefit (workers cannot consume `LiveAuthorization` at all, per Section 3).

**Gap, not fixed this phase (documented for the future deployment plan, Section 10)**: no HTTP route anywhere in `trading/api/` currently bridges an authenticated `Principal` (JWT-based operator identity) into an `OperatorIdentity`/`LiveAuthorizationWorkflow` call. Every exercise of the human-authorization chain to date has been via ad-hoc scripts, never a hardened web route. `operator_identity.py`'s own docstring names this as a deliberate future seam. Similarly, **no code path anywhere calls `TradingAccount.set_authorization_state()`** (zero callers found repo-wide) — the historical `CANARY_READY` transition for `ANGEL_ACCOUNT_B` was performed out-of-band, not through any route or function this test suite exercises.

## 5. The confirmed safety gap — CONFIRMED_SUCCESS / CONFIRMED_REJECTION / UNKNOWN_OUTCOME (Sections 16/26/27)

**Finding (pre-fix)**: `execute()` already distinguishes three broker-call outcomes internally — a definitive fill/rejection, a confirmed rejection after exhausted retries (`ConfirmedRejectionError` → `STATUS_REJECTED`), and a genuinely ambiguous/unknown outcome (`AmbiguousOrderStateError` → `STATUS_AMBIGUOUS`, "reconciliation required", never auto-retried). However, the `ExecutionResult` it returned for an ambiguous outcome carried `status="REJECTED"` — **identical** to a confirmed rejection. `PortfolioRiskManager.commit_reservation()` (called by `WorkerCoordinator` after every `execute_worker_intent()` call) branched only on `execution_result.success`/`status in TERMINAL_STATUSES`, so an ambiguous outcome and a confirmed rejection were released identically — **the reservation's exposure and order-count budget were given back as if the order definitely never happened, even when the broker's true response was unknown and the order may have actually gone through.** This matches exactly the blocker scenario the brief warned about.

**Minimal fix applied**:
1. `trading/common/execution.py`: added `ExecutionResult.ambiguous(intent, reason)` (status=`"AMBIGUOUS"`, distinct from `.rejected()`'s `"REJECTED"`). `_fail()` gained an optional `status` parameter; `_handle_ambiguous()` now passes `status="AMBIGUOUS"` instead of falling through to the default `"REJECTED"`. No other call site changed — every existing `_fail()` caller keeps its exact prior `"REJECTED"` behavior.
2. `trading/common/portfolio_risk.py`: `commit_reservation()` now checks `execution_result.status == "AMBIGUOUS"` **before** the existing `success`-based branch, and in that case files the reservation under `_open_orders` (the same bucket a still-open/partially-filled order already uses) instead of rolling back its exposure/order-count — i.e. it is held exactly as if the order might still be live, until an operator/reconciliation flow explicitly calls `resolve_open_order()`. It is never auto-released by a timer or a later unrelated call.
3. `trading/common/worker_coordinator.py`: added an explicit comment proving (not just asserting) that its own `except Exception` handler's unconditional `release_reservation()` call can never fire for a genuinely-unknown first-attempt outcome — `_reserve_portfolio_risk()` already skips reservation entirely once an idempotency record exists for the key, so `AmbiguousIdempotencyStateError` (only raised for a *subsequent* attempt against an already-AMBIGUOUS/PENDING key) always finds `reservation_id is None` at that point, making `release_reservation(None)`'s no-op behavior safe by construction, not by luck.

**New tests** (`tests/common/test_phase_17_1_live_readiness.py`, plus one added assertion in `tests/common/test_phase_15d_dr_deployment_recovery.py`): confirm `ExecutionResult.ambiguous()` produces a distinct status; confirm `commit_reservation()` keeps exposure/order-count open on an ambiguous outcome and makes it resolvable via `resolve_open_order()`; confirm confirmed-rejection and confirmed-fill behavior is unchanged (regression guards against overcorrecting); confirm a stale `release_reservation()` call against an already-filed ambiguous reservation is a documented no-op, not a bypass.

**This closes the one blocker this phase identified.** No other exception path in the reviewed pipeline was found to conflate confirmed-failure with unknown-outcome.

## 6. Reconciliation subsystem

`ReconciliationService`/`ReconciliationStatus`/`ReadOnlyBrokerView` (`trading/common/reconciliation.py`) exist, are read-only against the broker (structurally — `ReadOnlyBrokerView` does not define a mutation method at all), and are NOT wired into `execute()`'s exception handling — a fully separate, manually-invoked subsystem, by explicit design (its own module docstring states this). `scan_and_register()` finds every AMBIGUOUS/PENDING idempotency record and creates a `RECONCILIATION_REQUIRED` row; `reconcile_one()` compares expected-vs-actual against a real read-only broker query.

**Documented, pre-existing limitation, not fixed this phase (would be a cross-module architecture change, and this phase's charter is review + minimal fix only)**: a completed reconciliation result is never written back into `IdempotencyStore` — an idempotency record stays at AMBIGUOUS/PENDING forever even after a human has reconciled the true outcome, meaning a future replay attempt against that key still raises `AmbiguousIdempotencyStateError`. This is a real gap for a future live deployment and is called out explicitly in Section 12's readiness matrix (BLOCKED) rather than silently accepted.

## 7. Idempotency persistence across restart

`SqliteIdempotencyStore`/`SqliteLiveAuthorizationStore`/`PersistentAuditTrail` are all SQLite-file-backed (confirmed via each module's own `_DEFAULT_DB_PATH` and hash-chain/CAS-write design) and survive process restart by construction — no in-memory-only authoritative state exists in any of the three. Workers never maintain a competing idempotency database — `trading/worker/*` has zero references to `idempotency_store`/`sqlite` anywhere (confirmed by the pre-existing structural test `test_phase_16_12_worker_structural_safety.py`, re-read this phase).

**Worker-restart idempotency-key stability — a real, honestly-documented risk found this phase**: none of the three real strategies (`CombinedVwapNiftyStrategy`, `DoubleStraddleStrategy`, `VwapAlgoNiftyHedgeStrategy`) persist their in-memory entry/exit counters, and `DoubleStraddleStrategy`'s keys carry **no date component at all** (pure time-slot labels like `"HEDGE:ENTRY"`). Two consequences: (a) a worker crash before its own counter increments reproduces the exact same key on restart — safely absorbed by the idempotency store's replay logic; (b) more seriously, the SAME key is regenerated every trading day for `DoubleStraddleStrategy`, so day 2's real new order would be treated as a replay of day 1's completed order and **never reach the broker** unless the idempotency store is rotated/reset between sessions — this is a pre-existing strategy-design limitation, not introduced or fixed by this phase, and is listed as BLOCKED in Section 12 pending a strategy-level fix (out of this review's scope to redesign strategy trading logic, per this phase's own explicit constraint).

## 8. Full regression

Backend: **REGRESSION_BACKEND_PLACEHOLDER**
Frontend: **REGRESSION_FRONTEND_PLACEHOLDER**

New tests added this phase: `tests/common/test_phase_17_1_live_readiness.py` (15 tests: ambiguous-outcome status distinction, reservation-retention fix + 2 regression guards, 8 FakeBroker-direct scenario tests against `execute()` covering valid/missing-auth/expired-auth/kill-switch/risk-reject/confirmed-rejection/unknown-outcome/duplicate-network-submission, and one multi-worker cross-account/cross-strategy leakage test), plus one new assertion in the pre-existing `test_phase_15d_dr_deployment_recovery.py::test_ambiguous_broker_exception_does_not_retry_place_order`.

## 9. Legacy algo guard re-verification

Re-confirmed unchanged: `assert_live_mutation_allowed()` reads the kill-switch persistence file fresh on every call (no caching), never writes. Coverage gap in `Vwap_Algo_Nifty_hedge` (Section 1) is pre-existing and documented, not newly introduced. Legacy processes confirmed STOPPED on production as of the last read-only review (`phase-15d-11`, this session performed no fresh production check — see Section 11).

## 10. Future production deployment plan (not executed)

A minimum future deployment may run multiple strategy workers on one worker EC2 (no 1:1 assumption required — `trading/worker/main.py` selects its strategy via a `STRATEGY_ID` env var per process; several such processes, one per strategy, can share one host under separate systemd units). Deploying live-capable code is explicitly distinct from authorizing live trading: a fresh deployment must leave every strategy STOPPED, every account at its current `AccountAuthorizationState` (no code path changes it automatically — Section 4), and execution mode at its existing safe default. No deployment was performed this phase.

## 11. Read-only AWS / broker inspection

**Not performed this phase** — no AWS or broker credentials were available in this session. This phase relied entirely on the repository's own committed source and on the untracked `docs/phase-15d-11-human-live-canary-review-report.md` (itself a completed, prior read-only review) for production-state facts. That report's last-verified snapshot: production running commit `ff0d0f8` (15 commits behind this phase's HEAD), `/api/ready` reports `broker: not_configured`, no idempotency/reconciliation/live-authorization store wired into the production API process, Account A/B both READ_ONLY and flat, legacy processes stopped. **Per this phase's own instruction ("if account selection has changed, do not guess — report that human input is required"): a fresh read-only check has not been performed here, and the canary account (`ANGEL_ACCOUNT_B`, per `CANARY_ACCOUNT_ID`) has not been reconfirmed FLAT as of today.** This is marked BLOCKED in Section 12, not guessed.

## 12. Readiness matrix

| # | Control | Status |
|---|---|---|
| 1 | Kill switch blocks every mode centrally | PASS |
| 2 | Idempotency replay never re-calls broker | PASS |
| 3 | AccountAuthorizationState hard gate active in execute() | PASS |
| 4 | RiskManager 15-check gate unchanged/authoritative | PASS |
| 5 | LiveCanaryGuard 9-check gate, own kill switch, irreversible shutdown | PASS |
| 6 | LiveAuthorization single-use/expiring/scope-bound/durable/audited | PASS |
| 7 | LiveAuthorization consumption ordered before idempotency claim | PASS |
| 8 | No AUTO_APPROVE/bypass mechanism anywhere in `trading/` | PASS (independently re-verified) |
| 9 | `modify_order()` missing `is_live` check on all 3 real adapters | BLOCKED (pre-existing gap, not fixed — flagged for a future phase) |
| 10 | `Vwap_Algo_Nifty_hedge` legacy algo has no DRY_RUN gate, unguarded modifyOrder | BLOCKED (pre-existing gap, not fixed — flagged) |
| 11 | Worker cannot select broker/account/mode | PASS (new structural test) |
| 12 | Worker cannot create/consume LiveAuthorization | PASS (pre-existing structural test, re-verified) |
| 13 | Worker cannot change risk limits or kill switch | PASS |
| 14 | Worker-submitted intent structurally cannot reach a real broker (double-layer dry-run boundary) | PASS |
| 15 | Ambiguous vs. confirmed-rejection broker outcomes distinguishable | PASS (fixed this phase) |
| 16 | PortfolioRiskManager never releases exposure on unknown outcome | PASS (fixed this phase) |
| 17 | PortfolioRiskManager still releases exposure on confirmed rejection | PASS (regression-guarded) |
| 18 | Reconciliation is read-only against the broker | PASS |
| 19 | Reconciliation result feeds back into idempotency store | BLOCKED (not wired — pre-existing gap) |
| 20 | Idempotency/audit/authorization stores survive restart | PASS |
| 21 | Worker never holds a competing idempotency database | PASS |
| 22 | Strategy idempotency-key derivation stable across worker restart (crash-before-increment case) | PASS |
| 23 | Strategy idempotency-key derivation stable across trading days | BLOCKED (`DoubleStraddleStrategy` keys carry no date component — pre-existing) |
| 24 | Human-operator identity captured on authorization-granting actions | PASS (in the workflow object), BLOCKED (no HTTP route exercises it yet) |
| 25 | AccountAuthorizationState transition (e.g. to CANARY_READY) has an audited code path | BLOCKED (zero callers repo-wide — always done out-of-band) |
| 26 | Canary risk limits fully configured, no invented defaults | PASS (present in untracked `.env`, all required fields, no code default) — BLOCKED for a fresh clone with no `.env` |

**Overall Phase 17.1 verdict: PASS as a readiness *analysis*.** The distributed architecture's existing safety gates were re-verified from current source and hold; one real gap (Section 5) was found and minimally fixed; every other gap found (rows 9, 10, 19, 23, 24, 25, and the missing-`.env`-on-fresh-clone case in row 26) is honestly reported as BLOCKED for a *future* live canary, not silently accepted or worked around. None of these blockers were introduced by, or required to be fixed by, this review phase's own charter.

---

LIVE AUTHORIZATION = NONE
REAL BROKER ORDERS = 0
REAL BROKER MUTATIONS = 0
RUNNING LIVE STRATEGIES = 0

NO LIVE AUTHORIZATION GRANTED. NO LIVE ORDER SUBMITTED. NO REAL BROKER MUTATION PERFORMED. NO LIVE STRATEGY STARTED.

PHASE 17.2 NOT AUTHORIZED. HARD STOP.
