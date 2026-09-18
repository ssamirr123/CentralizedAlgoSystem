# Phase 15D.5 — Human Authorization & Controlled Canary Gate

## 1. Objective

Close the last remaining Phase 15D.4 limitation — "no durable
human-authorization audit event" — by building a durable, explicit,
exactly-scoped, single-use, expiring authorization mechanism binding a
human's approval to one specific future order, with a full audit trail.
This phase builds and validates that mechanism only; **it does not use
it to place a real order.**

## 2. Current Baseline

Phase 15D.4 = PASS (1441/0/0/0). Account A `READ_ONLY`, Account B
`READ_ONLY`/FLAT, no strategies running, no live authorization granted.

## 3. Authorization Model

New module: `trading/common/live_authorization.py` — `LiveAuthorization`
(frozen dataclass) + `SqliteLiveAuthorizationStore` (same per-call-
connection + `threading.Lock` discipline as every other store in
`trading.common`: `idempotency_store.py`, `audit_store.py`,
`reconciliation.py` — no new persistence technology introduced).

Fields: `authorization_id`, `account_id`, `broker_id`,
`credential_reference`, `symbol`, `side`, `quantity`, `order_type`,
`product_type`, `max_order_value`, `daily_loss_limit`,
`strategy_loss_limit`, `max_orders_per_day`, `idempotency_key`,
`authorized_by`, `authorization_scope`, `status`, `rejection_reason`,
`consumed_intent_correlation_id`, `authorized_at`, `expires_at`,
`consumed_at`, `revoked_at`, plus deployment identity
(`app_version`/`git_sha`/`deployment_id`/`environment`) and
`created_at`/`updated_at`/`version` for optimistic-concurrency bookkeeping
— matching the brief's required field list exactly, using existing
project naming (`account_id`, `broker_id`, `credential_reference`
mirror `TradingAccount`'s own fields; `idempotency_key` matches
`OrderIntent`'s).

**Not a "live trading enabled" switch**: there is no field anywhere in
this model that grants broad or reusable permission — every authorization
is scoped to one exact bounded action.

## 4. State Machine

```
create()                validate()                  try_consume()
PENDING ----------> AUTHORIZED --------------------------> CONSUMED
   |         \-> REJECTED (structural)                        |
   |                                                            |
   `-------------------- revoke() -----------------------------+---> REVOKED
   |                                                            |
    `------ expires_at elapses (checked + persisted on read) ---+---> EXPIRED
```

`CONSUMED`, `EXPIRED`, `REVOKED`, `REJECTED` are all terminal — no code
path transitions out of any of them (mirrors
`TradingAccount.set_killed()`'s own irreversibility precedent). Tested:
`test_create_starts_pending`, `test_validate_transitions_pending_to_authorized`,
`test_validate_rejects_structurally_invalid`, `test_revoke_pending_or_authorized`,
`test_revoke_terminal_state_fails`.

## 5. Exact-Scope Binding

`try_consume()` requires an exact match on: `account_id`, `broker_id`,
`credential_reference`, `symbol`, `side`, `quantity`, `order_type`,
`product_type`, `idempotency_key` — and `order_value ≤ max_order_value`.
Any single mismatch blocks consumption entirely (the authorization is
**not** partially honored). Tested via 9 parametrized cases
(`test_exact_scope_mismatch_blocks_consumption`) covering every field
individually — quantity, symbol, side, account, broker, credential
reference, order type, product type, idempotency key — plus
`test_order_value_exceeding_max_blocks_consumption` and
`test_exact_match_consumes_successfully`. Confirmed directly: an
authorization for `BUY 65 NIFTY22SEP2623550CE` does **not** authorize
`BUY 130` of it, a different instrument, `SELL` instead of `BUY`, or a
different account.

## 6. Expiration

Default TTL: 600 seconds (10 minutes) — conservative, hard-coded (no
prior project configuration existed for this). Expiry is **durably
persisted**, not merely computed on the fly: `get()` eagerly transitions
an overdue `PENDING`/`AUTHORIZED` record to `EXPIRED` and commits that
transition, so a fresh process reading the same record sees `EXPIRED`
identically (`test_expiry_is_durable_on_read_not_merely_computed`,
`test_scenario_c_expired_survives_restart`). Tests: before-expiry valid
(`test_authorization_before_expiry_is_valid`), at/after-expiry invalid
(`test_authorization_after_expiry_is_invalid`,
`test_expired_authorization_cannot_be_consumed`), deterministic clock
handling via an injectable `now` parameter to `is_expired()`.

## 7. Single-Use Behavior

`try_consume()` is an atomic, guarded `UPDATE ... WHERE authorization_id
= ? AND status = 'AUTHORIZED'` — the exact same compare-and-swap pattern
already established by `SqliteIdempotencyStore.claim()` and
`SqliteReconciliationStore.try_acquire()`. A second consumption attempt
against the same authorization always fails
(`test_replay_case_1_same_auth_same_intent_after_consumed`). Integration
test through the real `execute()` pipeline
(`test_single_use_second_execution_blocked_before_broker`) confirms the
fake broker's `place_order()` is never called a second time — the first
execution succeeds (`place_order_calls == 1`), a second attempt with the
same idempotency key is intercepted by the pre-existing, unchanged
idempotency-replay gate (returns the cached result) before the
authorization layer is even re-consulted; broker call count stays `1`.

## 8. Account Isolation

`test_authorization_for_account_b_cannot_execute_against_account_a`:
an authorization scoped to Account B cannot be consumed against Account
A (`account_id` mismatch → `LiveAuthorizationError`, zero broker calls).
`test_credential_reference_cannot_be_substituted`: `env:ANGELONE_B`
cannot be swapped for `env:ANGELONE_A` at consumption time.

## 9. Kill-Switch Integration

The central kill switch is checked at execute()'s existing step 0 —
unconditional, before every other gate, unchanged. The new authorization
gate runs *after* it, never before, so it can never override or bypass
the kill switch. `test_kill_switch_blocks_even_with_valid_authorization`:
a fully `AUTHORIZED` LiveAuthorization is still blocked, zero broker
calls, and the authorization itself remains untouched (`status` stays
`AUTHORIZED`, not consumed) — proving the kill switch is checked
*before* authorization is ever reached. `test_kill_switch_on_survives_restart_and_still_blocks`
confirms this holds after a simulated restart of the kill switch itself.

## 10. Risk Integration

`test_risk_manager_rejection_still_applies_with_valid_authorization`: a
`RiskManager` configured to reject on quantity still blocks execution
even with a fully valid, matching authorization — zero broker calls, and
the authorization remains unconsumed (`RiskManager` runs *before* the
authorization gate in execute()'s existing pipeline, unchanged).

**Final gate ordering** (existing architecture preserved, new gate
inserted at the one open slot immediately before the broker call):
```
Central Kill Switch (step 0, unconditional)
        ↓
Idempotency replay check (step 1, read-only)
        ↓
Account AuthorizationState gate (step 2 — CANARY_READY/LIVE_AUTHORIZED)
        ↓
RiskManager.validate() (step 3)
        ↓
Mode-specific gate: LiveCanaryGuard / RiskLimits.is_live_ready() (step 4)
        ↓
Broker resolution (step 5)
        ↓
Live Authorization gate (NEW — try_consume(), exact-scope + single-use)
        ↓
Idempotency claim() (atomic, immediately before the broker call)
        ↓
Broker Adapter (place_order)
```
No existing gate was reordered, weakened, or bypassed to make room for
this one.

## 11. Idempotency Integration

The authorization gate runs **before** the idempotency `claim()` call
specifically so an authorization rejection never poisons the idempotency
key (mirrors the exact reasoning already documented for why `claim()`
itself is deferred to immediately before the broker call — see
`idempotency_store.py`'s own docstring on the AG7002 lesson).
`test_authorization_bound_to_idempotency_key_rejects_different_key`
confirms an authorization scoped to `KEY-A` cannot be consumed for an
intent carrying `KEY-B`. Historical idempotency records — `260917000350205`
(`COMPLETED`) and the AG7002 attempt (`PENDING`) — are untouched by
anything in this module (`test_historical_idempotency_records_untouched`,
re-verified fresh again in §17 below).

## 12. Audit Events

Six new event-type constants: `AUTHORIZATION_CREATED`,
`AUTHORIZATION_VALIDATED`, `AUTHORIZATION_REJECTED`,
`AUTHORIZATION_CONSUMED`, `AUTHORIZATION_EXPIRED`,
`AUTHORIZATION_REVOKED` — durably persisted via the same
`PersistentAuditTrail` every other Phase 15D subsystem uses.
`credential_reference` is masked before being written (`mask_credential_reference()`,
reusing `audit_store.redact_string()`); no raw API key, password, MPIN,
TOTP, or token is ever logged — verified directly in
`test_authorization_lifecycle_events_are_durably_audited` (asserts
neither `"api_key"` nor `"totp"` appears anywhere in the recorded detail).
`test_rejected_authorization_is_audited` and
`test_revoked_authorization_is_audited` confirm the failure-path events
are recorded too.

## 13. Audit Correlation

`test_audit_correlation_authorization_to_broker_order` proves the full
chain: `authorization_id → idempotency_key → broker_order_id →
BROKER_ORDER_PLACED audit event`, all queryable by `idempotency_key`
through the existing `by_idempotency_key()` interface. Uses a fake
broker order ID only — no real order was involved.

## 14. Restart/Recovery Tests

All 5 required scenarios pass, each verified by constructing a **fresh**
`SqliteLiveAuthorizationStore` instance against the same on-disk file
(the same "restart" simulation technique used throughout every prior
Phase 15D subsystem):

| Scenario | Test | Result |
|---|---|---|
| A: created, not expired | `test_scenario_a_created_survives_restart_if_not_expired` | remains `AUTHORIZED` |
| B: consumed | `test_scenario_b_consumed_survives_restart` | remains `CONSUMED` |
| C: expired | `test_scenario_c_expired_survives_restart` | remains `EXPIRED` |
| D: revoked | `test_scenario_d_revoked_survives_restart` | remains `REVOKED` |
| E: ambiguous broker response after mutation | `test_scenario_e_ambiguous_broker_response_after_authorization_stays_protected` | existing `STATUS_AMBIGUOUS`/no-retry protections apply unchanged; authorization stays `CONSUMED` (not un-consumed, not retried) |

## 15. Replay Protection

All 8 required cases pass:

| Case | Test |
|---|---|
| 1. Same auth, same intent, after CONSUMED | `test_replay_case_1_same_auth_same_intent_after_consumed` |
| 2. Different idempotency key | `test_replay_case_2_different_idempotency_key` |
| 3. Different quantity | `test_replay_case_3_different_quantity` |
| 4. Different instrument | `test_replay_case_4_different_instrument` |
| 5. Different account | `test_replay_case_5_different_account` |
| 6. After expiry | `test_replay_case_6_after_expiry` |
| 7. After revocation | `test_replay_case_7_after_revocation` |
| 8. After restart | `test_replay_case_8_after_restart` |

## 16. Test Results

New file: `tests/common/test_phase_15d_5_live_authorization.py` — **41
tests**, all passing, fake/mock brokers only. Two genuine test-authoring
mistakes were found and fixed during development (not code defects): an
already-expired-at-creation record correctly reaches `EXPIRED` via
`get()`'s own eager expiry check, not `REJECTED` as first assumed; and a
same-idempotency-key replay is correctly intercepted by the pre-existing
idempotency-replay gate before the new authorization layer is even
re-consulted, returning a cached success rather than a fresh rejection.

Targeted regression (environment isolation, preflight, rejection
handling, `execution.py`, Phase 14.6, Phase 15D-DR — 176 tests): clean,
0 failures, verified via redirect (not pipe) with a trustworthy exit
code.

## 17. Full Regression Results

```
Total:   1488
Passed:  1488
Failed:  0
Skipped: 0
Errors:  0
Exit code: 0
```

Verified directly from `junit_15d5.xml`'s `<testsuite>` attributes and
the redirect-captured `EXIT=` line in `full_15d5.txt` — not inferred
from any background-task notification summary.

## 18. Broker Mutation Count

```
Real broker mutation calls during Phase 15D.5: 0
New live orders during Phase 15D.5: 0
```

## 19. Current Account State (fresh, read-only, this phase)

```
Account A: READ_ONLY (never touched)
Account B: READ_ONLY, position FLAT (fresh get_positions(): quantity=0)
Account B is_live_authorized(): False
Strategies: none running
Live authorization: NOT GRANTED
```

## 20. Known Limitations

1. **This gate is not yet wired into any production ad-hoc authorization
   workflow.** The Phase 15D.2-style manual scripts used for the actual
   canary attempts would need to be updated to explicitly `create()` →
   `validate()` a `LiveAuthorization` and pass its `authorization_id`
   via `OrderIntent.metadata` for a *future* real canary to actually
   exercise this mechanism end-to-end against a real broker. This phase
   builds and proves the mechanism; it does not retrofit it onto the
   historical Attempt 3 flow (which predates this phase and remains
   historical truth, untouched).
2. No scheduled/background worker exists to sweep expired authorizations
   proactively (carried forward from Phase 15D-RECON's equivalent
   limitation for reconciliation) — expiry is enforced correctly and
   durably whenever a record is *read*, but nothing currently reads them
   on a timer.
3. SQLite persistence for this new store has not been load-tested
   cross-process (same carried-forward limitation as every other store
   in this project).
4. No automated rollback tooling exists in this repository (carried
   forward, unchanged).
5. `authorized_by` is a free-text field with no upstream identity/login
   system behind it — this project has no user-authentication layer
   wired into the trading execution path, so "who authorized" is
   whatever string the caller supplies, not independently verified.

## Historical Integrity (re-verified fresh this phase)

```
260917000350205: COMPLETED — unchanged
260917000523943: 0 references in idempotency/reconciliation/audit — unchanged, unattributed
Historical AG7002 record: PENDING — unchanged
Audit hash chain: VALID (verify() = True)
```

---

# FINAL REPORT

## Phase 15D.5 — Human Authorization & Controlled Canary Gate

### Result
```
PASS
```

### Authorization Controls

| Control | Result |
|---|---|
| Explicit human authorization model | PASS |
| Exact action binding | PASS |
| Expiration | PASS |
| Single-use authorization | PASS |
| Account isolation | PASS |
| Credential isolation | PASS |
| Kill-switch integration | PASS |
| Risk integration | PASS |
| Idempotency binding | PASS |
| Replay protection | PASS |
| Durable authorization audit | PASS |
| Restart/recovery | PASS |

### Regression
```
1488 passed
0 failed
0 skipped
0 errors
EXIT=0
```

### Broker Safety
```
Real broker mutation calls during Phase 15D.5: 0
New live orders during Phase 15D.5: 0
```

### Current Live State
```
Account A: READ_ONLY
Account B: READ_ONLY / FLAT
Strategies: STOPPED
Live authorization: NOT GRANTED
```

### Historical Integrity
```
260917000350205 = COMPLETED / unchanged
260917000523943 = unattributed manual close / unchanged
AG7002 historical record = unchanged
Audit hash chain = VALID
```

### Known Limitations
See §20 above — none hidden, none represented as solved.

---

```
PHASE 15D.5 = PASS

NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
HARD STOP.
```
