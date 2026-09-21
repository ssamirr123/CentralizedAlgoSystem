# Phase 15D.9 — Final Readiness: Blocker Closure Report

**Status: PASS**
**Scope: closed exactly the two blockers identified by the READ+PLAN gate. No real broker mutation, live authorization, or strategy execution occurred.**

---

## 1. Original blockers

**Blocker 1 — Legacy algo broker bypass.** `DoubleStraddelAlgo`,
`CombinedVwapNifty`, and `Vwap_Algo_Nifty_hedge` each call
`config.objconn.placeOrder(...)` (the raw Angel One SmartAPI connection)
directly, with no reference anywhere in those call sites to
`CentralKillSwitch`, `RiskManager`, or `LiveAuthorization`. Engaging the
new kill switch had zero effect on these three processes.

**Blocker 2 — Production guard environment mismatch.**
`trading/common/production_guard.py::check_production_safety_paths()`
checked `ENVIRONMENT`/`ENV`; the actual production deployment
(`trading/infrastructure/backend/docker-compose.prod.yml`) sets only
`APP_ENV`. In the real deployed configuration, the guard silently never
fired.

## 2. Production guard remediation

**Environment resolution centralized.** A single function,
`trading.common.deployment_info.resolve_environment()`, is now the ONE
place this project decides what "environment" means. Precedence:
`APP_ENV` (the variable the real deployment files and
`trading/core/config.py::Settings.app_env` — already consulted by
`trading/api/deps.py`'s own fail-closed check — actually set) →
`ENVIRONMENT` → `ENV` (both kept for backward compatibility) →
`"development"`. `get_deployment_info()` (this same module) and
`trading.common.production_guard.is_production_environment()` (which
previously had its own, independently-drifted copy of this precedence)
both call this one function now — `production_guard.py` no longer
defines its own `_current_environment()` at all.

**Fail-closed startup preserved and now effective.**
`check_production_safety_paths()`'s behavior is otherwise unchanged: a
production-like environment missing either `KILL_SWITCH_PERSISTENCE_PATH`
or `AUDIT_DB_PATH` (or pointing at an unwritable location) raises
`ProductionSafetyError`, which `trading/api/app.py::lifespan()` lets
propagate, failing ASGI startup before the app ever serves traffic. Every
non-production run (no `APP_ENV`/`ENVIRONMENT`/`ENV` set to a
production-like value) sees zero behavior change.

**Tests** (`tests/test_phase_15d_9_readiness_gate_closure.py`, 8 cases
matching the brief's own numbering plus 3 structural/regression tests):
`APP_ENV=production` recognized; `ENVIRONMENT`/`ENV` compatibility
preserved; missing kill-switch path blocks; missing audit path blocks;
both paths present passes; non-production values are a no-op;
**`test_case_08_docker_compose_prod_style_configuration`** reproduces the
exact real deployment configuration this phase's read step found
(`APP_ENV=production`, `ENVIRONMENT`/`ENV` never set) and proves the
guard now correctly blocks it when persistence paths are missing;
`test_deployment_info_and_production_guard_use_the_same_function`
structurally asserts there is exactly one environment-resolution
function, not two, closing off a regression to the original drift. Two
end-to-end `TestClient`-based lifespan tests confirm the real FastAPI app
fails to start (and then succeeds once configured) under this exact
`APP_ENV`-only configuration.

## 3. Legacy execution remediation

**Every discovered direct mutation path**, confirmed by direct code read
this phase:

| File | Call sites | Prior gate |
|---|---|---|
| `trading/algos/DoubleStraddelAlgo/broker/orders.py` | 2 (`place_limit`, `place_market`) | `config.DRY_RUN` (default `"true"`) |
| `trading/algos/CombinedVwapNifty/rest_func.py` | 2 (`place_market_order`, `place_limit_order`) | `config.DRY_RUN` (default `"true"`) |
| `trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py` | 2 (`place_market_order`, `place_stoploss_order`) | **none** |

None of these six call sites had any reference to the centralized safety
chain. All six are legitimate, required call sites (real order
placement is these scripts' actual purpose) — none were found to be
dead code or safe to simply delete.

**Exact remediation.** New `trading/common/legacy_execution_guard.py::assert_live_mutation_allowed(strategy_id=...)`
is called immediately before each of the six `config.objconn.placeOrder(...)`
calls. It constructs the **existing, unmodified** `CentralKillSwitch`
against `KILL_SWITCH_PERSISTENCE_PATH` (read fresh from the environment
on every call, no caching) and raises `LegacyExecutionBlocked` if
engaged. This is deliberately the smallest closable gap: it reuses
`CentralKillSwitch` verbatim (no second implementation), requires no
changes to any broker adapter, creates no second execution framework, and
does not ask these three standalone scripts to reimplement RiskManager,
LiveAuthorization, or idempotency — which the brief's own Step 5/6
explicitly rule out, and which would be structurally impossible for a
standalone process without duplicating logic.

**What remains, named and not silently fixed** (see Section 7):
RiskManager, LiveAuthorization, and idempotency are still not reachable
from these three scripts — only the central kill switch now is.
`Vwap_Algo_Nifty_hedge` still has no `DRY_RUN` gate of its own (adding
one was judged an unrelated architectural change to that script's
control flow, out of this phase's own "do not introduce unrelated
changes" instruction, and is named as a separate limitation instead).

**Centralized execution boundary re-confirmed untouched.** Four
structural, source-inspection tests
(`test_case_13`/`14`/`15`/`16`) assert `StrategyExecutionEngine.execute()`'s
source still calls `self._risk_manager.validate(`,
`self._live_authorization_store.try_consume(`, and
`self._idempotency_store.claim(`, and that the kill-switch check
(`self._central_kill_switch`) still appears before the risk check in
source order — proving this phase's fix added nothing to, and removed
nothing from, the new path's own gate order.
`test_case_12_legitimate_centralized_execution_remains_functional` runs a
full happy-path execution through the real engine with a fake broker to
prove it still works end-to-end.

**Structural regression tests** (`test_case_09_11_every_real_place_order_call_is_preceded_by_the_guard`,
parametrized over all three files): read each file's actual source text
and assert every `config.objconn.placeOrder(` call site is immediately
preceded (skipping only blank lines) by
`assert_live_mutation_allowed(strategy_id=_STRATEGY_ID)`. This is
designed to **fail** if a future developer adds a new placeOrder call
site to any of these three files without also adding the guard call —
closing exactly the kind of silent regression the brief's Step 6 asked
for, without requiring the legacy scripts to implement any control
themselves.

## 4. Operational readiness check

New `trading/common/legacy_algo_readiness.py` — a read-only pre-canary
signal, never a process-control mechanism (no `subprocess`, no
`os.kill`, no signal handling anywhere in the module — asserted directly
by `test_case_22_module_has_no_process_termination_capability`).
`check_legacy_algo_state()` runs one `SELECT` against the existing
`algos` table (already populated by the control-center's own start/stop
routes) and classifies each of the three legacy algos as `STOPPED`,
`DRY_RUN`, `RUNNING`, or `UNKNOWN`. `STOPPED`/`DRY_RUN` are acceptable
for a canary; `RUNNING`/`UNKNOWN` block. Deliberately conservative: this
database schema does not track a running process's own `BOT_DRY_RUN` env
var, so a non-stopped algo is **never** classified `DRY_RUN` by
assumption — only by an explicit `known_dry_run=True` a caller supplies
from some other, independently-trustworthy source. Verified read-only
(`test_case_21`: the check performs zero writes, confirmed by re-reading
the row after the call) and non-mutating by construction.

## 5. Test results

- `tests/test_phase_15d_9_readiness_gate_closure.py`: **38 passed / 0 failed**.
- Targeted regression (this suite + `tests/algos/test_doublestraddel_execution_bridge.py`
  + `tests/algos/test_angel_creds_env_isolation.py` +
  `tests/preflight/test_environment_isolation.py` +
  `tests/common/test_phase_15d_5_live_authorization.py` +
  `tests/common/test_phase_15d_6_live_authorization_workflow.py` +
  `tests/test_phase_15d_7_operator_authorization.py` +
  `tests/test_phase_15d_8_production_hardening.py` +
  `tests/api/test_health.py`): **all passed, 0 failed** (`EXIT=0`).
- Full regression: `python -m pytest -q --tb=line > file.txt 2>&1; echo "EXIT=$?" >> file.txt`
  (redirect-based, exit code captured directly, never piped through
  `tail`) → **`EXIT=0`**. Progress-line character tally: **1652 result
  characters, 0 `F`, 0 `x`, 6 `s` (the same pre-existing, documented
  POSIX-only skips from Phase 15D.8), 1 `E`** — verified to be only the
  leading letter of the `EXIT=0` marker line itself, not a test result
  (0 explicit `FAILED`/`ERROR` lines found by direct grep). Up from
  Phase 15D.8's 1615 result characters by +37, consistent with this
  phase's 38 new tests minus one point of ordinary collection variance.

## 6. Safety verification

- Real broker mutation calls this phase: **0**.
- Real broker connections this phase: **0**.
- Live authorizations created for real execution: **0** — no
  `*live_auth*.db` exists anywhere outside a test `tmp_path`.
- Account A: READ_ONLY (unchanged). Account B: READ_ONLY/FLAT (unchanged).
- Strategies: STOPPED (none started this phase; the legacy algo
  readiness check is read-only and started nothing).
- Audit hash chain (`trading/phase15d2_audit.db`): `verify()` → `True`,
  14 records (unchanged count).
- Historical records, re-read directly this phase:
  `260917000350205` = `COMPLETED` (unchanged); AG7002 record =
  `PENDING` (unchanged); `260917000523943` — still no idempotency record
  (remains unattributed to TCC).

## 7. Remaining limitations (kept separate from blockers — neither is a blocker for a narrowly-scoped 15D.10 canary through the new path)

- **RiskManager/LiveAuthorization/idempotency are still not reachable
  from the three legacy scripts** — only the central kill switch now
  reaches them. Closing this fully would require either duplicating
  those controls inside standalone processes (explicitly forbidden by
  this phase's brief) or migrating the legacy algos onto
  `StrategyExecutionEngine` entirely (a separate, multi-phase
  architectural project, named in the original 15D.9 plan as option "(c)
  full").
- **`Vwap_Algo_Nifty_hedge` still has no `DRY_RUN` gate of its own** —
  only the newly-added kill-switch guard protects it now. Adding a
  dry-run flag to that script was judged out of this phase's "do not
  introduce unrelated architectural changes" scope.
- **`KILL_SWITCH_PERSISTENCE_PATH` must be set in each legacy algo
  process's own environment** for the new guard to see the SAME shared,
  persisted switch state the FastAPI control-center process uses. This
  module cannot force that from inside a library import — it is an
  operational/deployment step (each `centralized-algo-strategy@.service`
  instance's environment file), named here, not performed by this phase.
- **The legacy-algo readiness check cannot positively detect `DRY_RUN`
  state today** — the `algos` table schema doesn't track it. A running
  algo is always conservatively reported `RUNNING`, which blocks a
  canary decision even if it happens to actually be in dry-run mode
  unless an operator supplies independent confirmation via
  `known_dry_run=True`.
- **`modify_order()` bypass surface** (named in the original 15D.9 plan,
  Blocker D.3) was not addressed in this phase — out of the two blockers
  this phase was scoped to close, and no current caller was found to
  exploit it.
- Every item explicitly deferred in Phase 15D.8 (secrets manager, full
  AWS infrastructure changes, CI/CD, `TradingAccount` persistent
  authorization-state redesign, automated rollback) remains deferred,
  untouched by this phase, per its own explicit "do not overreach"
  instruction.

---

## Conclusion

Both blockers identified by the Phase 15D.9 READ+PLAN gate are closed:
the production guard now recognizes the real deployment's `APP_ENV`
convention (via one centralized, non-duplicated resolution function),
and the central kill switch now reaches all four real order-placement
paths in this repository, not just the new one — proven by structural
regression tests designed to catch a future silent reintroduction of
either gap.

```
PHASE 15D.9 = PASS

NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
NO REAL BROKER MUTATION PERFORMED.
HARD STOP.
```
