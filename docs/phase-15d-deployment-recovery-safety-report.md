# Phase 15D-DR — Deployment, Restart & Recovery Safety

**No real broker order was placed. No real broker mutation API was called. No strategy was started. No account was promoted. Account A and Account B remain `READ_ONLY`.**

## 1. Executive summary

This phase audited and hardened the platform against software failure, deployment, restart, timeout, duplicate-request, and rollback scenarios that could otherwise create a duplicate real order or bypass the existing authorization boundary. Two **genuine, previously-unfixed safety gaps** were found and closed:

1. **Ambiguous broker responses were blindly retried** (`StrategyExecutionEngine._retry()`) — any exception from the mutating `place_order()` call itself (network timeout, connection reset, rate limit) was treated identically to a confirmed rejection and triggered an automatic retry with a re-priced order, risking a genuine duplicate real order if the first attempt had actually reached the broker.
2. **A race window existed between the idempotency check and the idempotency persist** — two concurrent `execute()` calls for the same idempotency key could both pass the initial replay check before either persisted an outcome, both proceeding to the broker.

Both are now closed with targeted, minimal, additive changes — no existing safety gate was weakened, and every existing test continues to pass unchanged except one pre-existing test whose hardcoded response-key assertion needed updating to reflect a deliberate, in-scope addition (deployment version fields on `/api/health`).

## 2. Existing deployment architecture (as found)

- No CI/CD, container orchestration, or automated rollback tooling exists anywhere in this repository — deployment today is manual (no `Dockerfile`, `k8s/`, or deploy scripts were found).
- `trading/api/app.py`'s FastAPI `lifespan` context manager is the only startup/shutdown lifecycle hook; it initializes the database, bootstraps the first admin user, and starts an optional background heartbeat watcher — nothing related to trading authorization.
- `GET /api/health` (`trading/api/health.py`) already existed, reporting process/DB status only — no version/build identity, no broker/trading state.
- `CentralKillSwitch` (Phase 14.6) was **in-memory only**, explicitly documented as resetting to disengaged (the safe default) on restart — this phase closes that gap (see Section 5).
- `SqliteIdempotencyStore` (Phase 14.6) already persisted to a local SQLite file, restart-safe by construction — confirmed still correct, and extended with an atomic `claim()` operation (see Section 4).
- `TradingAccount`/`AccountAuthorizationState` (Phase 15B/15B.1) has **no persistent store at all** — every account is constructed fresh, in code, at process start. This is a structural finding in its own right (Section 9).
- `AuditTrail` (Phase 13) is an in-memory, hash-chained list — **does not survive a restart**. Flagged as a known limitation (Section 15), not fixed in this pass (a persistent audit sink is a larger, separate undertaking).

## 3. Changes implemented

| File | Change |
|---|---|
| `trading/common/execution.py` | New `AmbiguousOrderStateError`; `_retry()` re-raises it immediately instead of looping; `place_limit()`/`place_market_emergency()` wrap the mutating `place_order()` call to raise it on any exception; `execute()` catches it via a new `_handle_ambiguous()` helper, persisting `STATUS_AMBIGUOUS`; the idempotency `claim()` call was inserted immediately before the broker call (not earlier — see Section 4); `_check_idempotency_replay()` now treats `STATUS_AMBIGUOUS`/`STATUS_PENDING` identically, raising `AmbiguousIdempotencyStateError` rather than silently replaying or resubmitting. |
| `trading/common/idempotency_store.py` | New `STATUS_AMBIGUOUS`, `STATUS_PENDING` constants; new `AmbiguousIdempotencyStateError`; new `claim()` method on the `IdempotencyStore` protocol, `SqliteIdempotencyStore` (a bare `INSERT` against the existing `PRIMARY KEY`, relying on `IntegrityError` for atomicity), and `InMemoryIdempotencyStore`. |
| `trading/common/kill_switch.py` | New optional `persistence_path` constructor argument. Omitted (the default): byte-for-byte identical to the original in-memory-only behavior. Given: `engage()`/`disengage()` write state to a small JSON file; `__init__` loads prior state; an unreadable persistence file fails closed (loads as `engaged=True`) rather than silently defaulting to disengaged. |
| `trading/common/deployment_info.py` (new) | `get_deployment_info()` (app version, Git SHA, deployment ID, environment, startup timestamp) and `log_startup_banner()`. Explicitly carries no trading-authorization field. |
| `trading/api/health.py` | `GET /api/health` now also returns `app_version`/`git_sha`/`deployment_id`/`environment`. New, separate `GET /api/ready` reports `application`/`broker`/`trading_authorized` as three explicitly distinct fields — `trading_authorized` is hard-coded `False`. |
| `trading/api/app.py` | Calls `log_startup_banner()` at the start of `lifespan()`. |
| `tests/api/test_health.py` | Updated the hardcoded response-key set to include the four new, intentional version fields (the same "stale exact-count assertion" pattern fixed in Phase 14.6/15B). |
| `tests/common/test_phase_14_6_hardening.py` | `_BoomStore` (a pre-existing custom `IdempotencyStore`-shaped test double for the "write failure does not affect the result" test) needed a `claim()` method added -- a genuine, expected consequence of extending the `IdempotencyStore` interface with a new required method, not a stale/unrelated fix. `claim()` returns `True` (succeeds) so the test still exercises exactly what it always intended: a failure specifically in `put()` (persist), not in claiming. |

No existing safety gate (`RiskManager`, `LiveCanaryGuard`, `AuthorizationState` gate, `CentralKillSwitch`'s default-disengaged behavior, `TradingAccountRouter`, `BrokerAdapterFactory`) was modified in a way that weakens it.

## 4. Idempotency behavior

Two layers, both required:

1. **Replay** (`_check_idempotency_replay`, unchanged in position, extended in behavior): a prior `STATUS_COMPLETED`/`STATUS_REJECTED` record is replayed without a new broker call. A prior `STATUS_AMBIGUOUS` or `STATUS_PENDING` record now raises `AmbiguousIdempotencyStateError` — **never** silently replayed, **never** silently allowed to proceed to a fresh submission.
2. **Claim** (new): immediately before the broker is actually called (after every rejection-capable gate — authorization, RiskManager, LiveCanaryGuard — has already passed, so a routine rejection never poisons the key), `claim()` atomically inserts a `STATUS_PENDING` placeholder. If another concurrent request already claimed the same key, this request is rejected before ever reaching the broker. This closes the check-then-act race a plain `get()`-then-`put()` could not.

Restart safety: `SqliteIdempotencyStore` was already restart-safe (a real file); this phase adds tests proving it explicitly for the ambiguous/pending case (a "crashed mid-execution" `PENDING` record survives a simulated restart and correctly blocks a naive resubmission rather than silently resuming it).

## 5. Broker timeout / ambiguous response handling

See Section 3. The fix is narrow and conservative: **any** exception raised by the mutating `place_order()` call itself is now treated as ambiguous and never retried automatically — this trades a small amount of resilience (a transient, definitely-not-yet-submitted network blip now requires a fresh, explicit `execute()` call under a new key rather than an automatic retry) for the far more important guarantee that a possibly-already-submitted order is never resubmitted blindly. A **confirmed** `REJECTED` broker response (a normal `OrderResult`, not an exception) is unaffected and remains safely retryable with a re-priced order, exactly as before — proven by a dedicated regression test (`test_confirmed_rejected_status_is_still_safely_retried_with_new_price`).

## 6. Ambiguous response handling — reconciliation model

```
Broker call raises (timeout / reset / unknown response)
        ↓
AmbiguousOrderStateError (never retried)
        ↓
Persisted as STATUS_AMBIGUOUS
        ↓
Any future execute() call under the SAME idempotency_key
        ↓
AmbiguousIdempotencyStateError (raised, not caught)
        ↓
Human/operator must reconcile real broker state (order book / positions)
        ↓
Only then does a NEW idempotency_key (a deliberately fresh, distinct request)
proceed normally
```

This engine does **not** attempt automatic reconciliation — that would require querying the real broker's order book by symbol/time-window heuristics, which is exactly the kind of automated, unsupervised broker interaction this phase's own hard safety boundary forbids. Reconciliation remains a deliberate, human/operator action, matching every prior phase's own established authorization philosophy.

## 7. Kill-switch behavior

- Default (`CentralKillSwitch()`, no `persistence_path`): unchanged, in-memory only, resets to disengaged on restart — every existing caller (`trading/api/execution_state.py`, every existing test) is unaffected.
- With `persistence_path`: engaged/disengaged state now survives a restart, tested end-to-end (engage → simulated restart → still blocks a fresh `execute()` call). An unreadable persistence file fails closed (loads engaged) rather than silently resuming as disengaged.
- Engaging the switch still blocks every execution mode unconditionally, checked first in `execute()`, unchanged from Phase 14.6.
- Disengaging never restores or grants any other authorization — unchanged.

## 8. Account isolation

Re-verified (not re-architected) across a simulated restart: two accounts sharing one `SqliteIdempotencyStore` file, each executing under their own idempotency key, produce two records each correctly tagged with their own `account_id` after a fresh store instance reads the same file — no cross-account leakage. Credential isolation, risk-limit isolation, and authorization-state isolation were already proven in Phases 15B–15C.5 and are unaffected by this phase's changes (no shared code path between account resolution and the idempotency/kill-switch fixes above).

## 9. Rollback behavior

**No automated rollback tooling exists in this repository** (Section 2) — there is nothing to audit beyond the architectural guarantee this phase confirms holds by construction: no code path anywhere in `trading/` automatically closes, reverses, or modifies a broker position in response to a software rollback, redeploy, or version change, because no such "rollback reacts to trading state" code exists at all. This is a **vacuous** safety property today (true only because the feature doesn't exist), not an actively-enforced one — flagged honestly as a known limitation (Section 15) rather than claimed as a tested guarantee. If automated deployment/rollback tooling is added in the future, it must be built to explicitly preserve: audit data, idempotency records, broker order IDs, authorization state, and kill-switch state, and must never auto-reverse a position — this phase documents the requirement for that future work rather than building infrastructure that does not otherwise exist.

## 10. Persistence behavior

| State | Persisted? | Restart-safe? |
|---|---|---|
| Idempotency records | Yes (SQLite file) | Yes — confirmed, extended with `claim()` |
| Kill switch | Optional (new, this phase) | Yes, when `persistence_path` is given; no (by design) otherwise |
| `TradingAccount.authorization_state` | **No** | N/A — see Section 2; safe by construction (always defaults to `READ_ONLY`), not because it survives a restart |
| Audit trail | **No** (in-memory list) | **No — known limitation, unresolved this phase** |
| Risk limits (`RiskLimits`/`CanaryLimits`) | No (constructed from code/config each run) | Effectively yes, in the sense that the same code produces the same limits every run — not because any value is stored and reloaded |

Database-unavailable behavior: `SqliteIdempotencyStore`'s methods raise on failure (no silent fallback), and `StrategyExecutionEngine.execute()` does not catch a broken idempotency store — an unavailable idempotency store now provably fails `execute()` closed (raises) rather than silently proceeding as if no idempotency protection existed (`test_idempotency_store_unavailable_fails_closed_not_silently_in_memory`).

## 11. Order state machine

The literal, fully-enumerated state machine this phase's brief describes (`CREATED → RISK_APPROVED → AUTHORIZED → SUBMISSION_ATTEMPTED → BROKER_ACCEPTED → RECONCILING → FILLED/REJECTED/UNKNOWN`) was **deliberately not built as a new, formal enum**. The existing pipeline already expresses an equivalent set of states implicitly, at lower risk than a broad refactor: `RiskCheckResult`/`canary_result` (risk/canary approval), `ExecutionResult.status` (`FILLED`/`COMPLETE`/`REJECTED`/`CANCELLED`/`OPEN`), and now `IdempotencyRecord.status` (`STATUS_PENDING` → `STATUS_COMPLETED`/`STATUS_REJECTED`/`STATUS_FAILED`/`STATUS_AMBIGUOUS`). The one genuinely missing state — "submitted, but the outcome is unknown" — is exactly what `STATUS_AMBIGUOUS`/`STATUS_PENDING` now add. Introducing a single, unified, formal enum spanning every layer would be a large, cross-cutting refactor with real regression risk for a project already this deep into safety hardening; this is a deliberate scoping decision, not an oversight.

## 12. Test matrix

`tests/common/test_phase_15d_dr_deployment_recovery.py` (21 tests) + `tests/common/test_phase_15d_dr_deployment_smoke.py` (13 tests) = 34 new tests, covering:

Restart before/after intent, restart after idempotency persistence, restart after ambiguous broker response, restart with a leftover `PENDING` record, duplicate idempotency request, concurrent duplicate requests (real threads, real race), observability failure after broker acceptance (Blocker C re-verified), kill-switch persistence (engaged/disengaged) across restart, kill-switch end-to-end block + persistence, kill-switch fail-closed on an unreadable persistence file, account isolation across a simulated restart, idempotency-store-unavailable fail-closed, `READ_ONLY`-by-construction after "restart", `KILLED` has no reversal path, deployment info carries no authorization signal, readiness endpoint never reports `trading_authorized=True`, plus the full deployment smoke-test list (Area K's 13 applicable items — items 10/14/15 from that list, which need a real running server process, were not run as a live process boot; the FastAPI route functions themselves were exercised directly instead, which is the same level of rigor this project's existing test suite already uses for every other route).

## 13. Full regression result

```
Total collected: 1340
Passed:          1340
Failed:          0
Skipped:         0
```

One genuine, expected failure was found and fixed during this phase's own testing (Section 3's `_BoomStore` fix) — not a pre-existing or hidden failure; the fresh, final run above confirms 0 failures.

## 14. Real broker mutation count

```
Real broker authentication calls: 0
Real broker read-only calls: 0
Real broker mutation calls: 0
Real order-placement calls: 0
Real orders: 0
Strategies started: 0
```

Every test in this phase uses `PaperBroker`, in-process fakes/doubles, or a real (local, non-broker) `SqliteIdempotencyStore`/`CentralKillSwitch` file.

## 15. Known limitations

1. **Audit trail (`AuditTrail`) is in-memory only** — does not survive a restart. A crash or redeploy loses the audit log for any orders executed since the last... there is no persistent audit sink at all. This is a real gap for a production trading system and is the single most important follow-up from this phase.
2. **No persistent `TradingAccount`/`authorization_state` store exists** — safe today because every account defaults to `READ_ONLY` on construction, but this means a genuine `LIVE_AUTHORIZED` promotion (once that workflow exists) would also not survive a restart unless a future phase adds one — which is arguably the SAFER default (a restart should probably require re-authorization anyway), but is called out explicitly rather than left implicit.
3. **No automated deployment/rollback tooling exists** — the "rollback never reverses a position" property holds vacuously (Section 9), not because it was tested against real rollback automation.
4. **The full formal order-state-machine enum was not built** — see Section 11's reasoning.
5. **Reconciliation after an ambiguous response is manual/human, not automated** — by design (Section 6), not a gap, but worth stating plainly: this phase does not build a broker-order-book reconciliation tool.
6. **`claim()`'s atomicity for `SqliteIdempotencyStore` relies on SQLite's own file-level locking** across processes — proven correct for concurrent threads within one process (the tested scenario) and reasoned to hold across processes via SQLite's standard guarantees, but not separately load-tested with multiple real OS processes in this phase.

## 16. Remaining risks

- A restart occurring in the exact window between `claim()` succeeding and the broker call completing would leave a `PENDING` record requiring manual reconciliation — this is the intended, safe behavior (Area E/J), but operationally means an operator must have a runbook for "how do I reconcile a `PENDING`/`AMBIGUOUS` idempotency record," which does not yet exist as written documentation beyond this report.
- The audit-trail persistence gap (Known Limitation 1) means post-incident forensics for anything that happened since the last restart before a crash may be incomplete.

## 17. Exact next step

1. (Recommended, not required by this phase) Build a persistent audit-trail sink, closing Known Limitation 1 — the highest-value remaining gap this phase surfaced.
2. Otherwise: this phase's own hard stop applies — **do not start Phase 15D.2**, do not request live-order authorization, and wait for explicit human instruction before any further real-money activity.

## 18. Final safety gate checklist

- [x] No real order API called
- [x] No real mutation API called
- [x] No strategy started
- [x] No authorization escalation
- [x] Account A remains READ_ONLY
- [x] Account B remains safely controlled
- [x] Kill switch tested (default behavior + new persistence)
- [x] Idempotency tested across restart
- [x] Ambiguous broker response does not retry
- [x] Deployment does not authorize trading
- [x] Rollback does not reverse broker positions (vacuously true — see Section 9/15)
- [x] Persistence failure fails closed
- [x] Full regression completed
- [x] Report generated
