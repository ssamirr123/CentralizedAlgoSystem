# Phase 16.1 — Trading Control Center UI/Backend Foundation

**Status: PASS**
**Scope: validation of an already-existing foundation, plus test-coverage gap-filling. No new duplicate API routes or pages were created. No broker mutation, live authorization, or strategy start occurred.**

---

## 1. Architecture discovered

Before writing any code, a research pass (Step 1) confirmed that **the entire objective of this phase already exists**, built across three earlier phases in this repository's own history:

```
React UI (Vite + TypeScript + react-router-dom v6 + @tanstack/react-query v5)
   ↓ same-origin /api/* (dev: Vite proxy; prod: shared nginx)
FastAPI (trading/api/execution_routes.py, Phase 11)
   ↓
ExecutionState (trading/api/execution_state.py) — ONE instance per app, built fresh per create_app()
   ├── BrokerManager       (existing, Phase 1)
   ├── StrategyAssignment  (existing, Phase 1)
   ├── RiskManager         (existing, Phase 6)
   ├── StrategyRegistry    (existing, Phase 10)
   ├── CentralKillSwitch   (existing, Phase 14.6 — shared with LiveCanaryGuard)
   ├── MetricsRegistry / AuditTrail / AlertManager (existing, Phase 13)
```

No second `BrokerAdapter`, `RiskManager`, or `TradingAccount` model was created or considered — every requirement in this phase's brief maps onto an object that already exists.

Authentication: `execution_routes.py`'s router uses the **identical** existing JWT/RBAC stack (`get_principal`, `require_permission`, `Permission` enum, `enforce_rate_limit`) as every other authenticated route in this API — no second auth mechanism.

## 2. Implementation

Given everything requested already existed, this phase's actual work was:

1. **Verification** that the existing implementation satisfies every safety requirement in this phase's brief (Section 6 below).
2. **Closing the one real, confirmed gap**: zero frontend tests existed anywhere in the repository (confirmed by search — no `*.test.ts*`, no `__tests__`, and `package.json` had no test runner at all). Frontend test infrastructure (Vitest + React Testing Library + jsdom) was added, and tests were written against the existing pages per Step 14's own checklist.
3. **One small backend addition**: two new structural tests in the existing `tests/api/test_execution_routes.py`, proving this API layer never touches `LiveAuthorization`/`StrategyExecutionEngine` — closing the one Step 13 item (#10, "no LiveAuthorization is created/consumed") that wasn't already explicitly, structurally asserted.

No new backend route, no new page, no new domain model was created.

## 3. Backend APIs (existing, reused — mapped to this brief's suggested contract)

| Brief's suggested route | Actual existing route | Notes |
|---|---|---|
| `GET /api/control-center/overview` | `GET /api/system/status` + `GET /api/strategies` (client-composed on `TccOverviewPage`) | No single aggregate endpoint exists; the frontend already composes the overview from two existing, individually-cacheable queries — reusing this rather than adding a new aggregate endpoint avoids a second source of truth for the same data |
| `GET /api/control-center/accounts` | `GET /api/accounts` | |
| `GET /api/control-center/strategies` | `GET /api/strategies` | |
| `GET /api/control-center/assignments` | `GET /api/assignments` | |
| `GET /api/control-center/positions` | `GET /api/execution/positions` | Honestly returns `[]` — Phase 10's strategies do not yet generate order intents; not fabricated data |
| `GET /api/control-center/risk` | `GET /api/risk/status` + `GET /api/risk/limits` | |
| `GET /api/control-center/audit` | `GET /api/observability/audit` (frontend calls it via `getAudit`) | |

None of these expose `credential_reference`, `broker_client`, API keys, tokens, or passwords — confirmed both by existing tests (`test_account_response_never_includes_a_credential_field`) and by this phase's own fresh grep (Section 6).

**Mutating routes already present** (pre-existing, not introduced by this phase): `POST /api/strategies/{id}/start|stop` (flips an in-memory `StrategyStatus` only — never touches a broker), `POST /api/assignments` (routes a strategy to an account — never places an order), `POST /api/risk/kill-switch` (ADMIN-only, the one genuinely sensitive route, already fully audited and alerted). None of these were added, modified, or newly connected to anything by this phase.

## 4. Frontend pages (existing, reused)

All under `/execution/*` (namespaced to avoid colliding with the separate legacy telemetry nav): Overview (Dashboard), Strategies, Accounts, Brokers, Assignments, Orders, Positions, P&L, Risk, Logs (Audit), System Health. `LIVE_EXECUTION_ENABLED` is hardcoded `false` in `frontend/src/lib/config.ts`; the LIVE execution mode is filtered out of `ExecutionModeSelect` entirely (not merely disabled), backstopped server-side by every account being `SHADOW` and `StrategyAssignment.assign()` rejecting a mismatched mode with `422`. No `BUY`/`SELL`/`PLACE ORDER`/`AUTHORIZE LIVE`/`ENABLE LIVE` control exists anywhere in this UI, confirmed both by the Phase 12 report and by this phase's own new tests (every page test file explicitly asserts none of these controls render).

## 5. Data models reused (none duplicated)

`TradingAccount`, `StrategyAssignment`, `RiskManager`, `RiskLimits`, `CentralKillSwitch`, `StrategyRegistry`, `BrokerManager` — all imported from `trading/common/*`, unchanged. `ExecutionState` (`trading/api/execution_state.py`) remains the single composition point; this phase's new structural test (`test_execution_state_never_wires_a_live_authorization_store`) asserts its dataclass fields are exactly the 8 that already exist — no ninth field was added.

## 6. Security controls (Step 15, re-verified this phase)

- No credential, API key, access/refresh token, or password found in any API response type or any React source file (fresh grep this phase; the only matches were unrelated, pre-existing `AdminPage.tsx` form-state initializers for a *new user's* password field — empty-string state, not a secret value).
- No `.env` file staged or committed.
- No broker API call exists in `frontend/` — confirmed by the same grep pass finding zero broker SDK references anywhere in `src/`.
- No direct browser-to-broker communication path exists — the backend (`trading/api/*`) remains the only integration boundary, unchanged.
- Authentication/authorization follows the existing application mechanism exactly (Section 1) — no new auth stack was introduced.
- **New this phase**: `test_execution_routes_module_never_touches_live_authorization` and `test_execution_state_never_wires_a_live_authorization_store` — structural proof (source-scan + dataclass-field assertion) that this control-center layer has no path to `LiveAuthorization`/`StrategyExecutionEngine`, closing the one previously-implicit (never explicitly asserted) safety property in this area.

## 7. Test results

**Backend** (`tests/api/test_execution_routes.py`, 45 tests — 43 pre-existing + 2 new): all passing. Full backend regression: `python -m pytest -q --tb=line > file.txt 2>&1; echo "PYTEST_EXIT=$?" >> file.txt` (redirect-based, never piped through `tail`) → **`PYTEST_EXIT=0`, 1654 passed, 0 failed, 0 errors** (6 pre-existing, documented POSIX-only skips; up from the prior 1652-test baseline by exactly the 2 new structural tests, verified via direct `FAILED`/`ERROR` line grep, not the summary line alone).

**Frontend** (new this phase — Vitest + React Testing Library + jsdom, none of which existed before): 8 test files, **48 tests, all passing**:
- `components/States.test.tsx` (14 tests) — the shared `Loading`/`ErrorState`/`EmptyState`/`QueryBoundary` component, covering every page's loading/error/empty/unknown-state presentation in one place (every page delegates to this component for those states).
- `pages/TccOverviewPage.test.tsx` (4), `TccAccountsPage.test.tsx` (6), `TccStrategiesPage.test.tsx` (5), `TccAssignmentsPage.test.tsx` (4), `TccPositionsPage.test.tsx` (5), `TccRiskPage.test.tsx` (6), `TccLogsPage.test.tsx` (4) — dashboard/account/strategy/assignment/position/risk/audit rendering, credential-exclusion, and an explicit "no BUY/SELL/PLACE ORDER/AUTHORIZE LIVE/ENABLE LIVE control renders anywhere" assertion on every single page test file.
- Existing production build (`npm run build`) and typecheck (`npm run typecheck`) both re-verified passing, unaffected by the new test infrastructure (a separate `vitest.config.ts`, not a modification of the production `vite.config.ts`).

## 8. Changed files

```
M  frontend/package.json                          (+test/test:watch scripts, +5 devDependencies)
M  frontend/package-lock.json                      (lockfile update for the above)
M  tests/api/test_execution_routes.py              (+2 structural safety tests)
?? frontend/vitest.config.ts                        (new, separate from vite.config.ts)
?? frontend/src/test/setup.ts                        (new)
?? frontend/src/test/utils.tsx                       (new, shared test render helpers)
?? frontend/src/components/States.test.tsx           (new)
?? frontend/src/pages/TccOverviewPage.test.tsx        (new)
?? frontend/src/pages/TccAccountsPage.test.tsx        (new)
?? frontend/src/pages/TccStrategiesPage.test.tsx      (new)
?? frontend/src/pages/TccAssignmentsPage.test.tsx     (new)
?? frontend/src/pages/TccPositionsPage.test.tsx       (new)
?? frontend/src/pages/TccRiskPage.test.tsx            (new)
?? frontend/src/pages/TccLogsPage.test.tsx            (new)
```

No production source file (`.py` or `.tsx` outside of `*.test.tsx`) was modified. No API route, page, or domain model was added, removed, or changed in behavior.

## 9. Commit SHA

`[[COMMIT_SHA]]` on branch `web-base-algo-trading-control` — see Section 11 (deployment status) for why this commit is not yet pushed or deployed.

## 10. Deployment status

**Not deployed. Not recommended for immediate deployment either**, for a reason unrelated to safety: this phase's changes are entirely dev-time/test-time (a frontend test runner and its config, plus two backend test functions) — there is no user-facing or API-facing behavior change to ship. The existing, already-deployed production backend (`ff0d0f87a66a86beca19ff84b9d1e39c8395ef98`, confirmed still running, unchanged, in Phase 15D.11) is entirely unaffected by anything in this commit. Deployment is a separate decision for whenever a future phase actually changes shipped behavior — this phase's own value is captured entirely in the repository (tests + confidence), not in anything that needs to run on the host.

## 11. Known limitations (named, not silently resolved)

- No single normalized `/api/control-center/overview` aggregate endpoint exists — the frontend composes it client-side from two existing endpoints. If a future phase wants exactly the JSON shape this brief's Step 4 sketched (`environment`, `deployment.gitSha`, `system.health`, `trading.killSwitch`, `accounts.total/readOnly/liveAuthorized`, `strategies.total/running/stopped`), that is a small, additive, genuinely new endpoint — not something this phase invented data for.
- Positions/orders/P&L remain honestly empty — no strategy in this repository generates real order intents yet (Phase 10's own, still-current, deliberate scope boundary).
- The kill switch surfaced in this UI is a real, shared `CentralKillSwitch` instance, but — as the existing `TccRiskPage` UI copy itself already discloses — it is "not yet auto-enforced by any live validation path" through this specific API layer, because no strategy here generates a real order intent for it to intercept. This is accurate, pre-existing, honest UI copy, not something this phase needs to correct.
- Frontend test coverage is now real but not exhaustive — the remaining pages (Brokers, Orders, P&L, System Health, Overview's KillSwitchBanner sub-component) were not individually test-covered this phase; the shared `States.test.tsx` coverage plus the 7 page-level suites already written cover every category Step 14 asked for (dashboard, account, strategy, assignment, position, risk, audit, loading, error, unknown/unavailable, read-only presentation).
- `npm audit` reports 7 vulnerabilities (5 moderate, 1 high, 1 critical) introduced by the new devDependencies' own transitive dependency tree — these are test-tooling-only (never bundled into the production `dist/` output, confirmed by the unchanged production build size/output this phase), but named here rather than silently ignored.

## 12. Next recommended implementation step

Given the existing foundation is confirmed complete and now has real frontend test coverage, the next genuinely new work (explicitly **not** performed by this phase) would be: wiring real order-intent generation from at least one strategy (Phase 10's own named, deliberate scope boundary) so that Positions/Orders/P&L/kill-switch-enforcement stop being honestly-empty placeholders — and, separately and much more carefully, deciding whether/how the Phase 15D.5–15D.9 `LiveAuthorizationWorkflow` should ever be exposed through this control-center API at all (today it deliberately is not, per Section 6's own structural tests) — a decision this phase does not make and was not asked to make.

---

## Conclusion

```
PHASE 16.1 = PASS

NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
NO REAL BROKER MUTATION PERFORMED.
NO STRATEGY STARTED.
PRODUCTION NOT DEPLOYED WITHOUT EXPLICIT APPROVAL.
HARD STOP.
```
