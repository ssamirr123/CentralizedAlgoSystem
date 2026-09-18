# Phase 12 — React Trading Control Center

```text
Phase 11 backend (/api/strategies|accounts|brokers|assignments|risk|execution|system/*)
      ↓
frontend/src/api/{types,endpoints,hooks}.ts  (Phase 12 additions)
      ↓
11 new pages under a new "Trading Control Center" nav section
      ↓
Existing auth/RBAC/QueryBoundary/StatusBadge/ConfirmDialog conventions — reused, not reinvented
```

This phase adds 11 new pages to the **existing** React frontend (`frontend/`, Vite + React Router + TanStack Query, no CSS framework) rather than building a new app — the existing app is already branded "Trading Control Center" in its own sidebar (`Layout.tsx:38`), so this phase extends it in place, following every established convention exactly: the same `NAV_ROUTES` table, the same `apiRequest`/`endpoints.ts`/`hooks.ts` split, the same `PageHeader`/`QueryBoundary`/`StatusBadge`/`ConfirmDialog` components, the same disabled-button-with-tooltip permission-gating pattern, and the same hand-written-types-mirroring-Pydantic convention.

---

## Pages

| # | Requested section | Page component | Route | Nav permission |
|---|---|---|---|---|
| 1 | Overview | `TccOverviewPage` | `/execution` | VIEW |
| 2 | Strategies | `TccStrategiesPage` | `/execution/strategies` | VIEW |
| 3 | Trading Accounts | `TccAccountsPage` | `/execution/accounts` | VIEW |
| 4 | Brokers | `TccBrokersPage` | `/execution/brokers` | VIEW |
| 5 | Strategy Assignments | `TccAssignmentsPage` | `/execution/assignments` | VIEW |
| 6 | Orders | `TccOrdersPage` | `/execution/orders` | VIEW |
| 7 | Positions | `TccPositionsPage` | `/execution/positions` | VIEW |
| 8 | P&L | `TccPnlPage` | `/execution/pnl` | VIEW |
| 9 | Risk | `TccRiskPage` | `/execution/risk` | VIEW |
| 10 | Logs | `TccLogsPage` | `/execution/logs` | **ADMIN** |
| 11 | System Health | `TccSystemHealthPage` | `/execution/system-health` | VIEW |

**Logs is ADMIN-gated, not VIEW**, because it's backed by the pre-existing `GET /api/admin/audit` endpoint, which itself requires ADMIN on the backend — the nav entry's permission matches the backend's actual requirement exactly, so a non-admin never even sees a page that would just 403.

### Naming: why not the exact requested labels for every page

Six of the eleven requested section names (`Overview`≈`Dashboard`, `Strategies`, `Positions`, `P&L`, `Risk`, `Logs`, `System Health`) **already exist** as nav entries for the legacy telemetry system (deployed algo processes on EC2, a different concept entirely from this broker-agnostic execution framework). Reusing those exact labels/paths would either collide outright or silently shadow an existing, already-depended-upon page. Every colliding label is prefixed `Execution ` (matching the backend's own `/api/execution/*` module naming) so both sections stay simultaneously visible and unambiguous in the sidebar; the five names with no legacy counterpart (`Trading Accounts`, `Brokers`, `Strategy Assignments`, `Orders`, `System Health`→`Execution Health`) are otherwise as close to the brief as possible. This mirrors the exact same collision-avoidance reasoning Phase 11 already applied to `/api/execution/orders|positions|pnl` on the backend.

---

## The Strategy Screen

`TccStrategiesPage` renders exactly the requested columns:

```
Strategy | Status | Account | Broker | Mode | P&L | Position | Last heartbeat
```

joining data client-side from four backend calls (`GET /api/strategies`, `/api/assignments`, `/api/accounts`, `/api/execution/pnl`) — the backend deliberately doesn't pre-join these (Phase 11 keeps each resource's own endpoint single-purpose). **Position** and **Last heartbeat** are honestly rendered as `—` with an explanatory tooltip: no strategy generates a non-empty order-intent list yet (Phase 10's documented scope), so there is no live/shadow position to show, and "heartbeat" is a legacy-telemetry concept this execution framework doesn't have yet — inventing a value for either would misrepresent what the backend actually knows.

Per-row controls, exactly as requested:
- **Start / Stop** — `POST /api/strategies/{id}/start|stop`, gated on the `START`/`STOP` permissions (disabled + tooltip when missing, matching `AlgorithmsPage`'s existing convention).
- **Select account** / **select execution mode** — an "Assign" button opens a modal (account `<select>` + `ExecutionModeSelect`) that calls `POST /api/assignments`, gated on `TRADING_CONTROL`.
- **View risk status / orders / positions** — `Link`s to the Risk/Orders/Positions pages (satisfies "allow the user to view X" as in-app navigation, since Phase 11's backend doesn't expose per-strategy-filtered order/position sub-resources yet — there is nothing more specific to link to today).

---

## Live execution controls: hidden unless explicitly enabled by the backend safety gate

`frontend/src/lib/config.ts` **already had** exactly this concept before this phase: `LIVE_EXECUTION_ENABLED`, hard-wired to `false` and explicitly documented as "not driven by env" — the frontend's own pre-existing safety-gate convention for the legacy trading UI. Phase 12 reuses this constant rather than inventing a second one:

`frontend/src/components/ExecutionModeSelect.tsx` — the single shared component behind every execution-mode `<select>` in this phase (the Strategies page's Assign modal, the Assignments page's create form) — filters `LIVE` out of the option list unless `LIVE_EXECUTION_ENABLED` is `true`:

```ts
const options = (modes ?? []).filter((m) => m.value !== "LIVE" || LIVE_EXECUTION_ENABLED);
```

Since that constant is a hard-coded `false` requiring a deliberate code change to flip (not a config toggle, not a backend response field an attacker or misconfiguration could influence), **LIVE never appears as a selectable option anywhere in this UI today** — it is not merely disabled, it is absent from the list entirely. This is a stronger guarantee than hiding/disabling a rendered LIVE option: there is no LIVE control to inspect, right-click, or re-enable via devtools. The moment a future phase adds a genuine backend-driven safety-gate signal, `ExecutionModeSelect.tsx` is documented as the one place to update.

(Server-side, this is backstopped independently: all three example `TradingAccount`s are hardcoded `execution_mode=SHADOW`, and `StrategyAssignment.assign()` already rejects a mismatched execution_mode with a 422 — so even a hypothetical bypass of the frontend gate would still be refused by the backend Phase 11 already tested.)

---

## The UI never calls a broker API directly

Every data-fetching and mutating call in every new page goes through `frontend/src/api/endpoints.ts`'s new "Trading Control Center execution framework" section, which wraps `apiRequest()` — the same shared fetch client every other page uses, targeting only `/api/*` paths on the control-center backend. No new page imports `fetch`/`axios`/any broker SDK directly, and no new page references a broker hostname, API key, or credential field. `AccountOut`'s frontend type (`ExecutionAccount`) mirrors the backend response exactly, which itself excludes `credential_reference`/`broker_client` (Phase 11) — there is no credential field anywhere in this phase's type definitions for the UI to accidentally render.

---

## Files Created
```
frontend/src/pages/TccOverviewPage.tsx
frontend/src/pages/TccStrategiesPage.tsx
frontend/src/pages/TccAccountsPage.tsx
frontend/src/pages/TccBrokersPage.tsx
frontend/src/pages/TccAssignmentsPage.tsx
frontend/src/pages/TccOrdersPage.tsx
frontend/src/pages/TccPositionsPage.tsx
frontend/src/pages/TccPnlPage.tsx
frontend/src/pages/TccRiskPage.tsx
frontend/src/pages/TccLogsPage.tsx
frontend/src/pages/TccSystemHealthPage.tsx
frontend/src/components/ExecutionModeSelect.tsx
frontend/src/components/KillSwitchBanner.tsx
docs/phase-12-react-control-center-report.md
```

## Files Modified (all additive)
```
frontend/src/routes.tsx                 (+11 NAV_ROUTES entries)
frontend/src/api/types.ts               (+~15 interfaces mirroring execution_routes.py's Pydantic models)
frontend/src/api/endpoints.ts           (+~16 endpoint functions)
frontend/src/api/hooks.ts               (+~16 React Query hooks)
frontend/src/components/StatusBadge.tsx (+ StrategyStatus/ConnectionState badge-color mappings)
frontend/src/lib/format.ts              (+ brokerLabel() helper)
tests/core/test_config.py               (unrelated fix, see "Incidental fix" below)
```
No existing page, route, or component's existing behavior was changed. No production/strategy files were modified.

---

## Verification

```
cd frontend
npx tsc --noEmit    → 0 errors
npm run build       → succeeds (vite build, 156 modules transformed)
```

**No frontend test framework exists in this repository** (`frontend/package.json` has no test script, no Vitest/Jest/RTL dependency, and no existing `*.test.*`/`*.spec.*` file to mirror — confirmed by research before starting this phase). Introducing one from scratch was judged out of scope for "build a dashboard" without an explicit request to also stand up a new test framework; verification here is the same two checks (`tsc --noEmit`, `vite build`) this repository already uses as its only frontend correctness gate (`package.json`'s own `build` script runs both). Backend correctness for every endpoint this dashboard calls was already verified by Phase 11's 43 dedicated `pytest` tests, unchanged by this phase.

## Incidental fix: `tests/core/test_config.py`

While confirming the full backend suite stayed green, `tests/core/test_config.py::test_missing_optional_credentials` was found failing — but **only** when run after `tests/security/test_config_fails_safe.py` in the same process, and **only** on a machine with a real `trading/.env` file (this one). Root cause, verified via `git stash` (confirmed present even on the pre-Phase-9 commit, unrelated to any phase in this session): `test_config_fails_safe.py`'s algo-config-import test triggers `DoubleStraddelAlgo`/etc.'s own dotenv fallback, which sets `ANGELONE_MPIN` process-wide from the real `.env` file; `test_config.py`'s own local `clean_env` fixture didn't include `ANGELONE_MPIN` in its scrub list (a gap that predates this session — the same class of hazard Phase 5A already fixed once for `tests/conftest.py`'s own scrub list, but never propagated to this second, independent fixture). Fixed with the same one-line, precedented pattern: added `ANGELONE_MPIN` to `test_config.py`'s `_ENV_KEYS`. Verified: this specific test now passes regardless of run order.

**Three genuinely pre-existing, environment-specific failures remain, unrelated to any phase in this session**: `tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[*]` (all three algos). These fail only because a real `trading/.env` exists on this machine — the algos' own `config.py` files intentionally treat `.env` as a fallback source (by design, and "do not modify strategy code" forbids changing that), so deleting the corresponding env vars via `monkeypatch` doesn't produce an empty value the way the test expects; the fallback simply reads the real file instead. This is not fixable without either modifying algo code (out of scope) or removing the user's own real credential file (not this session's call to make) — left exactly as found, and confirmed via `git stash` to already exist before this session's work began.

## Full Backend Regression Suite
```
python -m pytest tests/ -q
```
Confirmed green except the three pre-existing, environment-specific `test_config_fails_safe.py::test_algo_config_empty_when_env_unset[*]` failures explained above (present before this session's work began, verified via `git stash`). The ordering-dependent `test_missing_optional_credentials` failure that also appeared in one run is fixed and re-verified passing regardless of run order.

---

## Safety Verification

- **The UI never calls a broker API directly** — every call goes through `apiRequest()` targeting `/api/*` on the control-center backend only.
- **No broker credential is fetched, stored, or rendered anywhere in this phase** — `ExecutionAccount`'s frontend type has no credential field, matching the backend response it mirrors.
- **LIVE execution mode is absent from every selector in this UI**, gated by the pre-existing, hard-coded-`false` `LIVE_EXECUTION_ENABLED` constant — not merely disabled, not present in the DOM at all.
- **No new page places, modifies, or cancels an order** — Orders/Positions pages are read-only displays of backend-reported (currently empty) data.
- Auth/RBAC is enforced exactly as everywhere else in this app: every API call carries the Bearer token via the shared `apiRequest` client, and every mutating control is `disabled` + tooltip-explained when the signed-in user lacks the required permission (verified structurally by matching each page's gating to the exact backend permission Phase 11 already enforces server-side).

## Known Limitations

1. **Position and Last-heartbeat columns are always "—"** on the Strategies page — honest, not a bug: no backing data exists yet (Phase 10/11 scope).
2. **No frontend automated tests** — see Verification above; `tsc`/`vite build` are this repo's existing frontation verification gate, and Phase 11's backend tests already cover every endpoint this dashboard calls.
3. **The "view orders/positions" per-strategy actions are unfiltered navigations**, not per-strategy-scoped views — the backend doesn't yet expose a strategy-filtered orders/positions sub-resource (Phase 11 built account-agnostic, all-strategies list endpoints only).

## Recommendation for a Future Phase
Once a future phase wires real order-intent generation and a persisted execution-outcome store (Phase 11's own recommendation), extend `GET /api/execution/orders|positions` with optional `strategy_id`/`account_id` query filters, and update the Strategies page's per-row links to pass them through.

STOP after this report — the UI never calls a broker API directly, talks only to the Trading Control Center backend, and LIVE execution controls remain absent unless a future, deliberate code change flips the existing `LIVE_EXECUTION_ENABLED` safety gate.
