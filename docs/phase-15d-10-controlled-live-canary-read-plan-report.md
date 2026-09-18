# Phase 15D.10 — Controlled Live Canary: READ + PLAN Gate

**This is a READ + ANALYZE + PLAN document only. No code, test, deployment, or infrastructure file was modified to produce it. No broker mutation, live authorization, or strategy execution occurred.**

---

## 1. Scope

Determine whether the repository is technically and operationally ready
for a controlled live canary execution under the existing safety
architecture (Phases 15D.1–15D.9, all PASS). This phase performs READ →
ANALYZE → PLAN → REPORT → STOP only.

## 2. Safety constraints observed throughout this phase

```
Real broker mutation calls = 0
Real broker orders = 0
Live authorizations created = 0
Strategy executions = 0
Account A = READ_ONLY (unchanged)
Account B = READ_ONLY / FLAT (unchanged)
Strategies = STOPPED (unchanged)
```

No file was edited to produce this report — confirmed by `git status`
showing the same 64 pending changes (all pre-dating this phase) both
before and after this analysis.

## 3. Current architecture (verified from code, not documentation)

`StrategyExecutionEngine.execute()` (`trading/common/execution.py`),
gate order confirmed by direct grep of its own step-comment markers this
phase:

```
step 0 (line 640): central kill switch — unconditional, every mode, first
step 1 (line 649): idempotency REPLAY check (authoritative, persistent)
step 2 (line 666): resolve TradingAccount + AuthorizationState hard gate
step 3 (line 732): RiskManager.validate() — always all 15 checks
step 4 (line 755): mode-specific structural gate
                     (LIVE_CANARY → LiveCanaryGuard.authorize();
                      LIVE → RiskLimits.is_live_ready())
step 5 (line 793): broker resolution
                     → Phase 15D.5 gate: LiveAuthorization.try_consume()
                       (only if intent carries authorization_id)
                     → Phase 15D-DR gate: idempotency CLAIM (atomic,
                       immediately pre-broker-call)
                     → BrokerClient.place_order()
step 6 (line 891): broker response validation (Blocker B) — every mode
step 7 (line 899): persist the definitive idempotency outcome
```

This is the actual, code-verified order — it matches every prior
phase's documentation exactly; no drift found.

`trading/common/live_authorization_workflow.py` wraps this with the
REQUEST → VALIDATE → PREFLIGHT → CONFIRM sequence (Phase 15D.6/15D.7),
consulting `AuthorizationService.evaluate()` (`trading/common/live_authorization_service.py`)
at both REQUEST and CONFIRM time. `trading/common/operator_identity.py`
supplies the authenticated `OperatorIdentity` those calls require.

`trading/common/legacy_execution_guard.py` (Phase 15D.9) and
`trading/common/legacy_algo_readiness.py` (Phase 15D.9) sit outside this
chain entirely — they protect/report on the three legacy algo processes,
not the path above.

`trading/common/production_guard.py` + `trading/common/deployment_info.py`
(centralized `resolve_environment()`, Phase 15D.9), `trading/common/file_permissions.py`,
`trading/common/time_sync.py`, `trading/tools/backup_safety_stores.py` —
all Phase 15D.8/15D.9 deployment-hardening modules, confirmed unchanged
and still wired (`check_production_safety_paths()` still called first
thing in `trading/api/app.py::lifespan()`).

## 4. Complete mutation-path inventory

Re-confirmed by direct grep this phase (`\.place_order\(`, `objconn.placeOrder`, `\.modify_order\(`):

| Path | File | Gated by |
|---|---|---|
| New centralized path | `trading/common/execution.py` (`_call()` inside `place_limit`/`place_market_emergency`) | Full chain (Section 3) |
| `DoubleStraddelAlgo` | `trading/algos/DoubleStraddelAlgo/broker/orders.py` (2 call sites) | `DRY_RUN` (default `true`) + `assert_live_mutation_allowed()` (Phase 15D.9) |
| `CombinedVwapNifty` | `trading/algos/CombinedVwapNifty/rest_func.py` (2 call sites) | `DRY_RUN` (default `true`) + `assert_live_mutation_allowed()` |
| `Vwap_Algo_Nifty_hedge` | `trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py` (2 call sites) | **no DRY_RUN gate** + `assert_live_mutation_allowed()` (Phase 15D.9 — this is now the ONLY gate on this file) |
| `AngelOneBroker`/`DhanBroker`/`ICICIBreezeBroker.modify_order()` | `trading/common/brokers/*.py` | read-only-mode check only; not part of `BrokerClient`; still only called by the two deliberate read-only validation harnesses (`angel_readonly.py`, `angel_connected_shadow.py`) — re-confirmed zero other callers this phase |
| `ConnectedShadowBroker.place_order()` | `trading/common/brokers/connected_shadow_broker.py` | structurally never references the real broker (Phase 5B fail-closed design, re-confirmed) |
| `ShadowBroker.simulate_intent()` | `trading/common/brokers/shadow_broker.py` | fully simulated, in-memory only, not a real broker |

No new mutation path was found beyond the ones documented in Phase
15D.9's own report. The `assert_live_mutation_allowed()` wiring added in
15D.9 is confirmed still present (structural regression tests for this
exist in `tests/test_phase_15d_9_readiness_gate_closure.py` and still
pass — see Section 17).

## 5. Legacy algorithm analysis

| | DoubleStraddelAlgo | CombinedVwapNifty | Vwap_Algo_Nifty_hedge |
|---|---|---|---|
| Can independently place an order | Yes | Yes | Yes |
| Central execution engine used | No | No | No |
| RiskManager | No | No | No |
| LiveAuthorization | No | No | No |
| Idempotency | No | No | No |
| Kill switch | **Yes (Phase 15D.9)** | **Yes (Phase 15D.9)** | **Yes (Phase 15D.9)** |
| Dry-run | Yes, default `true` | Yes, default `true` | **No dry-run gate exists** |

**Explicit documentation of the known Phase 15D.9 limitation** (verbatim
per this phase's own instruction): *the legacy algorithms currently have
only the centralized kill-switch protection at their direct placeOrder
boundary. They do not inherit the complete centralized execution chain.*

**Whether this is acceptable for the proposed 15D.10 canary is not
decided here** — that decision belongs to Section 19 (human decision
gate). The only fact this report adds: the 15D.10 canary, exactly like
the one already-completed successful canary, would go through the NEW
centralized path (Section 3), not through any of these three legacy
scripts — so this limitation affects the *legacy* algos' own independent
risk, not the mechanics of a 15D.10 canary transaction itself. Whether
those legacy processes must additionally be confirmed STOPPED/DRY_RUN
*during* the canary window (so their independent, ungated risk doesn't
coincide with the canary window) is exactly what Section 7's readiness
check is for.

## 6. Central execution-path analysis

Confirmed present and enforced, in the order shown in Section 3, by
direct code read this phase (no assumption from documentation):
`CentralKillSwitch` check, idempotency replay check, `AuthorizationState`
gate, `RiskManager.validate()` (14–15 checks), `RiskLimits.is_live_ready()`
for plain LIVE mode (`execution.py:784`, re-confirmed present), broker
resolution, `LiveAuthorization.try_consume()`, idempotency `claim()`,
`place_order()`, broker response validation, idempotency-outcome
persistence.

## 7. Authorization analysis

Verified against `trading/common/live_authorization.py`,
`live_authorization_workflow.py`, `live_authorization_service.py`,
`operator_identity.py` (all re-read this phase):

1. **Exact-account scoped** — `try_consume()` compares `account_id` exactly. **PASS**
2. **Exact-action scoped** — `try_consume()` compares `symbol`, `side`, `quantity`, `order_type`, `product_type`, `idempotency_key`, and (Phase 15D.7) `operator_id` when supplied, exactly. **PASS**
3. **Expires** — `LiveAuthorization.is_expired()`, durable-on-read transition to `EXPIRED` at `get()`. **PASS**
4. **Single-use** — `try_consume()`'s atomic `UPDATE ... WHERE status='AUTHORIZED'` compare-and-swap; a second attempt always fails. **PASS**
5. **Restart behavior** — a PENDING/AUTHORIZED record surviving past its `expires_at` is durably transitioned to `EXPIRED` on the next `get()` after restart (not merely recomputed); a CONSUMED record stays CONSUMED. **PASS**
6. **Operator identity authenticated** — `AuthorizationService.evaluate()` requires `operator.is_authenticated`; `validate_request()`/`confirm()` both call it. **PASS**
7. **Account ownership checked** — `TradingAccount.owner_id` compared against `operator.operator_id` when the account declares an owner; house/shared accounts (no declared owner) are not additionally restricted. **PASS**, with the pre-existing, already-documented nuance that an un-owned account is not ownership-restricted (Phase 15D.7's own explicit design choice, not a gap).
8. **Kill switch fail-closed** — corrupt/unreadable persistence file forces `engaged=True`; unconfigured path is in-memory-only, disengaged by default (documented, accepted default). **PASS**
9. **RiskManager enforced** — always all checks; LIVE mode additionally gated by `is_live_ready()`. **PASS**
10. **Idempotency persistent** — `SqliteIdempotencyStore`, file-backed by default, `0600`-hardened (Phase 15D.8). **PASS**
11. **Ambiguous broker responses do not auto-retry** — `AmbiguousOrderStateError` routes to a terminal `STATUS_AMBIGUOUS` outcome requiring manual reconciliation, confirmed in Phase 15D-DR's own test suite (re-run this phase, still passing). **PASS**
12. **Reconciliation detects missing/partial/filled** — `ReconciliationStatus` enum + `ReconciliationService`, confirmed via `tests/common/test_phase_15d_recon.py` (still passing this phase). **PASS**, with the standing, named limitation that only order-level (not position-level) corroboration is implemented.
13. **Audit records the complete lifecycle** — `PersistentAuditTrail`, hash-chained, append-only at the DB level; `AUTHORIZATION_CREATED/_VALIDATED/_CONSUMED`, `OPERATOR_AUTHENTICATED/_AUTHORIZATION_REQUESTED/_DENIED/_CONFIRMATION_ACCEPTED/_DECLINED`, `ORDER_INTENT_CREATED`, `RISK_DECISION`, broker-order events, `DEPLOYMENT_STARTUP/_SHUTDOWN` all confirmed present. **PASS**
14. **Broker response validation enforced** — `broker_response_validation.py`, unconditional for every mode since Phase 14.6 Blocker B. **PASS**

## 8. Risk analysis

`RiskManager.validate()` runs 14–15 checks unconditionally (re-confirmed
in this session's Phase 15D.8/15D.9 work: `RiskLimits`'s numeric fields
default to `None` = unenforced unless explicitly configured — this is
by design, not a gap, because plain LIVE mode is separately, additionally
gated by `RiskLimits.is_live_ready()`, which requires 7 explicit,
positive numeric limits and fails closed on any missing/zero/negative
value). `execution.py:784` confirmed to still call this gate for LIVE
mode. `LiveCanaryGuard.authorize()` (the mode actually used for the
prior successful canary and the mode a 15D.10 canary would most likely
use again) runs its own, separately fail-closed 10 checks
(`_check_shutdown`, `_check_kill_switch`, `_check_dedicated_account`,
`_check_idempotency_present`, `_check_duplicate`, `_check_max_quantity`,
`_check_max_order_value`, `_check_max_daily_loss`, `_check_max_strategy_loss`,
`_check_max_orders_per_day` — confirmed by direct grep of `live_canary.py`
this phase), and `CanaryLimits` has **no default value on any field** —
it cannot even be constructed with a missing/invalid limit.

## 9. Kill-switch analysis

`CentralKillSwitch` (re-read this phase): checked unconditionally, first,
in `execute()`; fails closed on a corrupt/unreadable persistence file
(forces `engaged=True`); disengaged-in-memory when no persistence path is
configured (documented default, unchanged); `harden_file_permissions()`
wired into `_save()` (Phase 15D.8, re-confirmed present). Phase 15D.9
additionally wired the same class into the three legacy algo scripts via
`legacy_execution_guard.py` (Section 5).

## 10. Idempotency analysis

`SqliteIdempotencyStore`: persistent by default (not in-memory), atomic
`claim()` immediately pre-broker-call, `0600`-hardened. Restart-durable
per Phase 15D-DR's own suite (re-run this phase, passing): no stale
PENDING record is ever silently resumed as if it succeeded; a confirmed
rejection persists a terminal `STATUS_REJECTED` (Phase 15D.3-R fix,
re-verified present).

## 11. Reconciliation analysis

`ReconciliationService`/`SqliteReconciliationStore`: order-level
classification (filled/rejected/partial/not-found/ambiguous), durable
`try_acquire()` compare-and-swap so only one reconciliation attempt owns
a record at a time, restart-recoverable. **Named limitation, unchanged
since Phase 15D.4**: no position-based corroboration — this reconciles
against the broker's own order-status API only, not the account's actual
held positions.

## 12. Audit analysis

`PersistentAuditTrail`: hash-chained (`verify_chain()`), append-only
enforced at the SQLite level (UPDATE/DELETE trigger-rejected, per Phase
15D-audit's own test suite), correlated by `correlation_id`/
`idempotency_key`/`account_id`. Re-verified this phase directly:
`trading/phase15d2_audit.db` → `verify()` = `True`, 14 records (unchanged
count from every prior phase this session checked it).

## 13. Account-state verification

No real Angel One credentials exist in this development environment
(confirmed by direct `env` inspection this phase — zero
`ANGELONE_*`/`ANGEL_*` variables present), and per this phase's own
instruction, no workaround involving a mutation-capable connection
attempt was made.

```
READ-ONLY VERIFICATION UNAVAILABLE
```

The last positively-verified state (Phase 15D.9, same session, direct
read of `trading/phase15d2_idempotency.db`) showed the historical
canary `260917000350205` `COMPLETED` and the manual SELL
`260917000523943` closing that position, with no idempotency record
attributing it to TCC. This report does **not** assume that state still
holds today — a live, real-credential read-only check immediately before
any future canary authorization is a hard prerequisite (Section 19),
not something this phase can substitute with a stale prior reading.

## 14. Production deployment analysis

- `production_guard.py`/`deployment_info.py`: centralized
  `resolve_environment()` (Phase 15D.9 fix) — `APP_ENV` → `ENVIRONMENT` →
  `ENV` → `"development"`. Confirmed still a single function (no
  re-drift) via the same structural test from Phase 15D.9
  (`test_deployment_info_and_production_guard_use_the_same_function`,
  re-run this phase, passing).
- `trading/infrastructure/backend/docker-compose.prod.yml` sets
  `APP_ENV=production` (unchanged since Phase 15D.9's own read);
  `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH` are expected to come
  from the external, untracked `/etc/centralized-algo/backend.env` file
  — still unverifiable from this repository, still an operational
  prerequisite (Section 19), not a code gap.
- `file_permissions.py`: `0600` hardening wired into all 4 safety
  stores + kill-switch JSON (re-confirmed present this phase).
- `/api/health` / `/api/ready`: bounded, read-only broker-readiness
  probe (Phase 15D.8) and clock-drift diagnostic (`not_checked` by
  default — no reference clock wired yet, unchanged limitation).
- Restart behavior: `docker-compose.prod.yml`'s containers run
  `restart: unless-stopped`; the backend systemd unit does `git pull
  --ff-only` then `docker compose up -d --build` at boot.
- Backup: `trading/tools/backup_safety_stores.py` exists, confirmed
  (Phase 15D.8's own read agent) not wired into any scheduled job —
  manual-only, unchanged.
- Deployment identity: `get_deployment_info()` (app_version/git_sha/
  deployment_id/environment), logged at startup, explicitly never a
  trading-authorization signal (unchanged, re-confirmed by its own
  module docstring).
- Logging: `trading/common/logger.py` (JSON handler), `log_shipper.py`,
  `observability.py`, `heartbeat.py` — present, not re-audited in depth
  this phase (same limitation named in Phase 15D.9's own report).
- Process isolation: `Dockerfile` runs as non-root `appuser`; the three
  legacy algo processes run as separate, independently-managed systemd
  services (`centralized-algo-strategy@.service`), entirely separate
  OS processes from the FastAPI control-center container.

## 15. Failure-scenario analysis

| # | Scenario | Expected behavior | Current implementation | Verdict |
|---|---|---|---|---|
| 1 | Operator authentication fails | Denied before any authorization step | `AuthorizationService.evaluate()` requires `is_authenticated`; `UNAUTHENTICATED`/`INVALID` both fail | PASS |
| 2 | Authorization missing | Execution refuses | `execute()`'s live-authorization gate requires `intent.metadata["authorization_id"]`, fails if absent | PASS |
| 3 | Authorization expires | Consumption refused | `try_consume()` checks `is_expired()`, transitions to `EXPIRED`, raises | PASS |
| 4 | Authorization already consumed | Second consumption refused | Atomic CAS on `status='AUTHORIZED'` | PASS |
| 5 | Account changes (reassigned strategy) | Execution refused | `try_consume()`'s `account_id` exact-match; proven via Phase 15D.6/15D.7's own reassignment tests | PASS |
| 6 | Instrument changes | Execution refused | Exact-match on `symbol` | PASS |
| 7 | Quantity changes | Execution refused | Exact-match on `quantity` | PASS |
| 8 | Side changes | Execution refused | Exact-match on `side` | PASS |
| 9 | Idempotency key reused | Replay, not re-execution | Idempotency-store replay check (step 1), returns cached result | PASS |
| 10 | Kill switch engaged | Execution refused, unconditionally | Step 0, before anything else | PASS |
| 11 | RiskManager rejects | Execution refused, no broker call | Step 3, before broker resolution | PASS |
| 12 | Broker rejects (clean, no order_id) | Terminal `STATUS_REJECTED`, no silent loss | `ConfirmedRejectionError` → `_handle_confirmed_rejection()` (Phase 15D.3-R) | PASS |
| 13 | Broker returns ambiguous response | Terminal `STATUS_AMBIGUOUS`, never auto-retried | `AmbiguousOrderStateError` path, requires manual reconciliation | PASS |
| 14 | Network times out | Treated as ambiguous, not silently retried as if failed | Covered by the same ambiguous-response path (timeout represented as `None`/exception) | PASS |
| 15 | Process crashes before broker response | Idempotency key remains claimed/pending; no silent resume as success | `STATUS_PENDING`, reconciliation required on restart | PASS |
| 16 | Process crashes after broker accepts order | Order recorded before crash if the accept response was received; else ambiguous | Depends on exactly when the crash occurs relative to the response — covered by the same PENDING/AMBIGUOUS paths, verified in Phase 15D-DR's suite | PASS |
| 17 | Audit write fails | Execution result unaffected; failure logged separately | `ObservabilityHealth`/`_obs()` wrapping (Phase 14.6 Blocker C) | PASS |
| 18 | Reconciliation finds no order | Classified NOT_FOUND, not silently assumed filled or cancelled | `ReconciliationStatus`, tested | PASS |
| 19 | Reconciliation finds partial fill | Classified PARTIAL, never treated as COMPLETE | Explicit test in `test_phase_15d_recon.py` | PASS |
| 20 | Reconciliation finds unexpected order | Not directly attributed automatically (this is exactly how `260917000523943` stayed unattributed) | Confirmed by direct historical-record read every phase this session | PASS |
| 21 | Application restarts | Kill switch/audit/idempotency/live-authorization all durable if their persistence paths are configured | Confirmed by direct code read + Phase 15D-DR/15D.5/15D.6 restart tests | PASS, **conditional on the operational prerequisite that those paths are actually configured in the real deployment** (Section 14) |
| 22 | Database is unavailable | `/api/health` reports `degraded`/503, never crashes; safety stores each independently fail on their own connection attempt, not silently treated as empty | `_check_database()` never raises; SQLite stores raise on a genuine I/O error rather than returning a false "no record" | PASS |

No GAP was found in this pass — every scenario resolves to an existing,
already-tested behavior. This does not mean zero risk; it means no
*additional* code-level gap was discovered beyond what Sections 5 and 14
already name as operational (not code) prerequisites.

## 16. Historical canary integrity

Re-verified by direct read this phase:

```
260917000350205 -> status=COMPLETED (unchanged)
AG7002 record (phase15d2-canary-NIFTY22SEP2623600CE...) -> status=PENDING (unchanged)
260917000523943 -> no idempotency record exists for it (still unattributed)
trading/phase15d2_audit.db -> verify() = True, 14 records (unchanged)
```

## 17. Test results

Targeted suites re-run this phase (all against fake/recording brokers,
zero real broker mutation):

- `tests/common/test_phase_15d_3r_rejection_handling.py`,
  `tests/algos/test_angel_creds_env_isolation.py`,
  `tests/preflight/test_environment_isolation.py`
- `tests/common/test_phase_15d_5_live_authorization.py`
- `tests/common/test_phase_15d_6_live_authorization_workflow.py`
- `tests/test_phase_15d_7_operator_authorization.py`
- `tests/test_phase_15d_8_production_hardening.py`
- `tests/test_phase_15d_9_readiness_gate_closure.py`
- `tests/algos/test_doublestraddel_execution_bridge.py`

Full regression: `python -m pytest -q --tb=line > file.txt 2>&1; echo "EXIT=$?" >> file.txt`
(redirect-based, exit code captured directly, never piped through `tail`).

**Result: `EXIT=0`.** Progress-line character tally: **1652 result
characters — 0 `F`, 0 `x`, 6 `s` (the same pre-existing, documented
POSIX-only skips carried from Phase 15D.8), 1 `E`** — confirmed to be
only the leading letter of the `EXIT=0` marker line itself (0 explicit
`FAILED`/`ERROR` lines found by direct grep). Identical result count to
Phase 15D.9's own full regression, consistent with this being a
read-only phase that added no new test files to the main suite (the
per-phase targeted suites listed in Step 15 items 2–8 were also re-run
separately, together, in one combined pass: all passed, `EXIT=0`).

## 18. Blockers

**None found.** Every control this phase re-verified (Sections 6–15)
resolves to PASS or a previously-named, non-blocking limitation. No new
architectural bypass, no reordered gate, no weakened check was
discovered.

## 19. Prerequisites

### MUST VERIFY BEFORE CANARY (operational, not code)

1. Live, real-credential, read-only re-verification of Account A =
   READ_ONLY, Account B = READ_ONLY/FLAT, immediately before any
   authorization — this report could **not** perform this (Section 13:
   `READ-ONLY VERIFICATION UNAVAILABLE` in this environment).
2. `KILL_SWITCH_PERSISTENCE_PATH` and `AUDIT_DB_PATH` confirmed actually
   set and writable on the real production host (the external
   `backend.env` file this repository cannot inspect).
3. The three legacy algo processes confirmed `STOPPED` or `DRY_RUN` for
   the entire canary window, via `trading/common/legacy_algo_readiness.py`'s
   `check_legacy_algo_state()` — a `RUNNING`/`UNKNOWN` result for any of
   the three must block.
4. No `LiveAuthorization` currently outstanding (PENDING or AUTHORIZED)
   — confirmed clean as of this phase (no real `*live_auth*.db` exists
   anywhere).
5. No open position on Account B, confirmed by read-only reconciliation
   immediately before authorization (subject to prerequisite 1's
   availability).
6. Full regression re-run same-day, `EXIT=0`, `0 failed`, `0 errors`.

### ACCEPTED LIMITATIONS (do not block)

- Legacy algos lack RiskManager/LiveAuthorization/idempotency (only the
  kill switch reaches them, per Phase 15D.9) — accepted because the
  15D.10 canary itself goes through the new path, not these scripts;
  mitigated operationally by prerequisite 3.
- `Vwap_Algo_Nifty_hedge` has no dry-run gate of its own.
- Reconciliation is order-level only (no position corroboration).
- Clock-drift diagnostic has no reference clock wired (`not_checked`).
- Backup/restore is manual-only.
- `modify_order()` exists outside the `BrokerClient` interface on three
  adapters but has zero current callers beyond the deliberate read-only
  validation harnesses.
- Monitoring/alerting not re-audited in depth this phase.

### OUT OF SCOPE (named per this phase's own instruction, unchanged from Phase 15D.8/15D.9)

New broker adapters; new strategies; UI/frontend work; strategy
optimization; automated rollback; secrets-manager integration; CI/CD
pipeline; `TradingAccount` persistent authorization-state redesign;
any change to deployed AWS infrastructure.

## 20. Decision matrix

| Area | Current State | Evidence | Blocker? |
|---|---|---|---|
| Operator authentication | Implemented, JWT + fake providers | Phase 15D.7 suite (55 tests), re-run this phase | NO |
| Authorization | Implemented, exact-scope, single-use, expiring | Phase 15D.5/15D.6/15D.7 suites, re-run this phase | NO |
| Kill switch | Implemented, fail-closed, unconditional | `kill_switch.py` re-read; DR suite re-run | NO |
| RiskManager | Implemented, LIVE requires `is_live_ready()` | `execution.py:784` re-confirmed | NO |
| LiveCanaryGuard | Implemented, 10 checks, no defaultable limits | `live_canary.py` re-read this phase | NO |
| Idempotency | Implemented, persistent, atomic | `idempotency_store.py` re-read; DR suite re-run | NO |
| Broker response handling | Implemented, unconditional | `broker_response_validation.py`, engine step 6 | NO |
| Reconciliation | Implemented (order-level); position-level absent | `test_phase_15d_recon.py` re-run | GAP (named, non-blocking) |
| Audit | Implemented, hash-chained, append-only | `verify()` = `True` this phase | NO |
| Account isolation | Implemented, `owner_id` enforced | Phase 15D.7 cases 10-12 | NO |
| Credential isolation | Implemented, exact-match at 2 layers | Phase 15D.5/15D.6/15D.7 | NO |
| Legacy algorithms | Kill-switch-gated only; no full chain | Phase 15D.9 remediation, re-confirmed this phase | GAP (named, non-blocking per Section 19) |
| Production guard | Fixed (APP_ENV recognized), env vars must still be set externally | Phase 15D.9 fix + tests, re-run this phase | NOT VERIFIED (external file) |
| Environment isolation | Implemented | Phase 15D.3-R suite, re-run this phase | NO |
| Deployment persistence | Code-correct; depends on external config | `execution_state.py` re-read | NOT VERIFIED (external file) |
| Legacy process readiness | Implemented, conservative, read-only | `legacy_algo_readiness.py` re-read this phase | NOT VERIFIED (needs live DB state, not available here) |
| Historical integrity | Unchanged | Direct read this phase | NO |

## 21. Proposed implementation plan

**NO CODE IMPLEMENTATION REQUIRED FOR THE NEXT GATE.**

This is not authorization to perform a live order. Every remaining item
in Section 19's "MUST VERIFY BEFORE CANARY" list is operational
(reading live state, confirming external configuration, confirming
process status) — none require a code change. If any of those checks,
performed live by a human with real credentials, reveal a problem (an
open position, a running legacy algo, a missing production env var),
that finding would then define a genuine new blocker requiring its own
scoped fix — not something this report can pre-empt without the actual
live data.

## 22. Explicit human decision gate

This report does not authorize, and cannot substitute for, the
following, all of which remain exclusively a human decision:

- Whether to perform the live, real-credential account-state
  verification this report could not perform.
- Whether to confirm the three legacy algo processes are stopped/dry-run
  for a canary window.
- Whether to actually request, confirm, and authorize a specific,
  scoped `LiveAuthorization` for a 15D.10 canary transaction.
- Whether the external production configuration (`backend.env`) is
  correctly set.

---

## External Read-Only Verification

**Performed:** 2026-09-18. **Environment:** local development checkout
with real Angel One credentials present in `trading/.env` (previously
used for the Phase 15D.1/15D.2 canary sessions this same engagement).
This section supersedes this report's earlier Section 13, which
incorrectly concluded no real credentials were available — a shell
`env` check does not see values sourced from a `.env` file, which this
project's own credential-loading convention (`_load_dotenv_if_present()`,
already used by `trading/tools/angelone_readonly_validation.py`) reads
separately. All checks below reused that exact, already-audited
`AngelOneBroker(config, read_only=True)` safety pattern (mutation
methods raise before any SmartAPI call, unconditionally, regardless of
`TRADING_MODE`) via a scratchpad-only script (never added to the
repository) that called **only** `connect()`, `get_funds()`,
`get_positions()`, `get_order_book()`, and `get_open_orders()` —
zero calls to `place_order`/`modify_order`/`cancel_order` anywhere.
`broker.is_read_only` was asserted `True` before every call and
reconfirmed `True` after.

### Account A

| Field | Value |
|---|---|
| Credential prefix | `ANGELONE_A_*` (masked: api_key len=8, client_id len=10, mpin len=4, totp_secret len=26 — no values printed or logged) |
| Authentication | **PASS** — session established |
| Available cash | 100.0 |
| Used margin | 0.0 |
| Positions | 0 |
| Order book | 0 orders |
| Open orders (dedicated endpoint) | `FAIL (BrokerRateLimitError)` — transient SmartAPI throttling, not a security or logic failure |
| `is_read_only` after all calls | `True` |

**Account A = READ_ONLY (in the operational sense: minimal funds, zero
positions, zero orders, no mutation attempted or possible).**

### Account B

| Field | Value |
|---|---|
| Credential prefix | `ANGELONE_B_*` (masked: api_key len=8, client_id len=10, mpin len=4, totp_secret len=26) |
| Authentication | **PASS** — session established |
| Available cash | 2949.6525 |
| Used margin | 101.5475 |
| Positions | 2 returned, **both `quantity=0`** (`NIFTY22SEP2622350PE`, `NIFTY22SEP2624350CE` — neither is the historical canary strike `NIFTY22SEP2623550CE`, which no longer appears at all, consistent with it having been fully closed) |
| Order book | 4 orders, **all `status=COMPLETE`, `remaining=0`** |
| Open orders (dedicated endpoint) | `FAIL (BrokerRateLimitError)` on two attempts, 15s apart — the API's own rate limiter, not this tool retrying excessively; not retried a third time per this phase's "do not hammer the API" instruction |
| `is_read_only` after all calls | `True` |

**Account B = FLAT.** Although the dedicated "Open Orders" endpoint
itself was rate-limited both times it was tried, this is not treated as
an unresolved gap: the **independent** evidence already obtained —
every returned position at `quantity=0` and every returned order at
`status=COMPLETE`/`remaining=0` — positively confirms zero open exposure
and zero pending orders without needing that specific endpoint to
succeed. No order was placed to test this, per the hard rule.

### What this verification could NOT reach (a different resource, not the same gap)

Steps 3–7 of this phase's brief (legacy strategy process state, the
*actual* production environment variable resolution, safety-persistence
file existence, file permissions, and kill-switch state — all **on the
real deployed AWS host**) remain **UNVERIFIABLE from this environment**,
for a reason distinct from Section 13's original (now-corrected)
account-credential gap: this session has broker API credentials, but no
SSH/console/filesystem access to the production EC2 host itself. No
amount of additional broker read-only calls closes this — it requires a
human (or an automation this session has no access to) with actual
access to that host to run the equivalent of `systemctl status
centralized-algo-strategy@*`, inspect `/etc/centralized-algo/backend.env`,
and `ls -l` the configured `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH`
files there.

### Local, repository-level re-verification (Steps 8–14, all available without host access)

Re-confirmed fresh, directly, this phase:

```
260917000350205 -> status=COMPLETED (unchanged)
AG7002 record (phase15d2-canary-NIFTY22SEP2623600CE...) -> status=PENDING (unchanged)
260917000523943 -> no idempotency record exists for it; 0 audit-event references found (still unattributed to TCC)
trading/phase15d2_audit.db -> verify() = True, 14 records (unchanged)
No *live_auth*.db anywhere outside a test tmp_path (no active LiveAuthorization)
```

Account isolation: `ANGELONE_A_*` and `ANGELONE_B_*` are structurally
distinct env-var namespaces resolved via `resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_A"/"env:ANGELONE_B")`
— confirmed by the two independent, differently-scoped sessions
established above, each authenticating separately with its own
credential set. No strategy execution occurred (no test in this phase's
verification touched `StrategyExecutionEngine`).

### Mutation / order count for this entire external-verification pass

```
Real broker mutation calls = 0
New orders placed = 0
Orders modified = 0
Orders cancelled = 0
Live authorizations created = 0
Strategies started = 0
```

## Final Verification Table

| Check | Result | Evidence |
|---|---|---|
| Account A authentication | PASS | Real read-only session established |
| Account A state | PASS | 100.0 cash, 0 positions, minimal-funds profile consistent with a non-trading account |
| Account A positions | PASS | 0 positions |
| Account B authentication | PASS | Real read-only session established |
| Account B state | PASS | 2949.65 cash, 101.55 used margin |
| Account B flat | PASS | 2 positions returned, both qty=0; 4 orders, all COMPLETE/remaining=0 |
| Legacy strategy states | NOT VERIFIED | Requires production host access this session does not have |
| Production environment | NOT VERIFIED | `APP_ENV` value on the real host is external, unverifiable from here (unchanged since Phase 15D.9) |
| Safety persistence | NOT VERIFIED | `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH` existence on the real host requires host access |
| File permissions | NOT VERIFIED | Same — requires host access |
| Kill switch | NOT VERIFIED | Real persisted production state requires host access; local dev has no configured persistence path |
| Historical canary | PASS | `260917000350205` = COMPLETED, re-read directly this phase |
| Manual external close | PASS | `260917000523943` has 0 idempotency/audit references |
| AG7002 historical record | PASS | Still `PENDING`, unchanged |
| Audit hash chain | PASS | `verify()` = `True`, 14 records |
| Account isolation | PASS | Distinct credential namespaces, two independently-authenticated sessions |
| Active live authorization | PASS | No `*live_auth*.db` exists anywhere real |
| Strategy execution state | NOT VERIFIED | Same as "legacy strategy states" — requires host access |
| Real broker mutations | **0** | Confirmed — scratchpad tool never called a mutating method |
| New orders | **0** | Confirmed |

---

## Production Host Read-Only Verification

**Performed:** 2026-09-18, immediately following the broker-account
verification above, once AWS CLI access (`arn:aws:iam::471112713822:user/trading-control-cli`)
and SSM connectivity to the production host were confirmed available in
this session (a capability this report's earlier drafts did not know it
had — the same class of oversight as the earlier `.env` miss, now
corrected). All checks used `aws ec2 describe-instances`,
`aws ssm describe-instance-information`, and `aws ssm send-command`
with `AWS-RunShellScript` documents containing **only** read-only shell
commands (`hostname`, `date`, `docker ps`, `systemctl list-units`,
`ps aux`, `crontab -l`, `env | grep` on a fixed allow-list of non-secret
key names, `curl` against `/api/health`/`/api/ready` only, `find`/`ls`,
`git log`, `docker inspect`). No command started, stopped, or restarted
any service; no file was created, written, or modified; no secret value
was printed at any point.

### Host identification

| Field | Value |
|---|---|
| Instance ID | `i-0f344752a1ca2811b` |
| Tag Name | `algo-backend` |
| Region | `ap-south-1` |
| State | `running` |
| Launched | `2026-08-31T10:32:54Z` |
| SSM agent | Online, last ping matched request time |

Two other EC2 instances exist in this AWS account (`Minakshi-Linux`,
`samir_linux`, both `t3.micro`) — **not inspected**, since they are not
tagged or named as part of this trading system's infrastructure and
inspecting them would be outside this verification's scope and an
unauthorized overreach into what appear to be unrelated personal
machines.

### ⚠️ Headline finding: the deployed backend predates this entire engagement's Phase 15D.5–15D.9 work

```
docker inspect app-backend-1 --format Created: 2026-09-09T04:14:53Z
curl http://localhost:8000/api/health -> {"status":"ok","service":"centralized-algo-backend",
    "timestamp":"...","database":"connected"}   <- NO app_version/git_sha/deployment_id/
                                                    environment/clock_drift fields at all
curl http://localhost:8000/api/ready -> 404 {"detail":"Not Found"}   <- route does not exist
```

The running production container's image was built **2026-09-09** — before
Phase 15D.5 (Human Authorization Canary Gate), 15D.6 (Controlled
Workflow), 15D.7 (Operator Authentication), 15D.8 (Production
Hardening), and 15D.9 (Blocker Closure) were ever written, all of which
happened in this session on 2026-09-17/18 in an **uncommitted local git
working tree that has never been pushed or deployed**. `/opt/centralized-algo`
on the host is not even a git checkout (`git log` → `fatal: not a git
repository`) — it contains only a baked `app/` directory from whatever
build produced that image.

**Concretely, none of the following exist in the currently running
production system**: `LiveAuthorization`/`LiveAuthorizationWorkflow`/
`AuthorizationService`/`OperatorIdentity`, the Phase 15D.8/15D.9
production-safety guard, `harden_file_permissions()`, the Phase 15D.9
legacy-algo kill-switch guard, the `/api/ready` endpoint, or any of the
`app_version`/`git_sha`/`clock_drift` health-endpoint fields. The
container's own environment contains **only** `APP_ENV=production` —
no `KILL_SWITCH_PERSISTENCE_PATH`, `AUDIT_DB_PATH`, or any other safety
env var. A filesystem-wide search (`find / -xdev`) for any file matching
kill-switch/audit/idempotency/reconciliation/live-authorization naming
patterns found **zero matches** anywhere on the host.

This is not a configuration oversight this report can classify as a
mere "NOT VERIFIED" gap — it is a **positively confirmed, actively wrong
safety condition**: this whole engagement's safety framework is real,
tested, and correct in the local repository, but it is not the code
running in production. Any live authorization or canary decision made
today under the belief that the deployed system enforces this
engagement's safety chain would be **factually incorrect**.

### Legacy algorithm process state (on this host)

```
systemctl list-units --type=service --all | grep -i centralized-algo
  -> only centralized-algo-backend.service (loaded, active, exited -- expected for its oneshot type)
  -> ZERO centralized-algo-strategy@*.service units loaded, for any of the three algo names
docker ps -a -> only app-backend-1, app-postgres-1 (no algo-specific container, running or stopped)
ps aux | grep -iE "DoubleStraddel|CombinedVwap|Vwap_Algo|python.*main.py" -> no matches
crontab -l -> only a healthcheck script, once per minute; /etc/cron.d/ has nothing else
```

**All three legacy algos: STOPPED**, on this host, by every method this
verification could check (systemd, Docker, raw process list, cron). This
verification cannot rule out one of the three being run manually,
interactively, from an unmanaged location (a developer's own machine) —
that is structurally unobservable from the production host itself; it
can only report what the production host shows, which is nothing running.

### Autostart protection

`centralized-algo-strategy@.service` templates exist in the repository
(`trading/infrastructure/strategy/systemd/`) but are **not instantiated**
on this host (confirmed above — `systemctl list-units --all` would show
a loaded-but-inactive unit if one had ever been enabled; none exist at
all). No cron entry references any algo. **No autostart path found for
any legacy algo on this host.**

### Kill-switch / safety-persistence state

No `KILL_SWITCH_PERSISTENCE_PATH` is configured in the running
container, so there is no persisted state to read — the deployed
(pre-15D.8) code's kill switch, if it exists in that older version at
all, is in-memory-only and implicitly disengaged, exactly like every
pre-Phase-15D-AUDIT deployment. This is consistent with, not
contradictory to, the "no safety files found" result above.

## Final Host Verification Table

| Production-host check | Result | Evidence |
|---|---|---|
| Actual production host identified | PASS | `i-0f344752a1ca2811b`, `algo-backend`, running |
| `APP_ENV` | PASS | `APP_ENV=production` confirmed in the running container |
| `resolve_environment()` | **BLOCKED** | This function does not exist in the deployed code (image predates Phase 15D.9) |
| Production guard configuration | **BLOCKED** | `production_guard.py` does not exist in the deployed code at all |
| Kill-switch store exists | **BLOCKED** | No file, no configured path |
| Audit store exists | **BLOCKED** | No file, no configured path (deployed `/api/health` response has no `PersistentAuditTrail` wiring evident) |
| Idempotency store exists | **BLOCKED** | No file found |
| Reconciliation store exists | **BLOCKED** | No file found |
| LiveAuthorization store exists | **BLOCKED** | No file found; module does not exist in deployed code |
| File permissions | N/A | No files exist to check permissions on |
| Persisted kill-switch state | **BLOCKED** | No persistence configured; deployed code likely predates the persistence feature entirely |
| DoubleStraddelAlgo state | PASS | STOPPED on this host (systemd/docker/process/cron all clean) |
| CombinedVwapNifty state | PASS | STOPPED on this host |
| Vwap_Algo_Nifty_hedge state | PASS | STOPPED on this host |
| Legacy autostart protection | PASS | No systemd unit instantiated, no cron entry |
| TCC service state | PASS (as a process) / **BLOCKED** (as a safety-compliant deployment) | Backend container up and healthy, but running code that predates the entire safety framework |
| `/api/health` | PASS (endpoint) / **BLOCKED** (as evidence of currency) | 200 OK, but response shape confirms a pre-15D-DR build |
| `/api/ready` | **BLOCKED** | 404 — endpoint does not exist in the deployed code |
| Active LiveAuthorization | N/A | Concept does not exist in the deployed code; also confirmed absent locally |
| Historical canary integrity | PASS | Verified from the local repository's own persistence (`trading/phase15d2_*.db`) — the historical canary was executed via local preflight tooling directly against the broker, not through this production backend, so its record does not live on this host |
| Audit hash chain | PASS | Same — local repository's `trading/phase15d2_audit.db`, `verify()` = `True`, unchanged |

## Conclusion

Every prerequisite this report could actually test **on the real,
credentialed broker side** passed (Account A/B verification, historical
integrity, audit chain — all reconfirmed above and in the prior
section). The account-level and historical-record gaps this report
previously could not close are now closed.

But the production-host verification this phase was specifically
commissioned to perform surfaced a **genuine, actively wrong safety
condition**, not a mere evidence gap: the real, running production
backend is built from code that predates this entire engagement's
authorization/authentication/production-hardening work by more than a
week. Every one of Phases 15D.5 through 15D.9's controls — the exact
things a human would rely on to believe a "controlled canary" is safe —
exists only in this local, uncommitted git working directory. **A
canary authorized today, under the belief that the production system
enforces this safety chain, would not actually be protected by any of
it**, because that system does not have this code.

```
PHASE 15D.10 EXTERNAL READ-ONLY VERIFICATION = BLOCKED

SAFETY CONDITION REQUIRES REMEDIATION.
NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
NO REAL BROKER MUTATION PERFORMED.
HARD STOP.
```

**The required remediation is a deployment, not a further verification
pass**: the local repository's committed state (Phases 15D.5–15D.9, all
currently uncommitted in this working tree) must actually be committed,
and then deployed to `i-0f344752a1ca2811b` through this project's own
existing, unmodified deployment path (`git pull --ff-only` +
`docker compose up -d --build`, per the systemd unit's own documented
behavior), before any human can honestly authorize a live canary against
"the system this engagement built." This report does not perform that
deployment — deploying to production is exactly the kind of
hard-to-reverse, shared-system action explicitly out of scope for a
read-only verification phase, and remains a separate, explicit human
decision.

The account-level (broker-side) prerequisites are now satisfied. The
remaining five checks are a host-access prerequisite, not a code or
architecture blocker — closing them requires a human with production
access to run the equivalent read-only checks there (e.g. `systemctl
status` on the three legacy services, `echo $APP_ENV` /
`cat backend.env` presence check, `ls -l` on the configured persistence
paths, and a direct read of the kill-switch JSON file), not any further
work in this repository.

This is not a claim that the system is unsafe — it is a claim that this
particular READ+PLAN pass, run from an environment with no live broker
access, cannot itself complete the account-state and operational checks
Section 19 lists. A human with real, read-only broker credentials
performing exactly those checks (Section 19's numbered list) — with no
code change required first — is the direct next step toward a
READY-FOR-REVIEW determination.
