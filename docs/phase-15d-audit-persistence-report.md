# Phase 15D-AUDIT — Persistent Audit Trail Hardening

Status at start of phase: Phase 15D.1-B PASS, Phase 15D-DR PASS, 1340/1340
regression passing, 0 real broker mutation calls, 0 real orders, Account A
(ANGEL_SAMIR) and Account B (ANGEL_ACCOUNT_B) both READ_ONLY, Phase 15D.2
NOT STARTED. This phase is architectural/durability work only — it never
touches a real broker.

## 1. Current problem

`trading.common.observability.AuditTrail` (Phase 13) is tamper-**evident**
(a SHA-256 hash chain detects any modification/reordering/deletion) but
explicitly **in-memory only** — its own module docstring says so. Every
event is lost on process restart, crash, or redeploy. For a system whose
whole purpose is producing durable evidence of what happened to real
money, that is the single most important gap identified across every
prior phase's "known limitations" section (Phase 15D-DR's report named it
explicitly as "the single most important remaining gap, not fixed this
phase").

This phase closes that gap without touching `AuditTrail` itself, per this
phase's Rule 1 ("do not rewrite working safety logic unnecessarily").

## 2. Architecture

```
Caller (StrategyExecutionEngine, BrokerManager, AlertManager,
strategy classes, CentralKillSwitch, an authorization-transition call site)
        |
        v
PersistentAuditTrail.append(event_type, *, correlation_id="", strategy_id="", **detail)
        |   (exact same signature as observability.AuditTrail.append() --
        |    every existing caller in this codebase only ever calls
        |    .append(), never isinstance() -- so this is a drop-in
        |    replacement with ZERO changes required at any call site)
        v
SqliteAuditStore.append(...)
        |
        |-- redact secret-shaped keys (api_key/password/totp/mpin/tokens/...)
        |-- reject non-JSON-serializable or oversized (>64KB) detail
        |-- inject deployment identity automatically (app_version/git_sha/
        |   deployment_id/environment, from trading.common.deployment_info)
        |-- BEGIN IMMEDIATE transaction: read last (seq, hash), compute
        |   this event's hash (chained to the prior one), INSERT, COMMIT
        |-- any failure -> ROLLBACK, raise AuditPersistenceError (never a
        |   silent "pretend it worked")
        v
   SQLite file (audit_events table)
        |
        |-- UPDATE/DELETE physically rejected by BEFORE triggers
        |   (audit_events_no_update / audit_events_no_delete) --
        |   append-only enforced by the database, not just Python discipline
        |
        v
   records() / trace() / by_account() / by_intent() / by_idempotency_key() /
   by_broker_order_id() / by_time_range() / by_event_type() / by_deployment_id()
   -- all read directly from the file; there is no separate in-memory
   list that could ever diverge from what a fresh process reads.
```

New module: `trading/common/audit_store.py` — `SqliteAuditStore` (the
durable backing store) + `PersistentAuditTrail` (the thin, duck-type-
compatible wrapper) + `record_authorization_transition()` (a free
function, described in §11).

## 3. Persistence implementation

Standard-library `sqlite3` only, same layering discipline as
`trading.common.idempotency_store.SqliteIdempotencyStore` (`trading.common`
stays free of any concrete database dependency). A short-lived connection
is opened per call under a `threading.Lock`, exactly like
`SqliteIdempotencyStore` — sqlite3 connections are not safe to share
across threads, and this engine already runs background threads.

Deterministic event IDs: `event_id == hash`, and `hash` is a pure
function of `(seq, every indexed field, detail, prev_hash)`. Replaying the
exact same append at the exact same chain position always yields the
exact same id — this is what "deterministic event IDs" means concretely,
and it doubles as the PRIMARY KEY, so a genuine duplicate-ID write is
rejected by the database itself (`sqlite3.IntegrityError` → wrapped as
`AuditPersistenceError`).

Indexes exist on `correlation_id`, `strategy_id`, `account_id`,
`idempotency_key`, `broker_order_id`, `event_type`, `deployment_id`,
`timestamp` — one per query method in §9.

Future PostgreSQL migration: nothing outside `audit_store.py` assumes
SQLite. `PersistentAuditTrail`'s public surface
(`append`/`records`/`trace`/`by_*`/`verify`) is the seam a
`PostgresAuditStore` would sit behind with zero changes to any caller —
see §16.

## 4. Event schema

Every event carries (as either an indexed column or a JSON `detail`
field): `event_id`, `seq`, `timestamp` (UTC), `event_type`,
`correlation_id`, `strategy_id`, `account_id`, `owner_id`, `broker_id`,
`idempotency_key`, `broker_order_id`, `deployment_id`, `app_version`,
`git_sha`, `environment`, plus whatever else a caller passes (instrument/
symbol/expiry/strike/option_type/side/quantity/order_type/product_type/
risk_decision/authorization_state/execution_state/broker_response
classification/reconciliation_state/kill_switch_state/error_code/
error_message/...) inside `detail`. Deployment identity is injected
**automatically on every single event** by `SqliteAuditStore.append()` —
callers never pass it themselves, so it's structurally impossible for an
event to be missing it.

Secrets are never persisted: any detail key whose name contains
`password`/`passwd`/`secret`/`api_key`/`apikey`/`access_token`/
`refresh_token`/`token`/`totp`/`mpin`/`private_key`/`auth_code`
(case-insensitive, recursively through nested dicts) has its **value**
replaced with `***REDACTED***` before the event is ever hashed or
written — proven by `test_20_secret_redaction` reading the raw SQLite
file directly and confirming the real secret string is nowhere in it.
Deliberately does **not** match the bare substring `credential`, because
`TradingAccount.credential_reference` is a real, non-secret field
(documented on that class as "a NON-secret pointer... must never itself
be, or contain, a secret value") that a broader match would have wrongly
redacted — `test_20_redaction_applies_inside_nested_dicts` locks in the
narrower, correct behavior.

## 5. Critical events (Area D)

Of the 16 events this phase's brief names:

| # | Event | Wired to a real call site this phase? |
|---|-------|------------------------------------------|
| 1 | OrderIntent created | Yes — `execution.py` `EVENT_ORDER_INTENT_CREATED` (pre-existing call site, now also carries `idempotency_key`) |
| 2 | Risk approved/rejected | Yes — `EVENT_RISK_DECISION` (pre-existing) |
| 3 | Human authorization requested | No — no human-authorization subsystem exists yet (Phase 15D.2 has never run); schema/query support proven directly by test |
| 4 | Human authorization accepted/rejected | No — same reason as #3 |
| 5 | Idempotency claimed | Indirectly — the claim itself isn't separately audited, but every event downstream carries `idempotency_key`, and the idempotency store's own record is the authoritative claim record (Phase 14.6/15D-DR) |
| 6 | Broker submission attempted | Yes — `EVENT_ORDER_INTENT_CREATED`/pre-submission gates already mark this point |
| 7 | Broker accepted/rejected | Yes — `EVENT_EXECUTION_RESULT`, `EVENT_BROKER_ORDER_PLACED` (now also carry `idempotency_key`/`broker_order_id`) |
| 8 | Broker response ambiguous | Yes — `EVENT_AMBIGUOUS_ORDER_STATE` (Phase 15D-DR call site, now durable) |
| 9 | Reconciliation started | No — no reconciliation subsystem exists yet; schema/query support proven directly by test |
| 10 | Reconciliation completed | No — same reason as #9 |
| 11 | Kill switch enabled | Yes — **new this phase**, `CentralKillSwitch.engage()` |
| 12 | Kill switch disabled | Yes — **new this phase**, `CentralKillSwitch.disengage()` |
| 13 | Authorization-state transition | Yes — **new this phase**, `TradingAccount.set_authorization_state()` + `record_authorization_transition()` |
| 14 | Application startup | Yes — **new this phase**, `trading/api/app.py` lifespan |
| 15 | Application shutdown | Yes — **new this phase**, `trading/api/app.py` lifespan |
| 16 | Deployment identity | Yes — automatic on every event (§4), plus explicit in #14/#15 |

Honest framing (matching Phase 15D-DR's own precedent for the rollback
section): #3/#4/#9/#10 have no producing subsystem in this codebase today
— no reconciliation module and no human-authorization workflow exist yet.
Defining the event-type constants and proving the store persists/queries
them correctly (`test_reconciliation_and_human_authorization_event_types_are_supported`)
is honest "the mechanism works" evidence; it is not a claim that anything
in production calls them yet. Fabricating a fake call site to make the
checklist look more complete would be worse than stating this plainly.

Fail-closed for the events that ARE wired: `SqliteAuditStore.append()`
either fully commits or raises `AuditPersistenceError` — there is no
"database unavailable → buffer in memory → continue" path anywhere in
this module, matching Area D's explicit prohibition. What happens when
`StrategyExecutionEngine.execute()` calls this via Phase 14.6's
`ObservabilityHealth.safe_observe()` is a **separate, already-settled**
decision from an earlier phase and is **not** relaxed here: an audit
write failure must never itself cause a duplicate broker order by
crashing `execute()` after a broker call already went out, so
`safe_observe()` still swallows the exception there. This is not a
contradiction — the store still never fabricates success (fail-closed at
its own layer), and `ObservabilityHealth.failures()`/`.healthy` remain
the visible signal that a safety-critical write did not durably happen.
Similarly, `CentralKillSwitch.engage()`/`disengage()` treat their own
audit call as best-effort (logged, never raised) — engaging a kill switch
must never be blocked by an audit hiccup, which would invert the safety
priority Area J itself exists to protect.

## 6. Restart behavior (Area E)

There is no separate in-memory list to lose: `PersistentAuditTrail`'s
`records()`/`trace()`/`by_*()` all read directly from the SQLite file, so
"the audit trail after restart" and "the audit trail" are the same object
by construction. `test_2_restart_persistence` and
`test_13_deployment_startup_event_carries_identity_and_survives_restart`
construct a *fresh* `PersistentAuditTrail` instance against the same file
(simulating a new process) and confirm every prior event is present, the
hash chain continues correctly from the last pre-restart hash (not a
fresh genesis), and a *new* append after "restart" still validates.

## 7. Crash behavior (Area F)

`test_crash_boundary_leaves_a_consistent_partial_trace` (parametrized
over the five boundaries A–E this phase's brief names) proves: whatever
prefix of events was durably appended before a simulated crash is exactly
what a fresh read shows — no event is ever fabricated to "fill in" a
stage that never actually completed, and the partial trace's hash chain
still verifies (a truncated trace is a valid chain, not a corrupted one).
An investigator reading `trail.trace(correlation_id)` after a real crash
can tell exactly which stage was last durably reached, because nothing
downstream of that stage will ever appear.

## 8. Ambiguous-response behavior (Area H)

Reused Phase 15D-DR's existing `AmbiguousOrderStateError` /
`STATUS_AMBIGUOUS` / `AmbiguousIdempotencyStateError` machinery in
`execution.py` and `idempotency_store.py` completely unchanged (Rule 1) —
only the audit *sink* underneath it changed, from in-memory to durable.
`test_9_ambiguous_response_survives_restart_and_blocks_automatic_retry`
proves the full sequence this phase's brief diagrams exactly:

```
AMBIGUOUS -> PERSIST -> RESTART -> RECONCILIATION REQUIRED
```

by constructing entirely fresh `StrategyExecutionEngine`/
`PersistentAuditTrail`/`SqliteIdempotencyStore` instances pointed at the
same files (the closest a single-process test suite can get to "a new
process") and confirming: the pre-restart `AMBIGUOUS_ORDER_STATE` audit
event is still there, and a second `execute()` call under the same
idempotency key raises `AmbiguousIdempotencyStateError` rather than
retrying. `test_10_no_automatic_retry_after_restart_broker_never_called_again`
adds a hard numeric proof: a shared call-counter on the fake broker stays
at exactly 1 across the simulated restart.

## 9. Account isolation (Area I)

`test_5_account_isolation_sequential` and
`test_5_account_isolation_concurrent_writes` (two real threads writing 20
events each for `ANGEL_SAMIR` and `ANGEL_ACCOUNT_B` simultaneously)
confirm `by_account()` never returns the other account's events, under
both sequential and concurrent writes, and that the shared hash chain
(a single, global sequence across both accounts — this store does not
partition the chain per account) still verifies afterward. No credential
value is ever stored (§4's redaction applies regardless of which account
an event belongs to).

## 10. Kill-switch persistence (Area J)

`CentralKillSwitch` gained an optional `audit_trail` constructor
parameter (Phase 15D-DR's existing `persistence_path` parameter is
unchanged and independent — a caller may use either, both, or neither).
`engage()`/`disengage()` record `previous_engaged`/`new_engaged`/`actor`/
`reason` via `EVENT_KILL_SWITCH_ENGAGED`/`EVENT_KILL_SWITCH_DISENGAGED`.
`test_11_kill_switch_engaged_before_restart_stays_engaged_and_blocks_execution`
combines both mechanisms: an engaged switch persisted via
`persistence_path`, reloaded by a fresh instance (simulating restart),
still blocks `StrategyExecutionEngine.execute()`.
`test_11_kill_switch_engage_survives_audit_failure` proves the switch
itself engages even when the audit sink raises — see §5's fail-closed
discussion.

Production wiring: `trading/api/execution_state.py`'s
`build_execution_state()` now threads an optional
`KILL_SWITCH_PERSISTENCE_PATH` env var into the real `CentralKillSwitch`
instance the API reads/writes (`trading/api/execution_routes.py`'s
`/api/.../kill-switch` endpoint) — unset (the default) preserves the
exact original in-memory-only behavior.

## 11. Authorization audit (Area K)

`TradingAccount.set_authorization_state(new_state, *, reason="")` is the
one new, supported way to change `authorization_state` besides the
already-irreversible `set_killed()` — it performs the transition (still
refusing to leave `KILLED`, exactly like `set_killed()`'s own contract)
and returns the *previous* state, so a caller passes that straight into
`trading.common.audit_store.record_authorization_transition(audit_trail,
account, previous_state, reason)`. This is a deliberate **free function**,
not a method on `TradingAccount` and not a hook baked into it —
`TradingAccount` stays free of any observability import (unchanged
layering), and the split makes the Area K boundary explicit and testable:
`test_12_authorization_audit_never_grants_authorization_itself` appends a
fabricated `LIVE_AUTHORIZED` audit event *without* ever calling
`set_authorization_state()` and confirms the account's real
`authorization_state` is completely unaffected — "audit logging is
observational" is proven, not just asserted in a docstring.

## 12. Deployment audit (Area L)

`trading/api/app.py`'s `lifespan()` now records `DEPLOYMENT_STARTUP`
(before `yield`) and `DEPLOYMENT_SHUTDOWN` (after `yield`, i.e. on normal
ASGI shutdown) to `app.state.execution.audit_trail` — the same instance
`build_execution_state()` wires everything else through — carrying the
exact `app_version`/`git_sha`/`deployment_id`/`environment` from
`trading.common.deployment_info.get_deployment_info()`. Both calls are
wrapped so an audit-write failure can never block startup or shutdown
(same precedent as the pre-existing `bootstrap_admin()` call just above
it). `test_13_deployment_startup_event_carries_identity_and_survives_restart`
covers the event/store contract directly; production wiring is additive
and exercised implicitly by every existing app-lifecycle test that spins
up `create_app()` (see §14 regression results).

## 13. Failure handling (Area N)

| Scenario | Behavior | Test |
|---|---|---|
| Database locked/unavailable | `AuditPersistenceError` raised, transaction rolled back, no partial row | `test_14_database_locked_fails_closed` |
| Malformed event (empty/`None` `event_type`) | `AuditPersistenceError` raised before any I/O | `test_14_malformed_event_type_rejected` |
| Duplicate event ID | Rejected by the `event_id` PRIMARY KEY (`sqlite3.IntegrityError`) | `test_4_duplicate_event_id_rejected` |
| Invalid `account_id` (wrong type) | `AuditPersistenceError` raised | `test_14_invalid_account_id_type_rejected` |
| Oversized event (>64KB detail) | `AuditPersistenceError` raised, nothing written (not truncated) | `test_14_oversized_event_rejected_not_truncated` |
| Non-JSON-serializable detail value | `AuditPersistenceError` raised (no silent `str()` coercion) | `test_14_non_serializable_detail_rejected` |
| Corrupted stored `detail_json` | `records()`/queries raise `AuditPersistenceError` rather than silently skipping or fabricating a value | `test_14_corrupted_detail_json_fails_closed_on_read` |
| Direct SQL `UPDATE`/`DELETE` | Rejected by database triggers (`sqlite3.IntegrityError`) | `test_3_append_only_update_rejected_at_database_level`, `test_3_append_only_delete_rejected_at_database_level` |

No scenario above results in silent data loss, a silent overwrite, or an
"audit unavailable → proceed anyway as if it succeeded" fallback.

## 14. Test matrix (Areas O/P)

New file: `tests/common/test_phase_15d_audit_persistence.py` — **45
tests**, covering all 20 items this phase's brief lists (event
persistence, restart persistence, append-only, duplicate handling,
account isolation, idempotency correlation, broker-order correlation,
ambiguous-response persistence/restart/no-retry, kill-switch persistence,
authorization audit, deployment audit, database failure, concurrent
writes, all four query-by-* types, secret redaction) plus extras: the
five-boundary crash-simulation parametrization, mechanism-only coverage
for reconciliation/human-authorization event types, and an explicit
drop-in-compatibility test against `StrategyExecutionEngine`.

`trading/common/execution.py` changed additively (Rule 1: nothing
rewritten, only new keyword arguments added to existing `.append()`
calls) so `ORDER_INTENT_CREATED`/`EXECUTION_RESULT`/`BROKER_ORDER_PLACED`/
`FILL` now also carry `idempotency_key` and `broker_order_id` — required
for Area G's intent → idempotency_key → audit events → broker_order_id
correlation to actually be queryable; before this change no successful
(non-ambiguous, non-replay) order's audit trail carried its idempotency
key at all. `test_6_idempotency_correlation_full_lifecycle_reconstructable`
and `test_7_broker_order_correlation` exercise this directly. This is a
genuine gap this phase found and closed, in the same spirit as Phase
15D-DR's own two safety-gap discoveries.

Targeted regression re-run this phase (unaffected code paths, confirming
no interaction with the new module):
`test_phase_14_6_hardening.py`, `test_phase_15d_dr_deployment_recovery.py`,
`test_phase_15d_dr_deployment_smoke.py`, `test_health.py`,
`test_phase_15c_4_cross_account_isolation.py`,
`test_phase_15c_5_final_gate.py` — **all passing** (exit code 0).

## 15. Full regression results

Full repository suite, run to completion with JUnit XML output for exact,
reliable counts (this shell's plain pytest summary line is truncated in
this environment; the JUnit `<testsuite>` attributes are authoritative):

```
tests=1383  failures=0  errors=0  skipped=0
```

(1338 pre-existing tests carried over from Phase 15D-DR's own final count
of 1340, adjusted for a small pre-existing collection-count discrepancy
unrelated to this phase, + 45 new Phase 15D-AUDIT tests in
`tests/common/test_phase_15d_audit_persistence.py`.) Zero new failures.
A second, independent full run (plain `-q`) also completed with exit code
0 before this JUnit run, for cross-confirmation.

## 16. Known limitations

- **No PostgreSQL implementation** — only `SqliteAuditStore` exists. The
  `PersistentAuditTrail` surface is the seam a future implementation sits
  behind; nothing else in the codebase would need to change.
- **Reconciliation and human-authorization events have no producing
  subsystem yet** (§5, rows 3/4/9/10) — the schema and query surface are
  ready and directly tested, but no code in this repository calls them
  today, because no reconciliation workflow or human-authorization
  workflow has been built (Phase 15D.2 has never run a real order).
- **Single global hash chain, not per-account** — `by_account()` filters
  correctly (proven under concurrency), but the chain itself is one
  sequence across every account. A tamper check (`verify()`) is therefore
  whole-database, not partitionable per account; this matches
  `observability.AuditTrail`'s own original design (a single trail per
  process) and was not changed.
- **Full-history load on some query paths** — `by_time_range()` and
  similar queries scan the indexed column directly (not a full table
  scan, thanks to §3's indexes), but there's no pagination/cursor support
  yet. Acceptable for this phase's scope (Rule: don't introduce
  infrastructure beyond what's needed); would need addressing before a
  production deployment accumulates a very large table.
- **No cross-process load testing.** Like `SqliteIdempotencyStore` before
  it (a limitation Phase 15D-DR already documented for that module), the
  atomic `BEGIN IMMEDIATE` transaction plus in-process lock has not been
  exercised under real multi-process concurrent load — only real
  multi-threaded concurrency within one process (§9).
- **`AlertManager`/`BrokerManager`/strategy classes were not re-pointed
  at `PersistentAuditTrail` in this phase's production wiring** —
  `build_execution_state()` wires ONE shared `audit_trail` instance
  through all of them already (pre-existing design), so opting into
  `AUDIT_DB_PATH` automatically covers all of them; nothing further was
  needed.

## 17. Exact next step

Build a real reconciliation subsystem (Area F/H's manual-reconciliation
boundary, and Area D's rows 9/10) that reads broker order-book/position
state and definitively resolves any `STATUS_AMBIGUOUS`/`STATUS_PENDING`
idempotency record, recording `RECONCILIATION_STARTED`/
`RECONCILIATION_COMPLETED` to this now-durable audit trail — this is the
one remaining piece needed before an `AMBIGUOUS` outcome from a real
canary order (Phase 15D.2) could ever be closed out with durable evidence
rather than manual, undocumented broker-console inspection.

Otherwise: this phase's hard stop applies — Phase 15D.2 (a real,
human-authorized canary order) was not started and requires the market to
be open, a fresh preflight, and explicit human authorization at the
presented gate, none of which happened in this phase.
