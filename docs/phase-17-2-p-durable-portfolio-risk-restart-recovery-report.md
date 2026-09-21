# Phase 17.2-P — Durable Portfolio Risk Persistence & Restart Recovery

**PHASE 17.2-P = PASS**

Phase 17.2 operator-authenticated acceptance: **STILL BLOCKED-PENDING-AUTHORIZED-OPERATOR** (unchanged — see below).
Phase 17.3 authorized: **NO**.

---

## 1. Original safety gap

`PortfolioRiskManager` (`trading/common/portfolio_risk.py`) held all state in pure
Python dicts: outstanding reservations, per-(strategy,account,symbol) ledger
positions, and per-day order counts. A TCC crash or restart discarded all of
it. On restart the manager would report zero reserved exposure, zero open
orders, and zero orders-per-day for positions/orders that might still be
genuinely outstanding at the broker — silently understating risk and
potentially permitting a new order that should have been rejected.

## 2. Existing behavior traced before any change

Full line-by-line trace of the 707-line pre-existing implementation:
`evaluate_and_reserve()` → `_evaluate_and_reserve_locked()` (under `self._lock`,
an `RLock`) computes a snapshot, checks limits, and on success creates a
`_Outstanding` keyed by a fresh `uuid.uuid4()` reservation id in
`self._reservations`. `commit_reservation()`/`release_reservation()` pop from
`_reservations` only. Non-terminal or `AMBIGUOUS` outcomes are filed into a
separate `_open_orders` dict (Phase 17.1's ambiguous-holding fix), resolved
only by the operator-facing `resolve_open_order()` or the Phase 17.1-R
reconciliation-feedback methods `find_reservation_by_idempotency_key()` /
`resolve_reconciliation()`. This second dict, and the strict separation of
which public methods may touch it, turned out to be the load-bearing safety
invariant of the whole class (see §11).

## 3. Authoritative vs. derived state

| State | Authoritative / Derived | Persisted |
|---|---|---|
| Reservation identity, idempotency key, strategy/account/symbol/side/qty/price/notional, order-count-day, status, fill price | Authoritative | Yes |
| Ledger position (qty, avg price, realized PnL) per strategy+account+symbol | Authoritative | Yes |
| Per-day order count per scope (strategy/account/portfolio) | Authoritative | Yes |
| Strategy/account/portfolio aggregate exposure, aggregate PnL, open-order counts | Derived | No — recomputed on every read from persisted rows, mirroring `_build_snapshot()` |
| Risk limits (max exposure, max daily loss, max orders/day, etc.) | Configuration, not state | No — a separate, pre-existing source of truth; never duplicated into this store |

## 4. Persistence schema (SQLite, `PORTFOLIO_RISK_DB_PATH`)

New module `trading/common/portfolio_risk_store.py`, `SqlitePortfolioRiskStore`.
Same per-call-connection + `threading.Lock` + `harden_file_permissions()`
discipline as the existing `IdempotencyStore`/`ReconciliationStore`/
`LiveAuthorizationStore`. Tables: `portfolio_reservations` (PK
`reservation_id`), `portfolio_ledger` (PK `strategy_id,account_id,symbol`),
`portfolio_order_counts` (PK `scope_type,scope_id,trading_date`),
`schema_meta` (schema version). No Redis, no Postgres, no Kafka.

## 5. Reservation status vocabulary

`RESERVED`, `OPEN`, `AMBIGUOUS`, `COMMITTED`, `RELEASED`. Deliberately did
**not** add separate `EXECUTION_PENDING`/`REQUIRES_RECONCILIATION`/
`REQUIRES_REVIEW` states: `AMBIGUOUS` already means exactly "held,
unresolved, needs reconciliation" in this codebase's existing vocabulary
(matches `execution.py`'s own `STATUS_AMBIGUOUS`), consistent with the
brief's own permission to reuse existing semantics.

## 6. Transaction / durability model

- **Reserve**: durable-before-execution. `store.create_reservation()` +
  3× `store.adjust_order_count()` happen *before* any in-memory dict is
  touched. If the store call raises, `evaluate_and_reserve()`'s existing
  fail-closed `try/except` catches it and returns `REASON_INVALID_RISK_STATE`
  — no in-memory reservation is ever created without a durable record behind
  it. Verified by `test_evaluate_and_reserve_is_fail_closed_when_store_write_fails`.
- **Commit / release / resolve**: best-effort durable write
  (`_persist_transition()`/`_persist_ledger()`, wrapped in
  `try/except Exception: pass`). By this point the broker call has already
  happened (or definitively hasn't); failing the in-memory transition
  wouldn't undo it. If the durable write itself fails, the discrepancy is
  caught and repaired by the next-restart reconciliation-convergence pass —
  never silently lost, only deferred.
- **Order counts**: `INSERT ... ON CONFLICT DO UPDATE SET count = MAX(0, count + ?)`
  — atomic, never negative.
- **Reservation transitions**: atomic CAS (`UPDATE ... WHERE status IN (...)`,
  checks `rowcount == 1`), idempotent by construction, mirroring
  `ReconciliationStore`'s own pattern.

## 7. Concurrency model

Unchanged from the existing design: all in-memory mutation stays under
`PortfolioRiskManager`'s existing `RLock`. The store itself serializes
writes under its own lock and uses SQLite's own atomicity for the CAS and
`ON CONFLICT` operations. Verified with 20-thread concurrent order-count
increments (no lost updates) and 20-thread concurrent reservation creates
(all succeed with distinct ids).

## 8. Startup / recovery algorithm (fail-closed)

`open_or_diagnose(db_path)`:
- Neither the DB file nor a companion `<path>.initialized` marker exists →
  legitimate first run → create both → `READY`.
- Marker exists but the DB file doesn't → unexpected data loss →
  `NOT_READY`, **never** silently recreates an empty store.
- DB file exists but fails `PRAGMA integrity_check` or its recorded
  `schema_meta.schema_version` doesn't match the code's `_SCHEMA_VERSION` →
  `NOT_READY`.
- DB file exists, passes integrity check, schema matches → `READY`, loads
  normally.

On construction with a `READY` store, `PortfolioRiskManager._recover()`
loads all non-terminal reservations, ledger positions, and order counts into
the same in-memory dicts every other method reads.

## 9. `PortfolioRiskReadiness` model

`READY` / `RECOVERY_REQUIRED` / `NOT_READY`, deliberately kept distinct from
process health — a backend that is `NOT_READY` on the risk store still
answers `/api/health` normally (the process itself is fine) while correctly
refusing every new reservation. `evaluate_and_reserve()` checks
`self._readiness != READY` **first**, before any limit evaluation, and fails
closed with `REASON_RISK_NOT_READY`. This propagates through
`WorkerCoordinator._reserve_portfolio_risk()` (which already treats any
`decision.allowed=False` as a rejection) with **no code change needed
there** — verified end-to-end by
`test_worker_coordinator_rejects_when_portfolio_risk_not_ready`
(`result.execution_result is None`, zero broker calls attempted).

## 10. Reconciliation integration (existing `ReconciliationService` reused — no second subsystem)

`_reconcile_with_idempotency()` runs once at recovery (and is also exposed
publicly as `reconcile_with_idempotency()` for on-demand re-triggering): for
every recovered non-terminal reservation carrying an `idempotency_key`, it
looks up that key in the authoritative `IdempotencyStore`:

- `STATUS_COMPLETED` → converge to `COMMITTED` (applies the fill to the
  ledger, exactly as a normal commit would).
- `STATUS_REJECTED` / `STATUS_FAILED` → converge to `RELEASED` (rolls back
  the order count).
- `STATUS_AMBIGUOUS` / `STATUS_PENDING` / no record at all → left untouched
  — still genuinely in-flight or still genuinely ambiguous, **never
  guessed**.

This is a deterministic, idempotent, re-runnable convergence pass, not a
fake distributed transaction across the three separate SQLite files. If the
process crashes mid-convergence, the next restart's convergence pass simply
re-derives the same correct outcome from the two authoritative sources
(`IdempotencyStore` + `PortfolioRiskStore`).

## 11. Critical regression found and fixed (see also §14)

The internal convergence path legitimately needs to resolve an
`AMBIGUOUS`-held entry sitting in `_open_orders`. My first implementation
let the *public* `commit_reservation()`/`release_reservation()` reach into
`_open_orders` too — which broke the pre-existing safety invariant (and its
guarding test, `test_release_reservation_is_a_noop_for_already_committed_ambiguous_id`):
a stray/duplicate call to either public method must never be able to
silently discard genuinely held exposure. Fixed by keeping
`commit_reservation()`/`release_reservation()` strictly `_reservations`-only,
and adding two separate internal-only methods,
`_converge_to_committed()`/`_converge_to_released()`, used exclusively by
the reconciliation-convergence pass, which alone may resolve an
`AMBIGUOUS`-held entry — and only once the `IdempotencyStore` has revealed
its terminal outcome.

## 12. Crash matrix (representative cells; full matrix in test suite)

| Cell | Scenario | Verified restart behavior |
|---|---|---|
| A | Crash before durable reserve write | No in-memory or durable reservation exists — as if never attempted |
| C/D | Crash after durable reserve, before broker call / no idempotency record yet | Reservation recovered as `RESERVED`, stays untouched (no guess) |
| F | Crash while `AMBIGUOUS`, `IdempotencyStore` later shows `COMPLETED` | Converges to `COMMITTED`, ledger updated, exactly 1 broker call ever recorded |
| G | Crash while `AMBIGUOUS`, `IdempotencyStore` later shows `REJECTED` | Converges to `RELEASED`, order count rolled back |
| J | Crash while `AMBIGUOUS`, `IdempotencyStore` still `PENDING`/`AMBIGUOUS` | Stays held, untouched, across repeated restarts |
| — | Missing expected DB (marker present, file gone) | `NOT_READY`, new reservations fail-closed |
| — | Corrupt DB / schema-version mismatch | `NOT_READY`, never silently recreated empty |
| — | Durable write failure on reserve | Fail-closed, no in-memory reservation created |

## 13. Multi-strategy / multi-account / same-symbol correctness

Verified across restart: two strategies on the same account (isolated order
counts and exposure), two accounts holding the same symbol (isolated ledger
rows), and two strategies trading the same symbol under the same account
(additive ledger position). All persisted and correctly reconstructed after
`del manager; reopen store; new manager`.

## 14. Test results

- `tests/common/test_portfolio_risk_store.py` — 19/19 passed (schema, fail-closed diagnosis, CRUD, ledger, order counts, concurrency).
- `tests/common/test_portfolio_risk_restart.py` — 21/21 passed (restart durability, readiness gating, crash-matrix convergence, idempotent commit/release, daily-counter persistence + trading-day rollover, multi-strategy/account/symbol, write-failure fail-closed, `WorkerCoordinator` end-to-end rejection).
- `tests/common/test_phase_17_2_p_portfolio_risk_e2e.py` — 5/5 passed (FakeBroker + real `StrategyExecutionEngine` + `ReconciliationService`, including a full reserve → AMBIGUOUS → restart → reconciliation FOUND → restart-again sequence, exactly 1 broker call across all three process lifetimes).
- Full existing suite (`test_portfolio_risk.py`, `test_phase_16_10_worker_portfolio_risk.py`, `test_phase_17_1_live_readiness.py`, `test_phase_17_1_r_reconciliation_feedback.py`, `test_risk_manager*.py`, `test_idempotency_store.py`, `test_execution*.py`, `test_worker_coordinator.py`, `test_kill_switch.py`, `test_phase_15d_5/6_live_authorization*.py`, `test_execution_routes.py`, `test_live_authorization_routes.py`) — all passed, zero regressions.
- Full backend regression (`python -m pytest -q`, whole suite) — exit code 0, zero `F` markers across 100% of the run. (The one non-failure warning present in the log — `PytestUnhandledThreadExceptionWarning` from a pre-existing AngelOne background-polling test double — is unrelated to this change and does not fail the suite.)
- Frontend/TypeScript/build: **not run** — no frontend files were touched in this phase.

## 15. Trading-day handling

Daily order counters use the manager's existing injectable clock
(`mgr._clock`), keyed by `order_count_day` (a `date`, ISO-8601 in storage),
never host-local implicit timezone. Verified: counters persist correctly
across a restart within the same trading day, and correctly start fresh
(rather than accumulating) when the clock crosses into a new trading day.

## 16. Worker independence

The worker EC2 (`i-03356005ada01fd8d`) does **not** hold a `PortfolioRiskStore`
or any risk database — `PortfolioRiskManager` and its store live exclusively
in the TCC process. No change was needed or made to the three worker
systemd services' code.

## 17. Production deployment

- **Pre-deployment check** (all read-only): strategies `DoubleStraddelAlgo`/
  `CombinedVwapNifty`/`Vwap_Algo_Nifty_hedge` all `STOPPED`/`INACTIVE`;
  3 workers `ONLINE` at the then-current `git_sha`; all 3 accounts
  `READ_ONLY`/`SHADOW`; `trading_authorized=false`; `live_trading_disabled=true`;
  kill switch disengaged; `usable LiveAuthorizations = 0` (queried directly,
  read-only, from `live_authorization.db` inside the running container);
  Account B (`ANGEL_ACCOUNT_B`) confirmed **FLAT** via a real read-only
  AngelOne broker session (`get_positions()`, `ANGEL_READ_ONLY=1`).
- **Backup**: `/var/backups/centralized-algo/20260920T181353Z/` — full
  `safety-data` directory, `backend.env`, pre-deployment deployed-SHA record,
  `docker compose ps` snapshot.
- **Deploy**: `git pull --ff-only` (fd061ca → 45f00ce → 147b8b3, see §18),
  `PORTFOLIO_RISK_DB_PATH=/app/data/portfolio_risk.db` added to
  `/etc/centralized-algo/backend.env` (chmod 600, root-owned), rebuilt via
  `docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml up -d --build`.
- **First-initialization verification**: `portfolio_risk.db` created inside
  the existing `/var/lib/centralized-algo/safety-data` bind mount,
  `schema_version=1` recorded, `.initialized` marker present, 0 reservations
  — recognized as a legitimate first run.
- **Controlled production restart test**: `docker compose ... restart backend`
  with strategies STOPPED throughout. Post-restart: `/api/health` OK,
  `/api/ready` → `trading_authorized: false`, all 3 strategies still
  `STOPPED` (no auto-resume), `portfolio_risk_readiness: READY`,
  `portfolio_risk_outstanding_reservations: 0`, kill switch disengaged,
  zero broker mutations.

## 18. Deployment self-correction (documented in full for transparency)

My first deployment command used `docker compose -f docker-compose.yml up -d --build`
(only the base repo-root file), omitting the production overlay
`trading/infrastructure/backend/docker-compose.prod.yml`. Effects, all
diagnosed and corrected within minutes, before any traffic reached the
misconfigured state:
- The backend container immediately crash-looped and exited (3) —
  `ProductionSafetyError: KILL_SWITCH_PERSISTENCE_PATH/AUDIT_DB_PATH not set`
  — because the overlay's `env_file:` directive (pointing at
  `/etc/centralized-algo/backend.env`) never took effect. The backend never
  reached a state where it could accept a request.
- Postgres was recreated onto a fresh, empty, docker-managed volume
  (`app_pgdata`) instead of its real bind mount
  (`/var/lib/centralized-algo/pgdata`), because the overlay's bind-mount
  override was also skipped. The original data on disk was **never touched
  or deleted** — confirmed by inspecting `/var/lib/centralized-algo/pgdata`
  (unchanged, dated Aug 29) after the fact.
- Corrected by redeploying with both `-f` flags. Verified via
  `docker inspect` that both `app-postgres-1` (bind mount →
  `/var/lib/centralized-algo/pgdata`) and `app-backend-1` (bind mount →
  `/var/lib/centralized-algo/safety-data`) were restored to their correct
  mounts. Removed the now-orphaned, never-used `app_pgdata` volume (created
  by my own error, held zero real writes since the backend never completed
  a successful startup against it).
- Net effect: zero real data loss, ~3 minutes of the health endpoint
  returning 502, zero broker/order/execution activity during the window
  (confirmed via the audit trail — every event in the window traces to
  container restarts, worker re-registration, and version-mismatch
  alerts, nothing else).

Separately: I found and fixed a gap the deployment surfaced —
`operations_snapshot.py`'s `SafetyView` dataclass gained
`portfolio_risk_readiness`/`portfolio_risk_outstanding_reservations`, but
the HTTP-facing `SafetyOut` pydantic model in `execution_routes.py` is a
*separate* schema and silently dropped both fields from the
`GET /api/operations/summary` response. Fixed (commit `147b8b3`), verified
via the running production API returning
`"portfolio_risk_readiness":"READY","portfolio_risk_outstanding_reservations":0`.

Also found, **not fixed** (explicitly out of scope for this persistence-only
phase — belongs to the pre-existing `WorkerCoordinator`/`WorkerRegistry`
subsystem from Phase 17.2, not to `PortfolioRiskManager`): the worker
client's `_post()` helper (`trading/worker/client.py`) only special-cases
HTTP 401 and 5xx; a 404 ("unknown worker" — the exact response a fresh
backend process gives for a session it has no memory of) or 409 (duplicate
session, hit briefly during my own restart-driven re-registration race) is
parsed as if it were a successful JSON body, which either silently drops
the heartbeat failure (404 case — worker never notices it needs to
re-register) or raises an unhandled `KeyError` on the missing `session_id`
key (409 case — the systemd unit's own restart policy recovered it in a
few seconds). This is a real, honest finding worth a future ticket; it did
not cause any unsafe state during this phase (strategies were STOPPED
throughout, and it self-healed via `systemctl restart`), and I did not
modify `trading/worker/client.py` or `runner.py` to fix it, consistent with
"persistence-only phase, do not disturb worker infrastructure beyond what
this remediation requires."

## 19. Post-deployment verification

- `/api/health` and `/api/ready`: OK, `trading_authorized: false`.
- All 3 strategies: `STOPPED`/`INACTIVE`.
- All 3 workers: `ONLINE`, `version_mismatch: false`, correctly re-registered
  after the TCC restarts.
- `portfolio_risk_readiness: READY`, `portfolio_risk_outstanding_reservations: 0`.
- Account B re-checked **FLAT** via real read-only broker session, after
  deployment completed.
- Audit trail for the entire deployment window reviewed in full: every
  event (`DEPLOYMENT_STARTUP`/`SHUTDOWN` ×5, `WORKER_REGISTERED`/`ONLINE`/
  `OFFLINE`/`HEARTBEAT_LOST`, `ALERT_RAISED`/`RESOLVED_WORKER_VERSION_MISMATCH`,
  `WORKER_SESSION_REPLACEMENT_REJECTED`) traces directly to the deployment
  and restart actions performed. Zero order, execution, or
  LiveAuthorization events of any kind.
- **Real broker mutation count throughout Phase 17.2-P: 0.** Every broker
  interaction was either a `FakeBroker`/`ShadowBroker`/test double in the
  test suite, or a real, explicitly read-only `get_positions()` call
  against Account B (twice: pre- and post-deployment).

## 20. Remaining limitations

- `PortfolioRiskManager`'s own risk *limits* remain configuration, sourced
  exactly as before this phase — this remediation persists *state* only,
  per the brief's own explicit instruction not to duplicate limits into the
  new store.
- The worker-client heartbeat robustness gap noted in §18 remains
  unaddressed (out of scope).
- Phase 17.2's own operator-authenticated SHADOW acceptance tests remain
  blocked — this phase does not touch, resolve, or attempt to work around
  that blocker in any way.

## 21. Phase 17.2 status (unchanged)

Phase 17.2 = **BLOCKED-PENDING-AUTHORIZED-OPERATOR**, exactly as concluded
in the prior report
(`docs/phase-17-2-controlled-production-deployment-connected-shadow-report.md`).
Nothing in this phase created, promoted, or reset any operator account;
nothing bypassed RBAC; no `LiveAuthorization` was created or confirmed; no
live strategy was started; no live canary instrument was chosen; no real
order was placed, modified, or cancelled.

---

**PHASE 17.2-P = PASS.**
**Phase 17.2 operator-authenticated acceptance: STILL BLOCKED-PENDING-AUTHORIZED-OPERATOR.**
**Phase 17.3 authorized: NO.**

The next action after Phase 17.2-P is still to obtain a legitimate
authorized operator session and finish the blocked Phase 17.2 SHADOW
acceptance tests.

**HARD STOP.**
