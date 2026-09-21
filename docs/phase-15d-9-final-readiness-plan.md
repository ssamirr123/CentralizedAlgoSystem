# Phase 15D.9 — Final Live-Readiness Plan

**READ + ANALYZE + PLAN only. No code, test, deployment, or infrastructure
file was modified to produce this document. No broker mutation, live
authorization, or strategy start occurred.**

---

## A. Current architecture — the complete live execution safety chain

For an intent submitted through the **new, Phase 15D broker-agnostic
path** (the only path Phase 15D.1 through 15D.8 built and verified):

```
Authenticated Operator (JWT / FakeAuthenticationProvider)
        ↓
AuthorizationService.evaluate() — authenticated? active? permission? account-owned?
        ↓
validate_request() — structural checks, dry-run RiskManager preview
        ↓
run_preflight() — read-only broker/funds/kill-switch/canary/idempotency preview
        ↓
Human confirmation (ConfirmationProvider, only literal True counts)
        ↓
LiveAuthorization created → validated → AUTHORIZED
        ↓
StrategyExecutionEngine.execute():
    0. CentralKillSwitch (unconditional, first, every mode)
    1. Idempotency replay check (authoritative)
    2. TradingAccount resolution + AuthorizationState gate
    3. RiskManager.validate() (14-15 checks; LIVE mode additionally requires
       RiskLimits.is_live_ready() — 7 explicit positive limits, confirmed
       wired at execution.py:784)
    4. Mode-specific gate (LiveCanaryGuard for LIVE_CANARY)
    5. Broker resolution
    6. Live-authorization try_consume() (Phase 15D.5/15D.7, exact-scope + operator_id match)
    7. Idempotency claim() (atomic, immediately pre-broker-call)
    8. BrokerClient.place_order()
    9. Broker response validation (unconditional, Phase 14.6 Blocker B)
    10. Persist idempotency outcome
        ↓
Reconciliation (order-level; position-level not implemented)
        ↓
Audit (hash-chained, append-only, correlated by idempotency_key/correlation_id)
```

This chain is intact, has not been weakened or reordered by any phase
through 15D.8, and is the path the one real successful canary
(`260917000350205`) and the one real rejection (AG7002) both went
through.

**This is NOT the only path in the repository that can place a real
order** — see Section D.

## B. Control matrix

See `docs/phase-15d-9-final-readiness-matrix.md` for the full,
row-by-row control matrix (28 controls). Summary: 24 PASS, 2 PARTIAL
(reconciliation position-corroboration; rollback documented-not-automated
— both accepted, non-blocking limitations), **2 BLOCKED**.

## C. Evidence required (and already obtained this phase)

- Full regression: last confirmed at Phase 15D.8's own report,
  `EXIT=0`, 0 failed/errors (verified by direct file inspection, not the
  trusted-but-unverified summary line — this project's own established
  methodology).
- Historical-record re-verification (this phase, direct read):
  `260917000350205` = `COMPLETED` (unchanged); AG7002 record = `PENDING`
  (unchanged); `260917000523943` — no idempotency record exists for it
  (still unattributed).
- Audit hash chain (this phase, direct check):
  `PersistentAuditTrail(db_path='trading/phase15d2_audit.db').verify()` →
  `True`, 14 records (unchanged count).
- No `*live_auth*.db` file exists anywhere outside a test `tmp_path`
  (re-confirmed this phase).
- Test-coverage inventory (this phase, via research agent): 1621
  collected test items across 116 files; every one of the 15 brief-listed
  test areas has dedicated coverage; the only coverage gap
  (position-based reconciliation) is a documented limitation, not a
  silent hole.

## D. Remaining blockers

### D.1 — Legacy algo processes bypass the entire Phase 15D safety chain

`trading/algos/DoubleStraddelAlgo/broker/orders.py`,
`trading/algos/CombinedVwapNifty/rest_func.py`, and
`trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py` each call
`config.objconn.placeOrder(...)` — the raw Angel One SmartAPI connection
object — **directly**, with no reference anywhere in those files to
`RiskManager`, `CentralKillSwitch`, `LiveAuthorization`, or
`StrategyExecutionEngine`. Verified by direct grep this phase (zero
matches for kill-switch/risk-manager keywords in `orders.py`).

- `DoubleStraddelAlgo` and `CombinedVwapNifty` each gate their own real
  order call behind a `DRY_RUN` env var
  (`BOT_DRY_RUN`/`BOT_DY_RUN`, **default `"true"`** — safe by default).
- `Vwap_Algo_Nifty_hedge/rest_func.py` has **no dry-run gate found at
  all** — `place_market_order()`/`place_stoploss_order()` call
  `config.objconn.placeOrder(orderparams)` unconditionally whenever
  invoked, with no local config flag checked in that file.
- The `DoubleStraddleStrategy`/`CombinedVwapNiftyStrategy`/
  `VwapAlgoNiftyHedgeStrategy` adapters registered in the new
  `StrategyRegistry` (`trading/api/execution_state.py`) are explicitly,
  by their own docstrings, **inert shims** — `generate_order_intents()`
  returns `[]`, and they deliberately "do NOT run, start, or otherwise
  touch the live algo process." The real algo processes, if running, are
  started independently via
  `trading/infrastructure/strategy/systemd/centralized-algo-strategy@.service`
  → `centralized-algo-agent START_ALGO %i`, entirely outside the FastAPI
  control-center process this whole Phase 15D safety framework lives in.

**Consequence for the readiness question**: engaging the new
`CentralKillSwitch` — the single control this entire project has spent
the most effort on making unconditional and fail-closed — has **zero
effect** on any of these three processes if one is running with
`DRY_RUN=false` (or, for `Vwap_Algo_Nifty_hedge`, running at all with a
live `objconn`). This directly contradicts the phase's own guiding
question: *"...the system has sufficient ... safeguards to execute only
that action and fail safely otherwise."* It does not, if "the system"
includes these three processes.

**This does not mean the specific, narrowly-scoped 15D.10 action (a
single order placed through the already-proven new path, using a fresh
`LiveAuthorization`) is unsafe on its own terms** — that path's own
chain is intact (Section A). It means a **broader** safety claim about
"the Trading Control Center" cannot be made honestly without either (a)
operationally guaranteeing these three legacy processes are stopped or
verifiably in `DRY_RUN`/safe mode for the entire duration of any live
canary window, or (b) a future phase actually retiring/gating them.

### D.2 — The Phase 15D.8 production guard is a no-op in the real deployed configuration

`trading/common/production_guard.py::_current_environment()` reads
`ENVIRONMENT`, then `ENV`, defaulting to `"development"` — deliberately
mirroring `trading/common/deployment_info.py`'s own, pre-existing
precedence (confirmed identical, this phase: both files use exactly
`os.environ.get("ENVIRONMENT", os.environ.get("ENV", "development"))`).

The actual production deployment
(`trading/infrastructure/backend/docker-compose.prod.yml`, confirmed this
phase) sets **`APP_ENV: production`** — never `ENVIRONMENT` or `ENV`.
Neither compose file sets `KILL_SWITCH_PERSISTENCE_PATH` or
`AUDIT_DB_PATH` either; those are expected to come from an untracked,
external `/etc/centralized-algo/backend.env` file this analysis cannot
inspect.

**Consequence**: in the real deployed container, as configured today,
`is_production_environment()` returns `False` (it sees `"development"`),
so `check_production_safety_paths()` silently returns without checking
anything — the exact silent-degradation failure mode Phase 15D.8 was
built to close remains open in practice, because the guard is watching
a variable name (`ENVIRONMENT`/`ENV`) the deployment doesn't set,
instead of the one it does (`APP_ENV`).

This is not a new bug Phase 15D.8 introduced in isolation — it inherited
an already-inconsistent convention from `deployment_info.py` — but it
means the specific safety property Phase 15D.8's report claims ("a
production deploy that forgets these env vars now refuses to start")
does **not** currently hold against the actual, real compose files in
this repository.

### D.3 (named, not currently exploited) — `modify_order()` bypass surface

`AngelOneBroker`, `DhanBroker`, and `ICICIBreezeBroker` each expose a
public `modify_order()` method that is **not** part of the `BrokerClient`
ABC interface `StrategyExecutionEngine` programs against. It is gated by
each adapter's own read-only check, but not by `RiskManager`,
`LiveCanaryGuard`, or idempotency — a caller holding a raw adapter
reference could call it directly. Grep confirms **no current caller does
this** (the only two callers found are the deliberate read-only
validation harnesses proving it's blocked). Not currently exploited, but
a latent architectural gap worth closing before any phase that hands out
raw adapter references more broadly (e.g., an admin diagnostic tool).

## E. Non-blocking limitations (do not prevent a controlled canary)

- **Reconciliation is order-level only** — no position-based
  corroboration. Named since Phase 15D.4; does not weaken the specific
  authorization-consumption chain a 15D.10 canary would exercise.
- **Rollback is documented, not automated.** Acceptable per the
  agreed Phase 15D.8 scope.
- **No reference clock wired into the clock-drift diagnostic** —
  reports `not_checked` today. Purely informational even when wired;
  absence doesn't weaken any gate.
- **Monitoring/alerting was not re-audited in depth this phase** —
  `observability.py`/`log_shipper.py`/`heartbeat.py`/CloudWatch config
  exist but weren't exhaustively re-verified; flagged as a gap in
  verification depth, not a known defect.
- **Secrets-manager integration, account-authorization-state
  persistence, a full CI/CD pipeline, and AWS infrastructure changes**
  remain explicitly deferred per the Phase 15D.8 agreement — none of
  these gate the narrow question this phase (or 15D.10) is asking, since
  the current credential/env-var-based approach and the
  reset-to-`READ_ONLY`-on-restart account-state behavior are both
  already fail-safe in the conservative direction.

## F. Required pre-canary conditions (must be true before 15D.10)

1. Full regression re-run immediately before 15D.10, with a trustworthy
   exit-code capture, `0 failed / 0 errors`.
2. **D.1 addressed operationally at minimum**: written, explicit
   confirmation (not assumed) that `DoubleStraddelAlgo`,
   `CombinedVwapNifty`, and `Vwap_Algo_Nifty_hedge`'s live processes are
   either stopped, or verifiably running with `DRY_RUN`/paper mode
   active, for the entire duration of the 15D.10 canary window. A
   one-line operator confirmation plus a process-list/config check,
   captured in the 15D.10 report, is sufficient — this does not require
   code changes.
3. **D.2 fixed or worked around**: either (a) `production_guard.py` and
   `deployment_info.py` are extended to also recognize `APP_ENV` (a
   small, additive change, out of this phase's own no-implementation
   scope but trivial for a future phase), or (b) the external
   `backend.env` file is confirmed (by whoever has access to the
   production host) to also set `ENVIRONMENT=production`, or (c) for the
   15D.10 canary specifically, the operator manually verifies
   `KILL_SWITCH_PERSISTENCE_PATH`/`AUDIT_DB_PATH` are correctly set and
   writable on the actual host before authorizing anything, independent
   of whether the automated guard fired.
4. No `LiveAuthorization` currently outstanding (PENDING or AUTHORIZED)
   anywhere — confirmed clean today (no real live-auth DB exists).
5. Account A = READ_ONLY, Account B = READ_ONLY/FLAT, confirmed
   immediately before 15D.10 by direct broker read (not assumed from
   this report's timestamp).
6. No open position on Account B (confirmed via read-only reconciliation
   immediately before 15D.10, not assumed).
7. Historical records (`260917000350205`, AG7002, `260917000523943`)
   re-verified unchanged immediately before 15D.10.

## G. Conditions that automatically block (restated, all still hold today except where noted)

Failed regression; missing kill-switch persistence (see D.2 —
**currently effectively unenforced in production config**); missing
audit persistence (same); unauthenticated operator reaching
authorization (not observed — PASS); account isolation failure (not
observed — PASS); credential isolation failure (not observed — PASS);
authorization mismatch (not observed — PASS); broken idempotency (not
observed — PASS); reconciliation failure (not observed for order-level;
position-level not implemented, a named limitation not a failure);
audit corruption (not observed — chain verified valid); **unsafe
deployment configuration (D.2 — currently true)**; **unknown/unguarded
mutation path (D.1 — currently true)**; unexpected open position (must
be re-checked immediately pre-15D.10, not assumed from this report);
active strategy (must be re-checked immediately pre-15D.10); live
authorization already outstanding (confirmed clean today); unresolved
production safety defect (D.1 and D.2 are both unresolved production
safety defects as of this report).

## H. What 15D.9 does NOT authorize

Nothing. This phase performed READ + ANALYZE + PLAN only.

```
15D.9 does NOT authorize:
  - a live order
  - a broker mutation
  - strategy activation
  - production trading
  - disengaging or re-engaging the kill switch
  - creating a LiveAuthorization
```

---

## Proposed 15D.9 implementation/test plan (for a FUTURE, separate
    implementation phase — not performed now)

Only what is needed to close D.1/D.2/D.3, minimally:

1. **D.2 fix**: add `APP_ENV` as a third, equally-weighted source in
   `_current_environment()` (production_guard.py) and, for consistency,
   `deployment_info.py`'s own environment resolution — so the startup
   banner and the guard agree with what the real deployment actually
   sets. Add a regression test asserting `APP_ENV=production` alone (no
   `ENVIRONMENT`/`ENV`) is recognized as production by the guard. This is
   a small, additive, well-scoped fix.
2. **D.1 mitigation options** (in order of increasing effort, to be
   decided by a human, not assumed here):
   - (a) Minimal: an operational runbook entry (already partially
     covered by Section F.2) plus a pre-canary automated check — a
     script that confirms the three legacy algo systemd services are
     inactive (or their `DRY_RUN` env var is `true`) before a 15D.10
     canary is allowed to proceed. Read-only, no behavior change to the
     algos themselves.
   - (b) Medium: wire a *shared* `CentralKillSwitch` persistence file
     into the legacy algos' own order-placement call sites (a single
     `if kill_switch_engaged(): raise/return` check added at each
     `placeOrder` call site), so the one kill switch this project has
     built actually governs all four order-placement paths, not just the
     new one. Would need its own dedicated, carefully-scoped phase given
     these are live, separately-deployed processes.
   - (c) Full: retire the legacy direct-SmartAPI order paths entirely in
     favor of routing everything through `StrategyExecutionEngine` (the
     long-term direction the `DoubleStraddleStrategy` adapter's own
     docstring already gestures at). Large, multi-phase effort.
3. **D.3 fix**: either remove the three adapters' extra `modify_order()`
   public methods (if truly unused) or add a runtime guard mirroring
   `_require_not_read_only()` that additionally requires the call to
   have originated from `StrategyExecutionEngine` — smallest fix: simply
   rename to `_modify_order_internal` / underscore-prefix them if no
   external caller needs the public method, closing the surface without
   removing functionality.

None of this was implemented in this phase, per its own explicit
instruction.

## Proposed 15D.10 prerequisites (exact conditions)

A future, separate Phase 15D.10 controlled-canary authorization should
not proceed until:

- All of Section F (1-7) are satisfied and re-verified on the day of the
  canary, not assumed from this report's timestamp.
- D.1 is at minimum operationally mitigated (Section F.2) and D.2 is at
  minimum worked around (Section F.3) — full code fixes (this plan's
  "implementation/test plan" above) are preferred but not strictly
  required if the operational workarounds are followed and documented.
- A human (not this session, not any automated process) explicitly
  reviews this report and its matrix and gives affirmative, written
  go-ahead specifically for 15D.10 — this document alone is not that
  authorization.
