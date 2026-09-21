# Phase 15D.9 — Final Live-Readiness Control Matrix

**This is a READ + ANALYZE document. No code was modified to produce it.**

Status key: **PASS** (implemented + tested + evidence found), **PARTIAL**
(implemented but with a named gap), **BLOCKED** (a real safety property
does not hold, or cannot be confirmed to hold, in the actual deployed
configuration).

| Control | Required | Implementation | Tests | Evidence | Status |
|---|---|---|---|---|---|
| Authentication | YES | `trading/common/operator_identity.py` (`OperatorIdentity`, `AuthState`), `trading/api/security/tokens.py` (JWT), `trading/api/deps.py` (`Principal`) | `tests/test_phase_15d_7_operator_authorization.py` (55), `tests/api/test_auth.py` (12) | Unauthenticated/invalid/disabled operator rejected before any authorization step; login/logout/refresh/rate-limit covered | **PASS** |
| Operator identity | YES | `OperatorIdentity`, `FakeAuthenticationProvider`, `JwtClaimsAuthenticationProvider` | `tests/test_phase_15d_7_operator_authorization.py` | `authorized_by` derived from identity, never caller-supplied free text (Phase 15D.7 closed the Phase 15D.6 gap) | **PASS** |
| RBAC | YES | `trading/common/operator_identity.py::LivePermission`/`ROLE_PERMISSIONS`; separate from `trading/api/security/permissions.py::Permission` (existing control-center RBAC, unmodified) | 15D.7 suite: viewer/trader/admin cases 5-9 | Viewer cannot request/confirm; missing-permission denied; kill switch fail-safe independent of permission | **PASS** |
| Account ownership | YES | `TradingAccount.owner_id` (existed since 15B, unused until 15D.7) + `AuthorizationService.evaluate()` | 15D.7 cases 10-12 | Wrong-owner operator denied; account-A auth cannot execute account-B and vice versa | **PASS** |
| Credential isolation | YES | `credential_reference` exact-match in `validate_request()`/`try_consume()` | 15D.5/15D.6/15D.7 credential-mismatch cases | Credential substitution rejected at multiple layers (request validation + consume-time) | **PASS** |
| Exact authorization (instrument/side/qty/type/product/key) | YES | `LiveAuthorization.try_consume()`'s field-by-field compare | 15D.5 (8 replay cases), 15D.6 (cases 18-23), 15D.7 (cases 16-21) | Every field independently tampered and rejected, at 3 separate layers (unit/workflow/RBAC) | **PASS** |
| Human confirmation | YES | `LiveAuthorizationWorkflow.confirm()` + `ConfirmationProvider` (only literal `True` counts) | 15D.6 cases 24-25, explicit decline/exception tests | Missing/invalid/declined/exception-raising confirmation never approves | **PASS** |
| Authorization expiry | YES | `LiveAuthorization.is_expired()`, durable-on-read transition to EXPIRED | 15D.5 (durable-on-read test), 15D.6 case 15, 15D.7 case 23 | Expiry checked and persisted at `get()`, not merely computed | **PASS** |
| Replay protection | YES | Idempotency `claim()` (atomic, immediately pre-broker-call) + `LiveAuthorization` single-use CAS | 15D.3-R, 15D.5, 15D.6, DR-recovery suites | Concurrent duplicate requests reach broker at most once; second execute() call replays cached result | **PASS** |
| Kill switch | YES | `trading/common/kill_switch.py::CentralKillSwitch`, checked unconditionally at `execute()` step 0 | `tests/common/test_kill_switch.py`, DR-recovery persistence tests, 15D.5/15D.6/15D.7 fail-safe cases | Fails closed on corrupt file; blocks even a fully-authorized action; independent of any permission | **PASS** |
| Risk limits | YES (LIVE requires explicit) | `RiskManager.validate()` (14-15 checks) + `RiskLimits.is_live_ready()` gate for LIVE mode specifically (`execution.py:784`) | `tests/common/test_risk_manager.py` (55), `test_risk_manager_account_isolation.py` | **Important nuance**: a bare `RiskManager()` with default `RiskLimits` does NOT itself block a LIVE order on numeric limits (`None` = unenforced) — LIVE-safety depends entirely on `is_live_ready()` being called before a LIVE broker call, confirmed still wired at `execution.py:784` | **PASS** (with the dependency named, not hidden) |
| Canary limits | YES | `trading/common/live_canary.py::CanaryLimits`/`LiveCanaryGuard` — no field has a default; `__post_init__` fails to even construct on an invalid limit | Phase 14.6 hardening tests, 15D.1/15D.2 canary reports | Dedicated-single-account allow-list, hard quantity/value/loss/order-count caps, emergency-shutdown one-way latch | **PASS** |
| Idempotency | YES | `SqliteIdempotencyStore`, persistent by default, atomic `claim()` | `tests/common/test_idempotency_store.py` (11) + cross-cutting in every other suite | Restart-safe, concurrent-safe, hardened to `0600` (Phase 15D.8) | **PASS** |
| Broker response handling | YES | `trading/common/broker_response_validation.py`, unconditional for every mode (Phase 14.6 Blocker B) | `test_broker_response_validation.py` (10), engine-level tests | Ambiguous/malformed/timeout responses never silently treated as success | **PASS** |
| Reconciliation | PARTIAL | `trading/common/reconciliation.py`, order-level only | `tests/common/test_phase_15d_recon.py` (31) | Order-level reconciliation solid; **position-based corroboration is not implemented** — a named, carried-forward limitation since Phase 15D.4 | **PARTIAL** |
| Audit trail | YES | `trading/common/audit_store.py::PersistentAuditTrail`, hash-chained, append-only at the DB level | `tests/common/test_phase_15d_audit_persistence.py` (43) | UPDATE/DELETE rejected at the DB level; corrections are new events, never mutations | **PASS** |
| Audit integrity | YES | Hash chain (`verify_chain()`) | Same suite + direct re-verification this phase (`trading/phase15d2_audit.db` → `verify()` = `True`, 14 records) | Chain verified valid as of this phase | **PASS** |
| Persistence (idempotency/audit/recon/live-auth) | YES | All four SQLite stores persistent by default, `harden_file_permissions()` wired into each (Phase 15D.8) | Store-level tests + `test_phase_15d_8_production_hardening.py` | Confirmed present in all 4 files via direct grep this phase | **PASS** |
| Restart recovery | YES | DR suite: idempotency/kill-switch/authorization all durable across a fresh store instance against the same file | `test_phase_15d_dr_deployment_recovery.py` (21), `..._smoke.py` (13) | No stale record silently resumed; ambiguous response requires manual reconciliation, never auto-retry | **PASS** |
| Environment isolation | YES | `_REQUIRED_ENV_KEYS` scoping in legacy algo configs (Phase 15D.3-R fix) | `test_angel_creds_env_isolation.py` (8), `test_environment_isolation.py` (11) | Credential-leak root cause fixed and regression-tested | **PASS** |
| Production guard | **NO — see finding** | `trading/common/production_guard.py`, wired into `trading/api/app.py::lifespan()` | `test_phase_15d_8_production_hardening.py` (guard unit + end-to-end lifespan tests) | **The guard checks `ENVIRONMENT`/`ENV`. The actual `docker-compose.prod.yml`/`docker-compose.yml` set only `APP_ENV`. In the real deployed configuration as it exists today, `_current_environment()` resolves to `"development"`, so the guard silently no-ops and never actually checks whether `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH` are set.** This is a pre-existing repo-wide convention (`deployment_info.py` has the identical `ENVIRONMENT`/`ENV`-only precedence, predating Phase 15D.8) that Phase 15D.8 faithfully mirrored rather than inventing a third competing convention — but the practical effect is the guard does not protect the real deployment as configured. | **BLOCKED** |
| Broker readiness | YES | `trading/api/health.py::_check_broker_readiness()`, bounded (1.5s), read-only | `test_phase_15d_8_production_hardening.py` (bounded-timeout test caught a real bug pre-fix) | Never calls `get_broker()`/`connect()`/any mutation method; times out safely | **PASS** |
| File permissions | YES | `trading/common/file_permissions.py::harden_file_permissions()` | 15D.8 suite (POSIX-gated) | `0600` confirmed on all 4 stores + kill-switch JSON; documented no-op on Windows | **PASS** |
| Clock diagnostic | YES (informational only) | `trading/common/time_sync.py::check_clock_drift()` | 15D.8 suite | Opt-in, `not_checked` by default, provably has no field/method that could gate execution | **PASS** (no reference clock wired yet — named limitation) |
| Backup/restore | YES | `trading/tools/backup_safety_stores.py` | 15D.8 suite (snapshot/verify/restore round-trip, corruption detection) | SQLite online-backup API used (safe against a live DB); confirmed NOT wired into any scheduled job (manual-only, by design) | **PASS** |
| Rollback | YES (documented) | `docs/phase-15d-8-production-hardening-report.md` section 9 | N/A (runbook, not code) | Documented procedure exists; not automated (explicitly deferred, named) | **PARTIAL** (documented, not automated — acceptable per the agreed 15D.8 scope) |
| Monitoring | PARTIAL | `trading/common/observability.py`, `trading/common/log_shipper.py`, `trading/common/heartbeat.py`, CloudWatch agent config in `trading/infrastructure/` | Not deeply re-verified this phase (out of the agreed 15D.8/15D.9 scope) | Exists but was not exhaustively audited in this readiness pass | **PARTIAL — not independently re-verified** |
| Operational controls | **NO — see finding** | `StrategyExecutionEngine` + the whole Phase 15D chain governs the NEW execution path only | N/A — no test exists for this property because it was not previously in scope | **The legacy algo processes (`trading/algos/DoubleStraddelAlgo`, `CombinedVwapNifty`, `Vwap_Algo_Nifty_hedge`) place real broker orders via a raw SmartAPI `config.objconn.placeOrder(...)` call, gated ONLY by each algo's own independent `DRY_RUN` env var (default `"true"`) — entirely outside `CentralKillSwitch`, `RiskManager`, `LiveAuthorization`, and every other control in this matrix.** Engaging the new kill switch has zero effect on these processes. | **BLOCKED** |

---

## Notes on cross-cutting redundancy

Several properties (account isolation, exact-action binding, kill-switch
fail-safety) are independently re-asserted at 3-4 different layers
(unit → workflow → RBAC → audit) rather than tested once — this is a
strength, not padding: a regression in any single layer would still be
caught by the others.

## Two BLOCKED rows, in one sentence each

1. **Production guard**: it protects nothing in the actual deployed
   containers today, because the compose files use `APP_ENV` and the
   guard (faithfully mirroring the rest of this codebase) checks
   `ENVIRONMENT`/`ENV`.
2. **Operational controls**: the three legacy algo processes can place
   real orders through a completely separate path that the entire
   Phase 15D safety framework — kill switch included — cannot see or stop.

Full detail, remediation options, and whether these matter for a
narrowly-scoped 15D.10 canary specifically (as opposed to "is the whole
Trading Control Center safe") are in
`docs/phase-15d-9-final-readiness-plan.md`.
