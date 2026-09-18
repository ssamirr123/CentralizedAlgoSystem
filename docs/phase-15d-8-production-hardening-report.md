# Phase 15D.8 — Production Deployment & Security Hardening

**Status: PASS**
**Scope: production/deployment hardening only. No real broker order was placed. No real broker connection was made. No real live authorization was granted. No strategy was started.**

---

## 1. Objective

Answer, and close the concrete gaps behind: *"If this system is
deployed as a production service, can we safely start, stop, restart,
recover, monitor, and operate it without weakening the live-trading
safety boundary?"* — hardening only; readiness judgment is explicitly
deferred to a separate Phase 15D.9, run independently.

## 2. Scope actually implemented (as agreed before implementation)

| Item | Status |
|---|---|
| Kill-switch persistence mandatory in production | ✅ implemented |
| Audit persistence mandatory in production | ✅ implemented |
| Missing production safety paths fail closed at startup | ✅ implemented |
| File permissions `0600` where applicable | ✅ implemented (POSIX) |
| Broker readiness probe: bounded, read-only, no order API | ✅ implemented |
| Clock-drift diagnostic: read-only, never changes trading behavior | ✅ implemented |
| SQLite backup + restore verification | ✅ implemented |
| Rollback runbook | ✅ documented (section 9) |
| Stale 15D-DR claim documented without rewriting that report | ✅ documented (section 10) |
| Secrets manager | ⏸️ deferred (dedicated infra-security phase) |
| Account authorization-state persistence | ⏸️ deferred (current reset-to-READ_ONLY remains fail-safe) |
| CI/CD pipeline | ⏸️ deferred |
| Actual AWS infrastructure changes | ⏸️ deferred (this phase is read-only w.r.t. `trading/infrastructure/`) |

## 3. Files changed

**New:**
- `trading/common/production_guard.py` — `check_production_safety_paths()`,
  `is_production_environment()`, `ProductionSafetyError`.
- `trading/common/file_permissions.py` — `harden_file_permissions()`
  (POSIX `0600`, silent no-op elsewhere, never raises).
- `trading/common/time_sync.py` — `check_clock_drift()`,
  `configured_warn_threshold_seconds()`, `ClockDriftResult`.
- `trading/tools/backup_safety_stores.py` — `snapshot_one()`,
  `verify_snapshot()`, `restore_one()`, plus a CLI (`snapshot`/`restore`
  subcommands).
- `tests/test_phase_15d_8_production_hardening.py` — 39 tests.
- `docs/phase-15d-8-implementation-plan.md`, this report.

**Modified:**
- `trading/api/app.py` — `lifespan()` now calls
  `check_production_safety_paths()` as its very first action, before
  `init_db()`, before the watcher/scheduler.
- `trading/api/health.py` — `/api/ready`'s `broker` field now reports a
  real (bounded, read-only) signal instead of a hardcoded `"unknown"`;
  `/api/health` gained an additive `clock_drift` field.
- `trading/common/idempotency_store.py`, `trading/common/audit_store.py`,
  `trading/common/reconciliation.py`, `trading/common/live_authorization.py` —
  each store's constructor now calls `harden_file_permissions(self._db_path)`
  once, right after schema init.
- `trading/common/kill_switch.py` — `_save()` now calls
  `harden_file_permissions()` on the persistence JSON file after every write.
- `tests/api/test_health.py` — updated for the new `clock_drift` field.

## 4. Production kill-switch / audit persistence — mandatory, fail-closed

`trading/common/production_guard.py::check_production_safety_paths()`:
outside a production-like `ENVIRONMENT`/`ENV` value (`"production"` /
`"prod"`, case-insensitive), it is a complete no-op — every dev/test/CI
run sees zero behavior change. Inside a production-like environment, it
requires **both** `KILL_SWITCH_PERSISTENCE_PATH` and `AUDIT_DB_PATH` to
be set and point at a writable location, and raises
`ProductionSafetyError` (which `trading/api/app.py::lifespan()` lets
propagate, failing ASGI startup) otherwise. This closes the exact gap
found during this phase's own read step: a production deploy that
simply forgot either env var previously ran silently degraded — an
in-memory kill switch that resets to **disengaged** on every restart,
and/or a lost audit trail. Verified end-to-end (not just at the
function level): `test_lifespan_startup_fails_in_production_without_persistence_paths`
actually boots the real FastAPI app via `TestClient` and confirms
startup fails; `test_lifespan_startup_succeeds_in_production_with_persistence_paths`
confirms it starts cleanly once both paths are configured.

## 5. File-permission hardening

`harden_file_permissions()` is POSIX-only (`os.chmod`, mode `0o600`) and
a documented no-op on Windows (NTFS ACLs don't map to POSIX mode bits;
this project's production target is Linux/EC2 per
`trading/infrastructure/backend/systemd/*`) — never raises. Wired into
the four safety-critical SQLite stores' constructors and the kill
switch's every JSON write. Tests skip the assertion on non-POSIX
platforms rather than asserting a platform-specific behavior that would
be meaningless there.

## 6. Broker readiness probe

`trading/api/health.py::_check_broker_readiness()` reports the
**current** connection state of whatever broker client(s) are already
attached to a registered account (`account.broker_client`) — it
**never** calls `BrokerManager.get_broker()`, preserving the existing,
explicit invariant documented in `trading/api/execution_state.py`
("no endpoint ... ever calls `BrokerManager.get_broker()`"). Only
`is_connected()` is called, never `connect()` or any order/mutation
method. Bounded to 1.5 seconds total via a one-shot `ThreadPoolExecutor`
whose pool is shut down with `wait=False` on timeout — **a real bug was
found and fixed during testing**: an initial implementation used
`with ThreadPoolExecutor(...) as pool:`, whose `__exit__` calls
`shutdown(wait=True)`, which silently defeated the whole timeout by
blocking on the very call it was meant to bound.
`test_broker_readiness_check_is_bounded` (a broker whose `is_connected()`
sleeps 5 seconds) caught this immediately: the endpoint returned in
~5.3s instead of the intended ~1.5s. Fixed by managing the executor
without a context manager and calling `shutdown(wait=False)` explicitly
in a `finally` block. `application` (process/DB readiness) and `broker`
(broker readiness) remain two independent fields — one can never gate
or slow down the other.

## 7. Clock-drift diagnostic

`trading/common/time_sync.py::check_clock_drift()` is opt-in
(`reference_clock=None` by default → `not_checked` immediately, zero
cost) and purely informational: `ClockDriftResult` has no field or
method that could gate execution (`test_clock_drift_never_changes_trading_behavior`
asserts this directly). Surfaced as an additive `clock_drift` field on
`/api/health`, which never affects that endpoint's `status`/503
decision. No reference clock (NTP or broker server-time) is wired in
this phase — a named, explicit limitation (section 11).

## 8. Backup / restore tooling

`trading/tools/backup_safety_stores.py` uses SQLite's own online backup
API (`sqlite3.Connection.backup()`) for `.db` files — safe against a
live, in-use database, unlike a plain file copy that can capture a torn
write mid-transaction — and an atomic rename-based copy for the
kill-switch JSON. `restore_one()` refuses to restore a snapshot that
fails its own `PRAGMA integrity_check` (or, for non-SQLite files,
existence + non-empty), and re-verifies the restored copy afterward.
Deliberately **not** scheduled or run automatically by this phase — an
operator invokes it explicitly (`python -m trading.tools.backup_safety_stores
snapshot ... --out-dir ...`); wiring it to cron/a systemd timer is a
separate, explicit deployment decision left to whoever operates the
production deployment.

## 9. Rollback procedure (runbook)

1. Identify the last known-good release (Git commit SHA / image tag).
2. Engage the kill switch first if the bad deploy may have altered
   trading state (`POST` the existing kill-switch API endpoint, or set
   it directly via `CentralKillSwitch.engage()` against the configured
   `KILL_SWITCH_PERSISTENCE_PATH`) — this survives the rollback itself
   because it is file-persisted, independent of which code version is
   running.
3. Redeploy the previous image tag (`docker-compose.prod.yml`) or
   `git checkout` to the previous release commit on the EC2 host managed
   by `trading/infrastructure/backend/systemd/centralized-algo-backend.service`.
4. Run `alembic downgrade` only if the bad deploy included a forward
   migration that the previous code version cannot read; otherwise skip
   (Alembic migrations in this project are additive by convention).
5. Restart the service (`systemctl restart centralized-algo-backend`,
   which re-runs `docker/entrypoint.sh` → migrations → uvicorn).
6. Confirm `GET /api/health` returns `200`/`"ok"` and `GET /api/ready`
   reports `application: "ready"`.
7. Confirm the kill switch's state (engaged/disengaged, per step 2)
   survived the restart by re-reading `KILL_SWITCH_PERSISTENCE_PATH`
   directly, and confirm `trading_authorized` is still hard-coded
   `false` on `/api/ready` (it always is — no code path can change this).
8. Only after 6–7 pass, disengage the kill switch (an explicit, human,
   audited action) if it was engaged purely as a rollback precaution.

## 10. Reconciling the stale 15D-DR claim

`docs/phase-15d-deployment-recovery-safety-report.md` states "No CI/CD,
container orchestration, or automated rollback tooling exists." That
report is **not** modified (this project's standing precedent: never
rewrite a prior phase's historical report). As of this phase, that
statement is **stale** — a real `Dockerfile`, `docker-compose.yml` /
`docker-compose.prod.yml`, systemd units, and an AWS infrastructure tree
(`trading/infrastructure/`) exist, predating this phase but postdating
that report. This phase's own report (here) is the durable record of
that reconciliation.

## 11. Test counts

`tests/test_phase_15d_8_production_hardening.py`: **39 passed** (6
skipped on this Windows dev machine — the POSIX-only file-permission
assertions, which run for real on the Linux/EC2 production target).

Targeted re-run alongside this phase's own suite: `tests/api/test_health.py`,
`tests/test_phase_15d_7_operator_authorization.py`,
`tests/common/test_phase_15d_5_live_authorization.py`,
`tests/common/test_phase_15d_6_live_authorization_workflow.py`,
`tests/algos/test_angel_creds_env_isolation.py`,
`tests/preflight/test_environment_isolation.py`,
`tests/common/test_phase_15d_dr_deployment_recovery.py`,
`tests/common/test_phase_15d_dr_deployment_smoke.py` — **all passed, 0
failed** (also confirmed the `ready()` signature change doesn't break
Phase 15D-DR's own direct-call tests — see section 12's incident note).

## 12. Full regression result

`python -m pytest -q --tb=line > file.txt 2>&1; echo "EXIT=$?" >> file.txt`
(redirect-based, never piped through `tail`).

**First full run surfaced a real regression**: changing `/api/ready`'s
signature to require `request: Request` broke two pre-existing Phase
15D-DR tests (`test_readiness_endpoint_never_reports_trading_authorized_true`,
`test_10_health_endpoint_reachable_and_never_reports_trading_authorized`)
that call `ready()` directly as a plain Python function, with no request
object. **Fixed** by making `request` optional (`request: Request = None`,
resolved without breaking FastAPI's special-case injection of the real
`Request` for the actual HTTP route — a plain `Request | None` type
annotation was tried first and rejected by FastAPI's pydantic-based
schema generation, so the parameter keeps the bare `Request` annotation
with a `None` default instead) — a direct call with no request now
degrades to `broker="unknown"`, identical to this endpoint's
pre-15D.8 behavior, while the real route still receives and uses the
actual request.

**Second full run**: `EXIT=0`, **0 explicit `FAILED` lines, 0 explicit
`ERROR` lines**, and the progress-line character tally (1615 dots + 6
skips) confirmed independently — no `F`/`x` characters, and the sole `E`
character found was the leading letter of the `EXIT=0` marker itself,
not a test result (verified directly: no line matches an error-result
pattern).

## 13. Broker mutation count / live account state

- Real broker mutation calls this phase: **0**. Every test uses
  `_FakeBroker` (this phase's own file) or `RecordingFakeBroker`
  (Phase 15D.6/15D.7); `_FakeBroker.place_order()` raises
  `AssertionError` if ever called, and no test triggers it.
- Real broker connections this phase: **0** — the broker-readiness probe
  never calls `connect()` or the lazy `get_broker()` factory.
- Live authorization: **NOT GRANTED** — no `*live_auth*.db` exists
  anywhere outside test `tmp_path` databases (re-confirmed).
- Account A: READ_ONLY (unchanged). Account B: READ_ONLY/FLAT (unchanged).
- Strategies: STOPPED (none started this phase).

## 14. Historical-integrity verification

- `260917000350205`: `status=COMPLETED` — unchanged.
- AG7002 record: `status=PENDING` — unchanged.
- `260917000523943` (manual SELL): still no idempotency record —
  remains unattributed to TCC.
- `trading/phase15d2_audit.db` hash chain: `verify()` → `True`, 14
  records (unchanged count).

## 15. Known limitations

- No reference clock (NTP/broker server-time) is actually wired into
  `check_clock_drift()` yet — the diagnostic and its `/api/health`
  surface exist, but report `not_checked` until a future phase supplies
  a real reference source.
- Backup/restore is manual/on-demand only; scheduling it is a
  deployment-configuration decision for whoever operates production, not
  code this phase enables by default.
- Secrets-manager integration, account-authorization-state persistence,
  a build/test CI pipeline, and any change to deployed AWS infrastructure
  remain explicitly out of scope (per the agreed 15D.8 boundary) —
  named here so they are tracked, not silently dropped.
- `/api/ready`'s broker signal only ever reflects an *already-attached*
  broker client's connection state; it cannot report on a broker that a
  deployment hasn't wired a live connection for yet (by design — see
  section 6).

---

## Conclusion

```
PHASE 15D.8 = PASS
NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
NO REAL BROKER CONNECTION MADE.
HARD STOP.
```
