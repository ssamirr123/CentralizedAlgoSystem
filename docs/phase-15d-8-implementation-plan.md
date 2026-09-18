# Phase 15D.8 — Implementation Plan: Production Deployment & Security Hardening

**Status: PLAN — awaiting go-ahead before implementation.**

Guiding question: *"If this system is deployed as a production service,
can we safely start, stop, restart, recover, monitor, and operate it
without weakening the live-trading safety boundary?"*

This phase hardens; it does not judge readiness (that is 15D.9's job,
run independently afterward) and it does not place any order, grant any
live authorization, or touch any historical record.

---

## 1. What already exists (read-only inventory, this session)

The repository is **not** greenfield for this phase — a fair amount of
production-deployment machinery already exists:

- **Config**: `trading/common/config.py` (`TradingConfig`/`BrokerCredentials`,
  env-var driven, `TRADING_MODE` defaults to `"paper"`), `trading/core/config.py`
  (`load_settings()`), `trading/common/deployment_info.py` (`ENVIRONMENT`/`ENV`,
  defaults to `"development"`).
- **Credentials**: `trading/common/credentials.py::resolve_credentials()` —
  `env:<PREFIX>` scheme only; its own docstring already flags a future
  `secretsmanager:` scheme as "intentionally not stubbed out."
- **Database**: `trading/database/connection.py` — Postgres via
  `DATABASE_URL` is the documented production database; SQLite is the dev
  fallback; Alembic runs migrations (`docker/entrypoint.sh`). Separate
  flat-file SQLite stores for idempotency/audit/kill-switch/reconciliation
  are **opt-in** via `AUDIT_DB_PATH` / `KILL_SWITCH_PERSISTENCE_PATH` env
  vars in `trading/api/execution_state.py` — unset by default.
- **Startup**: `trading/api/app.py::lifespan()` — startup banner, `DEPLOYMENT_STARTUP`/
  `DEPLOYMENT_SHUTDOWN` audit events, `init_db()`, `bootstrap_admin()`,
  background watchers.
- **Health/readiness**: `trading/api/health.py` — `/api/health` (DB
  reachability) and `/api/ready` (`trading_authorized` hardcoded `False`,
  `broker` hardcoded `"unknown"` — a permanent stub).
- **Kill switch**: `trading/common/kill_switch.py::CentralKillSwitch` —
  already fails closed on a corrupt persistence file (loads
  `engaged=True`), but the persistence path itself is opt-in, unenforced.
- **Deployment artifacts**: a real `Dockerfile` (non-root `appuser`),
  `docker-compose.yml` + `docker-compose.prod.yml`, systemd units
  (backend + per-strategy + watchdog), an AWS infra tree
  (`trading/infrastructure/`: IAM, Lambda orchestrator, nginx, CloudWatch),
  and `.github/workflows/secret-scan.yml` (gitleaks, the only CI workflow).
- **Broker/IP**: `trading/validation/angel_readonly.py` — the read-only
  guard proven in earlier phases; no explicit IP-allowlist code distinct
  from that guard was found.
- **Missing entirely**: time-sync/clock-drift handling; file-permission
  hardening (`chmod`/`0o600`) on any DB or secrets file; a build/test CI
  pipeline (only secret-scan exists); backup/rotation tooling for any
  safety-critical store.
- **Stale prior art**: `docs/phase-15d-deployment-recovery-safety-report.md`'s
  "no CI/CD or deployment tooling exists" statement is now **outdated** —
  the Dockerfile/compose/systemd/infra tree postdates that report. This
  plan reconciles against what exists rather than assuming a blank slate.

## 2. Scope decision — what 15D.8 actually implements

Given the breadth of the 13-area brief and this phase's own instruction
not to overlap with 15D.9, I'm proposing a **prioritized subset**,
ordered by how directly each gap threatens the live-trading safety
boundary specifically (not general production polish):

### In scope (implement this phase)

1. **Enforce kill-switch and audit persistence in production** — the
   single highest-severity gap found: today, a production deploy that
   forgets to set `KILL_SWITCH_PERSISTENCE_PATH` silently runs with an
   **in-memory kill switch that resets to disengaged on every restart**,
   and forgetting `AUDIT_DB_PATH` silently loses the audit trail on
   restart. Add a startup check (in `trading/api/app.py::lifespan()` or a
   new `trading/common/production_guard.py`) that, when
   `ENVIRONMENT`/`ENV` indicates production, refuses to start (fails
   closed, loud, at boot — not silently) unless both paths are set to a
   real, writable location. Never touches `execute()`'s own gates; this
   is a startup-time check, not a new runtime gate.
2. **Wire a real broker-connectivity signal into `/api/ready`** —
   replace the hardcoded `"unknown"` with an actual read-only
   connectivity probe (reusing `ReadOnlyBrokerView`/existing broker
   adapters, never placing an order), so an orchestrator can actually
   tell whether the broker side is reachable before restarting/routing
   traffic.
3. **File-permission hardening** — on process startup, set restrictive
   permissions (owner-only) on every safety-critical SQLite file and any
   kill-switch JSON file this process creates or opens, on platforms
   where that's meaningful (POSIX `0o600`; a documented no-op on
   Windows dev, since NTFS ACLs don't map to the same call — this
   matters for the EC2/Linux production target, not Windows dev).
4. **Time-sync / clock-drift check** — a new, read-only diagnostic
   (`trading/common/time_sync.py`) that compares local system time
   against a small set of reliable time sources (or, at minimum,
   confirms `datetime.now(timezone.utc)` is being used consistently and
   flags gross drift if a reference is configured) — surfaced via
   `/api/health` as an additional field, never blocking startup by
   itself (drift is a warning signal an operator acts on, not an
   automatic kill-switch trigger — that decision is explicitly left to a
   human, consistent with every other phase's "no automatic live-trading
   decisions" precedent).
5. **Backup/snapshot tooling for the safety-critical stores** — a
   small, explicit script (not automatic/scheduled within this phase)
   that snapshots the idempotency/audit/kill-switch/reconciliation
   SQLite files to a timestamped copy, plus a documented restore
   procedure. Scheduling it (cron/systemd timer) is noted as a
   deployment-config recommendation in the report, not code this phase
   ships and enables by default.
6. **Rollback procedure documentation** — a runbook section (in the
   phase report, referencing the existing `docker-compose.prod.yml` /
   systemd units) describing how to roll back a bad deploy: redeploy the
   previous image tag / `git checkout` to the previous release commit +
   `alembic downgrade`, confirm kill-switch/account state survived
   (per item 1), re-run the health/ready checks. Documentation only —
   this phase does not build new rollback automation.
7. **Reconcile the stale 15D-DR claim** — update
   `docs/phase-15d-deployment-recovery-safety-report.md` is explicitly
   **not** modified (historical report, left as-is per this project's
   own "never rewrite a prior phase's report" precedent); instead this
   phase's own report notes the reconciliation explicitly, so the
   record of what changed and when stays accurate without altering
   history.

### Explicitly out of scope for 15D.8 (named, not silently dropped)

- **Secrets-manager integration** (AWS Secrets Manager/SSM). This is the
  single largest gap found, but wiring real cloud secrets infrastructure
  is a substantial, separate change with real infra/deployment
  consequences (new IAM permissions, a new runtime dependency, credential
  rotation semantics) that deserves its own tightly-scoped phase, not a
  subsection of a broader hardening pass. This plan documents the gap and
  proposes the abstraction shape (a `secretsmanager:` credential-reference
  scheme already anticipated by `credentials.py`'s own docstring) for a
  future phase to implement.
- **`TradingAccount`/`AccountAuthorizationState` persistent store.**
  Flagged since Phase 15D-DR. Today it resets to its constructed default
  (`READ_ONLY`) on restart, which is the *safe* direction (fails closed,
  never silently re-arms a live-authorized account) — so this is a
  real gap but not one that weakens the safety boundary in the dangerous
  direction. Deferred to a future phase focused specifically on account
  state persistence, to avoid this phase quietly touching the account
  authorization model.
- **A full build/test CI pipeline.** Valuable, but orthogonal to the
  live-trading safety boundary this phase is scoped to, and a
  meaningfully separate piece of engineering work (test matrix, runtime
  selection, secrets for CI itself). Named here so it isn't lost, not
  built now.
- **Any change to `trading/infrastructure/`'s actual AWS resources**
  (IAM policies, EC2, Lambda). This phase only reads and reports on that
  tree; it does not modify live cloud infrastructure or credentials.

## 3. Hard safety rules for this phase (restated from the required
   end-state, made explicit and enumerated per this project's own
   precedent)

1. Do not place any real broker order.
2. Do not call any real broker mutation endpoint.
3. Do not create a real LiveAuthorization in a production/live database.
4. Do not transition Account A or Account B out of READ_ONLY /
   READ_ONLY-FLAT.
5. Do not start any strategy.
6. Do not weaken, reorder, or bypass any existing safety gate (kill
   switch, RiskManager, LiveCanaryGuard, idempotency, LiveAuthorization,
   the Phase 15D.7 operator-authorization boundary).
7. Do not modify historical audit/order/idempotency/reconciliation
   records, or any prior phase's report file.
8. Do not modify live AWS infrastructure (IAM, EC2, Lambda) — read-only
   with respect to `trading/infrastructure/`'s actual deployed resources.
9. All new/changed tests use fake/mock brokers and temp databases —
   never a real credential, never a real network call.
10. Any ambiguity fails closed.
11. Final state: `Real broker mutation calls = 0`, `Real live orders = 0`,
    `Live authorization = 0`, `Account A = READ_ONLY`,
    `Account B = READ_ONLY / FLAT`, `Strategies = STOPPED`.

## 4. Test strategy

- `tests/test_phase_15d_8_production_hardening.py` covering: the
  production-guard startup check (missing path → refuses to start in
  "production" env; present → starts; "development"/"docker" env →
  unaffected, zero behavior change for every existing test/dev run),
  the `/api/ready` broker-connectivity signal (reachable fake broker →
  `True`; unreachable/raising fake broker → `False`, never raises),
  the time-sync diagnostic (no reference configured → reports
  "not checked", never blocks; configured with a mocked clock → detects
  drift correctly), file-permission hardening (created file's mode bits
  on a POSIX-like check, skipped/no-op path documented for Windows), and
  the backup-snapshot script (produces a timestamped copy, restore round-trips).
- Full regression re-run with trustworthy exit-code capture, plus a
  targeted re-run of Phase 15D.4/15D.5/15D.6/15D.7's own suites (this
  phase touches `trading/api/app.py` and `trading/api/health.py`, so the
  existing `tests/api/test_health.py` and control-center API tests are
  re-verified explicitly).

## 5. Compatibility strategy

- The production-guard check only activates when `ENVIRONMENT`/`ENV`
  resolves to a production-like value; every existing dev/test/CI run
  (which never sets that) sees zero behavior change.
- `/api/ready`'s new broker check defaults to the same `"unknown"`
  behavior when no broker is configured (e.g., in a pure API-only test
  environment) — additive, not a breaking schema change to the endpoint.
- No changes to `execute()`, `RiskManager`, `LiveAuthorization`,
  `LiveAuthorizationWorkflow`, or the Phase 15D.7 operator-identity
  layer — this phase is deployment/ops surface only.

---

## Next step

Awaiting go-ahead to implement exactly this scoped design (items 1–7
under Section 2's "in scope" list). If a different prioritization is
preferred (e.g., secrets-manager integration should be pulled into this
phase after all, or the account-state persistence gap should be
addressed here), say so before I begin implementation.
