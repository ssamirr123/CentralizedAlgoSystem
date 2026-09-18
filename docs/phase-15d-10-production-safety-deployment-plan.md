# Phase 15D.10-R — Production Safety Deployment Remediation: READ + PLAN

**READ + PLAN + REPORT only. No commit, push, deployment, restart, environment change, database creation, or permission change was made to produce this document. No LiveAuthorization created, no order placed.**

---

## 1. Production/local divergence (restated from Phase 15D.10's own finding)

The running production backend (`i-0f344752a1ca2811b`, image built
2026-09-09) predates Phases 15D.5–15D.9 entirely: no `LiveAuthorization`/
`operator_identity`/`production_guard`/`legacy_execution_guard` modules
exist there, `/api/ready` 404s, and no safety-persistence env var is
set. This plan defines exactly what would need to happen to close that
gap — it does not perform any of it.

## 2. Current production deployment (verified from code this phase)

- **Image**: built from `Dockerfile` (repo root) — `python:3.11-slim`,
  non-root `appuser` (uid 10001), bakes `APP_ENV=docker` as an image
  default (overridden per-compose-file at runtime).
- **Entrypoint**: `docker/entrypoint.sh` — `alembic upgrade head`, then
  `exec uvicorn trading.api.app:create_app --factory`.
- **Compose**: `docker-compose.yml` (base: postgres + backend, dev
  defaults) overlaid with `trading/infrastructure/backend/docker-compose.prod.yml`
  (production overrides: `restart: unless-stopped`, `APP_ENV: production`,
  secrets via `env_file: /etc/centralized-algo/backend.env` — a
  root-owned, `chmod 600`, **not-in-this-repository** file).
- **Systemd**: `centralized-algo-backend.service` — `Type=oneshot`,
  `WorkingDirectory=/opt/centralized-algo/app`,
  `ExecStartPre=git -C /opt/centralized-algo/app pull --ff-only`,
  `ExecStart=docker compose ... up -d --build --remove-orphans`.

### ⚠️ New finding this phase: the documented deployment mechanism does not currently match the real host

Phase 15D.10's own host inspection found `/opt/centralized-algo/app`
contains only a baked `app/` directory (no `.git`) — `git log` there
returns `fatal: not a git repository`. The systemd unit's
`ExecStartPre` assumes a working git checkout with a configured remote.
**As the host stands today, running this systemd unit's `ExecStart`
would fail at the `git pull` step before ever reaching `docker compose`.**
This is a real, concrete gate item (Section 20) — a git checkout must
exist at `/opt/centralized-algo/app` with the correct remote configured
before the documented deployment path can work at all. This plan does
not diagnose why this drifted (a prior manual/ad hoc deployment is the
most likely explanation) or fix it — that is itself a decision requiring
human judgment about which mechanism to actually use going forward.

### ⚠️ Second new finding this phase: no persistent volume exists for the backend container

`docker-compose.yml`'s `backend` service declares **no volume mount** of
any kind (only `postgres` has one: `pgdata:/var/lib/postgresql/data`,
plus a bind-mount for it in the prod overlay). `docker-compose.prod.yml`
additionally gives the backend a `tmpfs` mount for `/app/logs`
(explicitly ephemeral, wiped every restart). **Any SQLite file the
backend writes to its own container filesystem is destroyed the next
time `docker compose up -d --build` recreates that container** — which
is exactly what this systemd unit's `ExecStart` does on every deploy,
including the one that would ship this safety framework. This is Step
7's `DEPLOYMENT BLOCKER` condition, triggered for real: **without adding
a persistent volume, deploying today's code would not make the safety
stores durable — they would be re-created empty on every subsequent
deploy**, defeating the entire purpose of Phase 15D.8's persistence
hardening. Section 7 below specifies the minimal fix.

### ⚠️ Third new finding this phase: no HTTP route exists to trigger a live authorization remotely

`trading/api/execution_routes.py` never constructs a
`StrategyExecutionEngine` and never imports `LiveAuthorizationWorkflow`
(confirmed by grep this phase — zero matches for
`StrategyExecutionEngine(` anywhere under `trading/api/`). The entire
Phase 15D.5–15D.9 chain is a Python library + script-level framework
(exercised by tests and by `trading/preflight/live_canary.py`-style
local scripts — the same mechanism that ran the actual historical
canary), **not** something the deployed FastAPI service currently
exposes as an endpoint. Deploying this code makes the *modules* available
on the host and makes `production_guard.py` protect the *process
itself* at startup — it does **not**, by itself, create a way to trigger
a canary through the web API. Any future canary would still be run the
same way the historical one was: a script executed with real credentials
against `StrategyExecutionEngine` directly (locally, or on the host via
SSM, never through a not-yet-built HTTP endpoint). This is a scoping
clarification, not a blocker — but it materially changes what
"deploying the safety framework" accomplishes, and this plan does not
propose building that HTTP surface (explicitly out of scope, unrequested).

## 3. Target commit

- **Current branch**: `feature/broker-agnostic-execution-framework`.
- **Current HEAD**: `7b2637b` — "feat(execution): Phases 15B-15D.1 -
  multi-account architecture, authorization gate, and live-canary
  preflight" (2026-09-15).
- **Working-tree status**: 19 modified + 46 untracked files, all
  uncommitted. Every file maps to Phase 15D.2 through 15D.10 work
  performed in this session — direct review of the full file list found
  **no unrelated change** (no dependency bump, no unrelated refactor, no
  stray formatting-only diff). The "target commit" does not exist yet:
  it would be a new commit on top of `7b2637b` containing exactly this
  working tree, once a human reviews and approves it.
- Nothing in this list was committed or pushed by this phase.

**Test results (Step 3 baseline check, re-run fresh this phase):**
`python -m pytest -q --tb=line > file.txt 2>&1; echo "EXIT=$?" >> file.txt`
→ **`EXIT=0`**. Progress-line tally: **1652 passed, 0 `F`, 0 `x`, 6 `s`
(the same documented POSIX-only skips carried since Phase 15D.8), 1 `E`**
(confirmed to be only the leading letter of the `EXIT=0` marker itself —
0 explicit `FAILED`/`ERROR` lines by direct grep). This is an **exact
match** to the previously validated baseline (1652 passed / 0 failed /
0 errors from Phase 15D.9/15D.10) — the working tree has not drifted
since that evidence was captured. **Gate 1 (Section 19) is satisfied.**

## 4. Target image

Would be built by the existing, unmodified `Dockerfile` (`docker build .`
via `docker compose ... up -d --build`) from the target commit above,
once committed. No `Dockerfile` change is required for the code itself
— only the compose-file volume addition (Section 7) and the
`backend.env` additions (Section 6).

## 5. Existing deployment mechanism (reused, not replaced)

`git pull --ff-only` (once the checkout is fixed — Section 2) →
`docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml up -d --build --remove-orphans`,
triggered by `systemctl restart centralized-algo-backend` (or the boot
path). This plan proposes **no new deployment system** — only the one
additive compose-file change in Section 7 and the `backend.env`
additions in Section 6.

## 6. Environment configuration

Verified from actual code (`trading/api/execution_state.py`,
`trading/common/production_guard.py`) — not invented:

| Variable | Required for | Currently in prod `backend.env`? |
|---|---|---|
| `APP_ENV=production` | `resolve_environment()` (Phase 15D.9) | **Yes** (confirmed live, in `docker-compose.prod.yml` itself, not `backend.env`) |
| `KILL_SWITCH_PERSISTENCE_PATH` | `production_guard.py` (mandatory once code is deployed); `build_execution_state()`'s kill switch | **No** (confirmed absent live) |
| `AUDIT_DB_PATH` | `production_guard.py` (mandatory); `build_execution_state()`'s audit trail | **No** (confirmed absent live) |

**Idempotency, reconciliation, and LiveAuthorization persistence have NO
environment-variable wiring in `execution_state.py` at all** — unlike
kill-switch/audit, these three stores are never constructed by
`build_execution_state()` today (Section 2's third finding). There is
therefore no existing env-var name to configure for them in the current
codebase; a future phase that actually wires
`StrategyExecutionEngine`/`LiveAuthorizationWorkflow` into the API layer
would need to add that wiring (and its own env vars) at that time — not
invented here.

No authentication configuration beyond what Stage 18 already provides
(`AUTH_SECRET_KEY`, etc., already presumably set in `backend.env` since
the existing login flow works today) is required for the code in this
working tree — `JwtClaimsAuthenticationProvider` is a bridge with no
route calling it yet (Phase 15D.7's own named limitation), so it needs
no new production secret.

## 7. Persistent storage design

**Required additive change** (specified here, not made): add one
bind-mounted volume to `trading/infrastructure/backend/docker-compose.prod.yml`'s
`backend` service, mirroring postgres's own existing pattern:

```yaml
  backend:
    volumes:
      - /var/lib/centralized-algo/safety-data:/app/data
```

| Store | Host path | Container path | Env var |
|---|---|---|---|
| Kill switch | `/var/lib/centralized-algo/safety-data/kill_switch.json` | `/app/data/kill_switch.json` | `KILL_SWITCH_PERSISTENCE_PATH` |
| Audit | `/var/lib/centralized-algo/safety-data/audit.db` | `/app/data/audit.db` | `AUDIT_DB_PATH` |
| Idempotency | *(not wired into the API today — Section 2)* | — | — |
| Reconciliation | *(not wired into the API today)* | — | — |
| LiveAuthorization | *(not wired into the API today)* | — | — |

Directory ownership: `/var/lib/centralized-algo/safety-data` on the
host must be created (once, manually, by whoever performs the eventual
deployment) and owned so that container UID 10001 (`appuser`) can write
to it — matching the existing `postgres` bind-mount's own precedent
(`/var/lib/centralized-algo/pgdata`, presumably already correctly owned
for the `postgres` image's own UID). File-level permissions inside that
directory are then self-enforced by `harden_file_permissions()`
(`0600`) the first time each store initializes — no manual `chmod` of
the individual files is needed, only the parent directory's ownership.

**Restart behavior**: a plain container *restart* (no rebuild) already
preserves anything on the container's own writable layer even without a
volume — the blocker is specifically *recreation* (`up -d --build`),
which every future code deployment performs. The volume above is what
makes safety state survive recreation, not just restart.

Backup location: `trading/tools/backup_safety_stores.py` (Section 15)
would snapshot from these same host paths.

## 8. Fresh-production database behavior

| Store | Classification |
|---|---|
| Kill switch | **NEW STORE** — no file exists anywhere on the host today (confirmed by filesystem-wide search, Phase 15D.10) |
| Audit | **NEW STORE** — same |
| Idempotency | **NEW STORE** (once wired — not applicable to this deployment, since it isn't constructed by the API layer at all yet) |
| Reconciliation | **NEW STORE** (same caveat) |
| LiveAuthorization | **NEW STORE** (same caveat) |

No migration is required for any of these — there is no existing
production data to migrate, confirmed by direct filesystem search this
session. The historical canary's own records
(`260917000350205`/AG7002/`260917000523943`) live in the **local
repository's** `trading/phase15d2_*.db` files, not on this host, and
are not part of this deployment's scope — they must not be copied to,
or recreated on, the production host as part of this remediation
(doing so would fabricate a production record for an order that was
never placed through this backend).

## 9. Permission design

Already self-enforcing (Section 7): each store's own constructor calls
`harden_file_permissions()` (`trading/common/file_permissions.py`,
POSIX `0600`) once, immediately after schema initialization/first
write — confirmed present in `audit_store.py`, `idempotency_store.py`,
`kill_switch.py`, `live_authorization.py`, `reconciliation.py` (grep
this phase, 5/5). The container runs as non-root `appuser` (uid 10001,
Dockerfile line 23) — the mounted directory's host-level ownership
(Section 7) is the only manual step; file-level permissions inside it
need no separate action.

## 10. Production guard

`trading/api/app.py::lifespan()` calls
`check_production_safety_paths()` as its first action (confirmed by
grep this phase — before `init_db()`, before the watcher). With
`APP_ENV=production` already set live and `resolve_environment()`
(Phase 15D.9) now checking `APP_ENV` first, the precedence chain
(`APP_ENV` → `ENVIRONMENT` → `ENV` → `"development"`) is exercised
correctly and cannot be silently bypassed by any of the three names —
this was itself proven by a dedicated regression test in Phase 15D.9
(`test_case_08_docker_compose_prod_style_configuration`) reproducing
this exact compose configuration. Once the target commit is deployed
with `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH` both set (Section
6/7), the application will **fail to start** if either path is missing
or unwritable — this is the intended, verified fail-closed behavior,
not a bug to work around.

## 11. Health/readiness

`/api/ready` 404s in production today because that route
(`trading/api/health.py`) does not exist in the deployed (pre-15D-DR)
code at all — not a routing misconfiguration. Deploying the target
commit adds the route automatically (it is registered in
`trading/api/app.py::create_app()` exactly like `/api/health`, no
separate wiring needed). The target commit's `/api/health` response
will additionally include `app_version`/`git_sha`/`deployment_id`/
`environment`/`clock_drift` (all present in the code today, confirmed
by this session's own Phase 15D.8 work); `/api/ready` will report
`application`, a bounded read-only `broker` signal (Phase 15D.8), and
`trading_authorized` (always `false`).

## 12. Legacy algorithm protection

The target commit includes `legacy_execution_guard.py` and
`legacy_algo_readiness.py`, and all six `placeOrder` call sites across
the three legacy algo files are already wired to the kill-switch guard
(Phase 15D.9, structurally regression-tested). These three algo scripts
are **not part of the backend Docker image at all** — they run as
separate, independently-deployed processes (their own systemd templates
under `trading/infrastructure/strategy/`), confirmed **not currently
instantiated** on the `algo-backend` host (Phase 15D.10's own finding:
zero matching systemd units, containers, or processes). This deployment
plan does not touch those three scripts' own deployment path at all —
it only ships the guard code that *they* would need to import if/when
they are ever deployed and run, from wherever they actually execute
(this host or elsewhere). **They must remain STOPPED throughout this
entire deployment** — nothing in this plan starts, enables, or
provisions them.

## 13. Operator authentication

No new production authentication configuration is required (Section
6). Stage 18's existing JWT/RBAC system (already live, since the
current login flow presumably already works against this deployment)
is what `JwtClaimsAuthenticationProvider` would eventually bridge to —
but since no route calls that bridge yet (Section 2's third finding),
there is nothing to configure for it today. **No deployment blocker
here** — this is a non-blocking, already-named limitation (a route
doesn't exist yet), not a missing credential.

## 14. Rollback

- **Current production image**: `sha256:18f541bd4be3552eb2701f8692145c88845148f3be5b02b7fa3d2103851a876e` (built 2026-09-09, confirmed live this session).
- **Target image**: not yet built (would be built from the approved
  target commit, Section 3).
- **Rollback command**: `docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml up -d --build` against the previous commit (`git checkout 7b2637b` or the last-known-good SHA before this deployment), OR, if the exact prior image digest is retained by Docker locally, `docker compose up -d` after re-tagging that digest — either path is the existing mechanism, not new tooling.
- **Database compatibility**: `alembic upgrade head` runs on every
  start; this working tree adds no new Alembic migration (confirmed —
  none of the new/modified files touch `trading/database/models.py`'s
  schema or an `alembic/versions/` file), so a rollback never needs an
  `alembic downgrade`.
- **Safety-store compatibility**: rolling back to the pre-15D.5 image
  does **not** delete the volume added in Section 7 — Docker volumes/
  bind-mounts persist independently of which image is running. The old
  code simply won't read those files (it doesn't know about them), and
  the new code, if redeployed later, would find them exactly as left.
  **Rollback does not destroy or reset safety persistence** — Gate 7
  (Section 19) is satisfiable by construction, not by extra tooling.

## 15. Backup

`trading/tools/backup_safety_stores.py` (Phase 15D.8): snapshots each
`.db` file via SQLite's own online backup API (safe against a live,
in-use database) to a timestamped copy in an operator-specified
`--out-dir`; the kill-switch JSON is copied via an atomic rename-based
file copy. Verification: `PRAGMA integrity_check` for SQLite snapshots,
non-empty-file check otherwise. Restore: `restore_one()` refuses a
snapshot that fails verification, then re-verifies the restored copy.
**Not wired into any scheduled job** (confirmed again this phase — no
change since Phase 15D.8/15D.9). Because there is currently **no
existing safety-store data on the production host** (Section 8 — every
store would be a fresh, empty file), a pre-deployment backup of
*production* data is a no-op the first time this is deployed; this tool
becomes meaningful starting from the first real use of the deployed
stores, and running it as a manual, scheduled operator task afterward
remains the recommendation (unchanged from Phase 15D.8's own report).

## 16. Deployment sequence (PLAN ONLY — not executed)

```
 1. Freeze current production state (no new deploys until this completes).
 2. Confirm /opt/centralized-algo/app is (or is remade into) an actual
    git checkout with the correct remote configured (Section 2's first
    finding) -- prerequisite to step 3, decided and performed by a human.
 3. Human reviews this working tree's full diff (19 modified + 46
    untracked files) and the local full-regression evidence (Section
    17/"Test results", once available).
 4. Human commits the reviewed change set on top of 7b2637b.
 5. Push the approved commit to the remote the host's checkout tracks.
 6. Add the one-line volume addition to
    trading/infrastructure/backend/docker-compose.prod.yml (Section 7)
    in that same commit or a small follow-up commit -- also
    human-reviewed before push.
 7. On the host: create /var/lib/centralized-algo/safety-data with
    ownership allowing container UID 10001 to write to it.
 8. Add KILL_SWITCH_PERSISTENCE_PATH and AUDIT_DB_PATH to
    /etc/centralized-algo/backend.env (root-owned, chmod 600, outside
    the repo -- edited directly on the host, not via this repository).
 9. Verify /etc/centralized-algo/backend.env's other required secrets
    (AUTH_SECRET_KEY, broker credential vars if any) are already present
    and correct -- read-only check, no values printed.
10. git pull --ff-only on the host (now that step 2 is satisfied).
11. docker compose -f docker-compose.yml
      -f trading/infrastructure/backend/docker-compose.prod.yml
      up -d --build --remove-orphans
    (equivalently: systemctl restart centralized-algo-backend).
12. Verify the production guard did not block startup (container
    actually comes up -- if KILL_SWITCH_PERSISTENCE_PATH/AUDIT_DB_PATH
    are misconfigured, per Section 10 the app will correctly refuse to
    start; that is success, not failure, of this step's own check).
13. Verify safety stores now exist at the configured paths, owned/
    permissioned correctly (Section 9).
14. Verify /api/health (200, new fields present) and /api/ready (200,
    no longer 404).
15. Verify legacy algorithms remain STOPPED (unchanged from Phase
    15D.10 -- re-check, don't assume).
16. Verify no active LiveAuthorization exists (trivially true -- the
    store is brand new).
17. Verify audit hash chain is valid (trivially true on a fresh store;
    meaningful after the first real event).
18. Verify idempotency/reconciliation persistence -- N/A today (not
    wired into the API layer, Section 2); note this explicitly rather
    than fabricating a check that cannot exist yet.
19. Perform a controlled restart test (Section 18) and re-verify every
    item above survives it.
20. Verify rollback readiness (confirm the previous image digest is
    still available locally on the host, or retaggable).
21. STOP. Report results. A separate, explicit human decision is
    required before any LiveAuthorization, strategy start, or order.
```

## 17. Post-deployment verification (checklist for that future phase, not performed now)

**Application**: `/api/health` 200 with new fields; `/api/ready` 200
(not 404); `APP_ENV=production` confirmed in the running container;
`resolve_environment() == "production"` (can be checked via a one-line
`python -c` invocation inside the container, read-only); production
guard confirmed to have run (container is up *because* both paths were
valid, or correctly refused to start otherwise).

**Safety persistence**: kill-switch JSON exists at the configured path,
mode `0600`; audit DB exists, `PRAGMA integrity_check` clean,
`verify_chain()` (once events exist); idempotency/reconciliation/
LiveAuthorization — explicitly not applicable until a future phase
wires them into the API layer (Section 2).

**Security**: file permissions `0600` on both existing stores; operator
authentication unchanged (Stage 18, already live); account isolation/
credential isolation unchanged (nothing in this deployment touches
`trading/common/credentials.py` or account configuration).

**Legacy strategies**: `DoubleStraddelAlgo`/`CombinedVwapNifty`/
`Vwap_Algo_Nifty_hedge` all STOPPED — re-verified, not assumed
unchanged from Phase 15D.10.

**Historical integrity**: `260917000350205`, `260917000523943`, and the
AG7002 record are **local repository state**, not production state —
re-verified in the local checkout only; this deployment does not touch
them and does not create any production-side historical record.

**Broker**: mutations = 0, new orders = 0 — a deployment restarts a
process; it never itself calls a broker.

## 18. Restart test (to be performed post-deployment, not now)

After the deployment sequence (Section 16) completes and passes its own
checks, a **separate**, controlled `docker compose restart backend` (or
a full `down`/`up` cycle) must be performed and must prove: the
kill-switch file's content is unchanged after restart; the audit DB's
record count and `verify()` result are unchanged; `production_guard.py`
runs again on the new process and still passes (or still fails closed,
consistently); `/api/health`/`/api/ready` return to `200` once the
container is back up. This is listed here as a required future step —
it is not performed in this READ+PLAN phase.

## 19. Deployment approval gates

| Gate | Status as of this report |
|---|---|
| 1 — Code (tested commit, full regression PASS, no unrelated changes) | Regression re-run this phase (Section 20's "Test results"); no unrelated changes found; commit not yet made (human step) |
| 2 — Image (built from approved commit, SHA verified) | Not applicable yet — no commit exists to build from |
| 3 — Persistence (all safety stores durable) | **Not yet satisfied** — requires the Section 7 volume addition, not yet made |
| 4 — Environment (`APP_ENV=production`, all required safety paths configured) | `APP_ENV` already correct; `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH` not yet added |
| 5 — Guard (production guard active, fail-closed verified) | Code-verified and regression-tested locally (Phase 15D.9); not yet exercised on the real host (that only happens once deployed) |
| 6 — Legacy (all legacy strategies STOPPED) | Confirmed STOPPED as of Phase 15D.10's live check; must be re-confirmed at actual deployment time |
| 7 — Rollback (path verified, safety data preserved) | Verified by design (Section 14) — rollback does not touch the persistent volume |
| 8 — Human approval | **Not given** — required before any of Sections 16's steps proceed |

## 20. Remaining blockers

1. **`/opt/centralized-algo/app` is not currently a git checkout** — the
   documented `git pull --ff-only` deployment step would fail as-is.
   Must be resolved (re-clone, or `git init` + remote + reset) before
   Section 16 step 10 can run. This is a **DEPLOYMENT BLOCKER**,
   resolved by a human deployment action, not a code change.
2. **No persistent volume exists for the backend container** — without
   Section 7's one-line addition, any deployment (including this one)
   would produce safety stores that do not survive the *next*
   deployment. This is a **DEPLOYMENT BLOCKER**, resolved by the
   specified additive compose-file change plus one host-side directory
   creation — both described precisely above, neither performed by this
   phase.
3. **Idempotency, reconciliation, and LiveAuthorization stores are not
   wired into the API layer's `ExecutionState` at all** — this is not a
   blocker for *this* deployment (which only needs to ship the code and
   make the kill-switch/audit persistence real), but it means "the
   safety framework is deployed" will still not include a live,
   API-reachable idempotency/authorization gate until a future phase
   adds that wiring. Named here so it is not mistaken for something this
   deployment closes.

No blocker requires a code change to the safety modules themselves —
every blocker found is a **deployment/infrastructure** gap (git
checkout state, missing volume, missing wiring), consistent with this
whole remediation phase's own scope.

---

## Final result

The plan is complete: every step in Sections 1–18 is specified precisely
enough to execute without further investigation, every blocker found has
an exact, minimal, described (not performed) remediation, the rollback
path is safe by construction (Section 14), and the code-side prerequisite
(Gate 1 — tested commit, clean regression, no unrelated changes) is
satisfied as of this report. Gates 2–8 (Section 19) remain unsatisfied
because they require actions — committing, adding the volume mount,
editing `backend.env`, and deploying — that this phase does not perform.

```
PHASE 15D.10-R READ + PLAN = READY FOR HUMAN DEPLOYMENT APPROVAL

NO DEPLOYMENT PERFORMED.
NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
NO REAL BROKER MUTATION PERFORMED.
HARD STOP.
```

Per this phase's own absolute final rule: this result does not itself
authorize committing, pushing, deploying, restarting, creating a
database, creating a LiveAuthorization, authorizing an account, starting
a strategy, or placing an order. Executing Section 16's sequence — or
any part of it — requires a separate, explicit human decision, made
after reviewing this plan, and even a fully successful deployment would
still require a further separate decision before any live canary
(Section 20 of the original brief's own sequence:
`15D.10-R → HUMAN APPROVAL → DEPLOYMENT → POST-DEPLOYMENT VERIFICATION → HUMAN REVIEW → LIVE-CANARY DECISION`).
