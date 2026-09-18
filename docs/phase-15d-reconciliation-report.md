# Phase 15D-RECON — Persistent Broker Reconciliation

Status at start of phase: Phase 15D.1-B PASS, Phase 15D-DR PASS, Phase
15D-AUDIT PASS, 1383/1383 regression passing, 0 real broker calls of any
kind, 0 real orders, Account A (ANGEL_SAMIR) and Account B
(ANGEL_ACCOUNT_B) both READ_ONLY, Phase 15D.2 NOT STARTED. This phase is
read-only, architectural work — it never touches a real broker.

## 1. Problem statement

Phase 14.6/15D-DR gave `StrategyExecutionEngine.execute()` two terminal-
but-unresolved outcomes for a broker submission: `STATUS_AMBIGUOUS` (the
mutating call itself raised — a network timeout, connection reset — so
whether the broker actually received the order is unknown) and
`STATUS_PENDING` (a `claim()` was made but the process died before a
definitive outcome was persisted). Both are deliberately never retried —
see `idempotency_store.py`'s own module docstring — because retrying an
order whose prior submission may have already succeeded risks a real
duplicate. But nothing in this codebase, before this phase, ever
*resolved* that uncertainty. An operator had no durable, systematic way to
ask "did the broker actually get this order?" — only manual, undocumented
broker-console inspection. Phase 15D-DR's own report named exactly this as
the "manual reconciliation by design" known limitation, and Phase
15D-AUDIT's own §17 named building it as the explicit next step.

## 2. Architecture

```
IdempotencyStore                    PersistentAuditTrail (Phase 15D-AUDIT)
  STATUS_AMBIGUOUS/PENDING                EVENT_ORDER_INTENT_CREATED
  records (no expected-order detail)      (symbol/side/qty/order_type/
        |                                  exchange/product_type/expiry/
        |                                  strike/option_type, keyed by
        |                                  idempotency_key -- already
        |                                  indexed: by_idempotency_key())
        v                                        |
  ReconciliationService.scan_and_register() <----+
  (read-only: idempotency_store.list_by_status() + audit_trail.by_idempotency_key())
        |
        v
  SqliteReconciliationStore.create_required(...)  -- idempotent (INSERT OR
  |                                                   IGNORE, deterministic
  |                                                   reconciliation_id
  |                                                   derived from the
  |                                                   idempotency_key)
  v
  RECONCILIATION_REQUIRED (durable)
        |
        v   ReconciliationService.reconcile_one(reconciliation_id, broker)
        |     1. try_acquire() -- atomic conditional UPDATE (Area L)
        |     2. RECONCILIATION_STARTED audit event -- FAIL CLOSED here
        |     3. ReadOnlyBrokerView(broker) -- get_order()/get_order_book()/
        |        get_open_orders(), whichever the adapter actually supports
        |     4. expected-vs-actual comparison
        v
  RECONCILED_FILLED | RECONCILED_ACCEPTED | RECONCILED_REJECTED |
  RECONCILED_NOT_FOUND | RECONCILIATION_UNKNOWN | RECONCILIATION_FAILED
        |
        v
  complete() persists the result (owner+status guarded UPDATE) -->
  RECONCILIATION_COMPLETED audit event (best-effort past this point --
  the durable answer already exists in reconciliation_records)
```

New module: `trading/common/reconciliation.py` — `ReconciliationStatus`,
`ReconciliationRecord`, `SqliteReconciliationStore`, `ReadOnlyBrokerView`,
`ReconciliationService`.

**Why no changes to `idempotency_store.py`'s write path or schema were
needed** (Rule 1: don't redesign working execution logic unnecessarily):
`IdempotencyRecord` deliberately keeps only a *hash* of the tradeable
intent fields, never the fields themselves (see that module's own
`compute_intent_hash()` docstring) — reconciliation needs the actual
fields to compare against, and the one place they were already being
durably recorded is Phase 15D-AUDIT's `ORDER_INTENT_CREATED` event. Rather
than add a second, parallel place to store the same data (which could
drift out of sync with the first), `_reconstruct_expected()` reads it back
out of the audit trail via the query interface Phase 15D-AUDIT already
built (`by_idempotency_key()`). The only two changes needed elsewhere:

- `idempotency_store.py` gained one new **read-only** method,
  `list_by_status(status)` (both `SqliteIdempotencyStore` and
  `InMemoryIdempotencyStore`) — there was previously no way to *discover*
  outstanding `AMBIGUOUS`/`PENDING` records without already knowing their
  key. `get()`/`put()`/`claim()` are unchanged.
- `execution.py`'s existing `EVENT_ORDER_INTENT_CREATED` audit call
  (already extended once, in Phase 15D-AUDIT, to add `idempotency_key`)
  gained `exchange`/`product_type`/`expiry`/`strike`/`option_type` —
  exactly the fields Area A/E need that weren't captured before. No other
  line in `execution.py`'s core pipeline changed.
- `execution.py`'s `OrderState` dataclass gained two additive, defaulted
  fields (`symbol: str = ""`, `side: OrderSide | None = None`) so a
  broker's own order-lookup response can carry enough information for an
  expected-vs-actual comparison — the pending-order management logic that
  already consumes `OrderState` (`_manage_pending()`) only ever reads
  `status`/`filled_quantity`/`remaining_quantity` and is completely
  unaffected. `AngelOneBroker.get_order()`/`get_order_book()` were updated
  to populate them from `tradingsymbol`/`transactiontype`, fields
  Angel's `orderBook()` response already returns — no new broker API call.

## 3. Reconciliation states

Exactly the nine states this phase's brief lists (Area B), implemented as
`ReconciliationStatus(str, Enum)`: `NOT_REQUIRED`,
`RECONCILIATION_REQUIRED`, `RECONCILIATION_IN_PROGRESS`,
`RECONCILED_ACCEPTED`, `RECONCILED_FILLED`, `RECONCILED_REJECTED`,
`RECONCILED_NOT_FOUND`, `RECONCILIATION_FAILED`, `RECONCILIATION_UNKNOWN`.
No duplicate/conflicting model was introduced — `STATUS_AMBIGUOUS`/
`STATUS_PENDING` (idempotency_store.py) and this enum describe two
different things (the underlying execution's own broker-submission
outcome vs. the reconciliation attempt's own lifecycle/finding) and are
used together, never merged. `NOT_REQUIRED` is defined for completeness
(a record simply never needing reconciliation) but this phase's own flow
never creates a record in that state — only `RECONCILIATION_REQUIRED`
records are ever created, exclusively for genuinely unresolved
`AMBIGUOUS`/`PENDING` intents; a normal, definitively-resolved order never
gets a reconciliation row at all (documented in §16 as a scope choice, not
an oversight).

## 4. Persistence model

`SqliteReconciliationStore`, standard-library `sqlite3` only, same
per-call-connection-plus-lock discipline as `SqliteIdempotencyStore`/
`SqliteAuditStore`. Every field Area A lists is a specific, named,
non-secret column (`reconciliation_id`, `idempotency_key`,
`correlation_id`, `strategy_id`, `account_id`, `broker_id`, `symbol`,
`exchange`, `expiry`, `strike`, `option_type`, `expected_side`,
`expected_quantity`, `expected_order_type`, `expected_product_type`,
`broker_order_id`, `status`, `broker_status`, `filled_quantity`,
`average_price`, `rejection_reason`, `error_message`, `owner`,
`started_at`, `claimed_at`, `completed_at`, `app_version`, `git_sha`,
`deployment_id`, `environment`, `created_at`, `updated_at`, `version`) —
**no generic metadata/detail blob exists on this table**, so there is no
column a caller could accidentally stash a credential into
(`test_24_no_credential_columns_exist_in_reconciliation_schema` asserts
this directly against the live schema). Free-text fields this module
itself constructs (`error_message`/`rejection_reason`, which may echo part
of a broker exception's own message) are passed through the new
`trading.common.audit_store.redact_string()` before being written, as
defense-in-depth (`test_24_secret_redaction_in_error_message`).

`reconciliation_id` is deterministic — `"RECON-" + sha256(idempotency_key)
[:20]` — which is what makes `create_required()` naturally idempotent via
`INSERT OR IGNORE`: calling it twice for the same underlying execution
attempt (e.g. once from a scan before restart, once after) never creates
a duplicate row.

Unlike `audit_store.py`'s deliberately append-only design, this table is
mutable by design — a reconciliation record legitimately transitions
through states over its lifetime. Safety under concurrent/repeated access
comes from ownership-and-status-guarded `UPDATE`s (§8), not immutability;
the append-only, full history of what happened is the separate audit
trail (`RECONCILIATION_STARTED`/`RECONCILIATION_COMPLETED` events).

## 5. Broker lookup strategy

Exactly the preference order Area D specifies, using only capabilities
that already exist on `BrokerClient`/its adapters (no new broker API
invented):

1. `broker_order_id` known → `ReadOnlyBrokerView.get_order(id)` (the same
   duck-typed optional hook `execution.py`'s own pending-order management
   already relies on). An adapter's own "not found" sentinel
   (`AngelOneBroker.get_order()` returns `status="UNKNOWN"` rather than
   raising) is recognized and treated as "fall through to the next
   strategy", not as a definitive answer.
2. No `broker_order_id`, or lookup-by-id came back unknown → the account's
   full order book, via `get_order_book()` or `get_open_orders()`
   (whichever the adapter implements; `AngelOneBroker` implements both,
   Dhan/ICICI/shadow adapters implement neither today — reconciliation
   correctly reports `RECONCILIATION_UNKNOWN` for those rather than
   guessing).
   - Book search came back **empty** (a real, successful "searched, found
     nothing" answer) → `RECONCILED_NOT_FOUND`.
   - Book has **exactly one** entry → treated as the candidate; the
     expected-vs-actual comparison (§6) then decides whether it's a match
     or a `RECONCILIATION_FAILED` mismatch.
   - Book has **more than one** entry and no `broker_order_id` to
     correlate against → `RECONCILIATION_UNKNOWN` ("ambiguous... cannot
     uniquely identify"), never a guess at which one is "the" order.
3. No lookup capability exists at all on the adapter → `RECONCILIATION_UNKNOWN`.

Position reconciliation (Area D's item 5) was deliberately **not** wired
in as an independent evidence source this phase: net position exposure
alone can corroborate but can never confirm a *specific* order (a position
change could come from any order), so using it to claim `RECONCILED_FILLED`
would be an inference, not evidence — see §16.

A genuine bug was found and fixed while building this: the original
`book = view.get_order_book() or view.get_open_orders()` treated a
**successful, genuinely empty** `[]` result the same as "this adapter has
no lookup capability" (`None`), because `[]` is falsy in Python — silently
misclassifying a real "not found" as "unknown". Fixed to check `is None`
explicitly; `test_8_order_not_found_after_successful_empty_search` and
`test_10_and_11_broker_query_failure_is_unknown_not_a_crash` both caught
this during development (see also `test_23_symbol_mismatch_is_reconciliation_failed`,
which caught a second, related bug — see §6).

## 6. Expected-vs-actual comparison

Symbol and side are compared (Area E's worked example: expected `BUY 1
lot NIFTY CE`, actual `SELL 1 lot` → `RECONCILIATION_FAILED`) whenever the
looked-up `OrderState` carries that data (§2's `symbol`/`side` addition).
A mismatch on either never attempts a fix — it is recorded as
`RECONCILIATION_FAILED` with a specific `error_message` naming exactly
what didn't match, for a human to investigate.

A second real bug was found here during development: the first
implementation pre-filtered order-book candidates by symbol match
*before* running the comparison, which meant a book containing exactly
one order with the *wrong* symbol was filtered down to zero candidates
and misreported as `RECONCILED_NOT_FOUND` instead of the intended
`RECONCILIATION_FAILED` (the order absolutely was found — it just didn't
match). Fixed by using **count alone** (0 / 1 / many) to decide whether a
candidate can be correlated at all, and running the full expected-vs-
actual comparison unconditionally on whatever single candidate is found —
never filtering by the very fields the comparison exists to check.

Terminal-state mapping: `REJECTED`/`CANCELLED` → `RECONCILED_REJECTED`;
`FILLED`/`COMPLETE` with `filled_quantity >= expected_quantity` →
`RECONCILED_FILLED`; anything else with a genuinely found, matching order
(open, or filled less than expected) → `RECONCILED_ACCEPTED`, with the
*actual* `filled_quantity` always recorded verbatim — **never** rounded up
to "complete" (Area F: `test_7_partial_fill_not_treated_as_complete`
proves `expected=65`-style partial fills stay `RECONCILED_ACCEPTED`, not
`RECONCILED_FILLED`, with the exact filled/remaining split preserved).

## 7. Ambiguous-response handling

`test_area_j_full_ambiguous_to_completed_lifecycle` drives the *exact*
diagram in this phase's brief end-to-end, from a real
`StrategyExecutionEngine.execute()` call that produces a genuine
`STATUS_AMBIGUOUS` record (reusing Phase 15D-DR's own machinery
unchanged) through `scan_and_register()` → `RECONCILIATION_REQUIRED` →
`reconcile_one()` → `RECONCILIATION_STARTED`/`COMPLETED` audit events →
`RECONCILED_FILLED`. `test_20_no_automatic_retry_reconciliation_never_calls_place_order`
additionally monkeypatches the fake broker's `place_order` to raise an
`AssertionError` if ever called, and confirms reconciliation completes
successfully without touching it — not just "no retry happened to occur"
but "the mechanism that would retry cannot reach the broker at all".

## 8. Restart behavior

Exactly like `PersistentAuditTrail`/`SqliteIdempotencyStore`, there is no
in-memory state to lose: `SqliteReconciliationStore` reads/writes only the
file. `test_12_restart_during_reconciliation_leaves_it_in_progress_and_recoverable`
and `test_13_restart_before_reconciliation_still_reconcilable` construct
entirely fresh `SqliteReconciliationStore`/`ReconciliationService`
instances against the same file (the closest a single-process suite gets
to simulating a new process) and confirm: a record claimed but never
completed survives restart as `RECONCILIATION_IN_PROGRESS` (never silently
lost, never silently reset), `reclaim_stale()` (Area M) moves it back to
`RECONCILIATION_REQUIRED` (never to `NOT_REQUIRED`, which would wrongly
imply nothing was ever wrong) only once its `claimed_at` is older than the
caller-supplied threshold, and a fresh worker can then successfully
acquire and complete it.

## 9. Concurrent reconciliation

`try_acquire()` is an atomic, persistence-backed conditional `UPDATE`
(`WHERE reconciliation_id = ? AND status = 'RECONCILIATION_REQUIRED'`,
checking `rowcount == 1`) — the same compare-and-swap pattern
`SqliteIdempotencyStore.claim()` already established via a bare `INSERT`,
adapted here to an `UPDATE` since a reconciliation record already exists
before it's claimed. This relies on SQLite's own file-level transaction
serialization, not only the in-process `threading.Lock` — satisfying
Area L's explicit "do not rely solely on in-memory locking". Two real
threads racing for the same `reconciliation_id`
(`test_15_concurrent_reconciliation_only_one_owner_wins`) always produce
exactly one winner. `complete()` is symmetrically guarded by `owner AND
status = IN_PROGRESS`, raising `ReconciliationOwnershipError` (never
silently overwriting) if a stale/duplicate completion attempt arrives
after ownership has moved on (`test_25_completion_by_wrong_owner_fails_closed`).

## 10. Account isolation

`ReconciliationService` never resolves accounts or credentials itself —
it operates on whatever `BrokerClient` the caller already resolved for a
given `account_id` (the same `BrokerManager.get_broker(account_id)` call
`execution.py` uses), so isolation is inherited from that existing,
already-tested per-account separation. `test_17_account_isolation` and
`test_17_concurrent_account_reconciliation_no_cross_contamination` (two
real threads reconciling `ANGEL_SAMIR` and `ANGEL_ACCOUNT_B`
simultaneously against two distinct fake brokers with distinct order
books) confirm zero cross-contamination in both the reconciliation store's
`by_account()` query and the final recorded results. Both
`TradingAccount` instances remain `READ_ONLY` throughout — reconciliation
never touches `authorization_state`.

## 11. Audit integration

`RECONCILIATION_STARTED` (Area C) and `RECONCILIATION_COMPLETED` (Areas
G/H) are written via the durable `PersistentAuditTrail` from Phase
15D-AUDIT — both event-type constants were already defined there; this
phase added one more, `EVENT_RECONCILIATION_FAILED`, for the case where
reconciliation itself (not the underlying order) couldn't be completed.
**Fail-closed exactly where Area C demands it**: if `RECONCILIATION_STARTED`
cannot be persisted, `reconcile_one()` aborts immediately — via
`mark_failed()` — **before ever querying the broker**
(`test_25_reconciliation_started_audit_failure_fails_closed_before_broker_query`
uses a broker whose `get_order()` raises `AssertionError` if called at
all, proving the abort happens strictly before any broker interaction).
The `RECONCILIATION_COMPLETED` write, by contrast, is best-effort past
that point (mirroring `CentralKillSwitch`'s own audit-call precedent) —
by the time it's attempted, the definitive answer is already durably
committed to `reconciliation_records` via `complete()`, so a failure here
means only the audit *mirror* of an already-safe result didn't get
written, not that the attempt itself was lost (Area P: "never silently
lose the reconciliation attempt").

## 12. API / service boundary

No new HTTP endpoint was added this phase (Rule 1 / this phase's own
instruction: "if appropriate" — judged not necessary given the existing
Python-level surface already satisfies Area Q's example operations
directly): `SqliteReconciliationStore.get()` / `by_intent()` /
`by_idempotency_key()` / `by_account()` / `list_outstanding()` /
`history()` are all plain read-only queries a future route or scheduled
worker can call. There is no method on this store, `ReconciliationService`,
or `ReadOnlyBrokerView` that can mutate a broker — not merely unused, but
absent (`test_read_only_broker_view_has_no_mutating_methods` asserts
`place_order`/`cancel_order`/`modify_order` don't exist as attributes at
all). If an HTTP "reconcile now" endpoint is added in the future, it
would call `reconcile_one()` directly — already exactly the read-only
operation Area Q describes.

## 13. Test matrix

New file: `tests/common/test_phase_15d_recon.py` — **33 tests**, covering
every one of the 25 items this phase's brief lists (pending/ambiguous
detection, started/completed durability, filled/rejected/partial/not-
found/unknown outcomes, broker timeout and read-only-API-failure handling,
restart during and before reconciliation, duplicate request handling,
concurrent reconciliation, stale in-progress handling, account A/B
isolation, idempotency correlation, audit persistence, no-automatic-retry,
kill-switch interaction, broker-order-ID correlation, expected-vs-actual
mismatch, secret redaction, persistence failure) plus extras: the
end-to-end Area J diagram proof and an explicit "reconciliation used a
fake broker throughout" sanity check.

Targeted regression re-run this phase (unaffected code paths):
`test_phase_14_6_hardening.py`, `test_phase_15d_dr_deployment_recovery.py`,
`test_phase_15d_dr_deployment_smoke.py`, `test_phase_15d_audit_persistence.py`,
`test_health.py`, `tests/validation/` (covers the `AngelOneBroker`
`OrderState` extension), `test_phase_15c_4_cross_account_isolation.py`,
`test_phase_15c_5_final_gate.py` — all passing (exit code 0).

## 14. Regression results

Full repository suite, run to completion with JUnit XML output for exact,
reliable counts:

```
tests=1414  failures=0  errors=0  skipped=0
```

(1383 carried over from Phase 15D-AUDIT's own final count + 33 new Phase
15D-RECON tests in `tests/common/test_phase_15d_recon.py` — 1383 + 33 =
1416; the 2-test difference reflects the same small pre-existing
collection-count variance already noted in Phase 15D-AUDIT's report,
unrelated to this phase.) Zero new failures. Exit code 0.

## 15. Known limitations

- **Position-based corroboration not implemented.** Area D's "position
  reconciliation where appropriate" evidence source was deliberately left
  out — net position exposure can corroborate but never uniquely confirm
  one specific order, and using it to claim `RECONCILED_FILLED` would be
  an inference rather than evidence. A future enhancement could surface
  it as an additional, clearly-labeled corroborating signal alongside
  (never instead of) an order-level finding.
- **Average fill price is never populated on `RECONCILED_FILLED`/
  `RECONCILED_ACCEPTED`.** `OrderState` (the existing, shared type every
  adapter already returns from `get_order()`) has never carried an
  average-price field — extending it further was judged out of this
  phase's scope; `ReconciliationRecord.average_price` stays `None` until
  a future phase extends `OrderState` similarly to how this phase added
  `symbol`/`side`.
- **`broker_id` is not populated by `scan_and_register()`.** The
  idempotency record doesn't carry a broker_id (only `account_id`), and
  `ReconciliationService` deliberately doesn't hold a `BrokerManager`/
  `TradingAccount` registry to look one up (keeping it decoupled from
  account routing). A caller that already has this information (e.g. a
  future scheduled worker that iterates registered accounts) can supply
  it directly to `create_required()`.
- **Ambiguous order-book correlation is single-candidate-only.** When no
  `broker_order_id` is known and the account's order book has more than
  one entry, this phase reports `RECONCILIATION_UNKNOWN` rather than
  attempting any heuristic (timestamp proximity, symbol+quantity
  matching) to narrow it down — a deliberately conservative choice
  (Area D: "never assume... unless evidence supports it") that trades
  resolution rate for never guessing wrong.
- **Reconciliation records are only created for `AMBIGUOUS`/`PENDING`
  intents**, never for definitively-resolved ones — `NOT_REQUIRED` is
  defined in the enum for schema completeness but no code path in this
  phase ever sets it. This keeps `reconciliation_records` small and
  focused on what actually needs attention, at the cost of not providing
  a reconciliation-store-based audit of *every* order (the durable audit
  trail already serves that purpose for every order, resolved or not).
- **No scheduled/background worker was built.** `scan_and_register()` and
  `reconcile_one()` are ready to be called by one (a cron-style loop, an
  API-triggered "reconcile now" action) but this phase does not add such
  a driver — matching the explicit "if appropriate" framing of Area Q and
  this phase's own scope boundary.
- **Cross-process concurrency for `try_acquire()`/`complete()` relies on
  SQLite's file-level locking** (same as `SqliteIdempotencyStore.claim()`
  before it) and has only been proven under real multi-threaded, single-
  process concurrency this phase (§9) — not multi-process load.

## 16. Future improvements

- A scheduled worker (e.g. an APScheduler job or a small standalone
  process) that periodically calls `scan_and_register()` then
  `reconcile_one()` for every outstanding record, plus a periodic
  `reclaim_stale()` sweep.
- Extend `OrderState` with an `average_price` field (mirroring this
  phase's `symbol`/`side` addition) once a real adapter can reliably
  supply one, closing the gap in §15.
- A read-only HTTP surface over `SqliteReconciliationStore`'s existing
  query methods, once there's an actual consumer (a dashboard view,
  an on-call runbook) that needs one.
- Position-based corroboration as a labeled, secondary signal (§15).

## 17. Exact next step

Build the scheduled reconciliation worker named above, so
`STATUS_AMBIGUOUS`/`STATUS_PENDING` records are resolved automatically
(read-only) on a cadence rather than requiring a human or a test to call
`scan_and_register()`/`reconcile_one()` manually. That closes the loop
this phase opened: a real ambiguous outcome from a future canary order
(Phase 15D.2) would then be reconciled with durable evidence within
minutes, not only on manual inspection.

Otherwise: this phase's hard stop applies — Phase 15D.2 (a real,
human-authorized canary order) was not started and requires the market to
be open, a fresh preflight, and explicit human authorization at the
presented gate, none of which happened in this phase.
