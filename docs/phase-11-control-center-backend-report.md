# Phase 11 — Trading Control Center Backend

```text
Strategy/Account/Broker/Assignment/Risk state (Phases 1-10, trading/common/*)
      ↓
trading/api/execution_state.py -- ExecutionState (per-app, built at create_app() time)
      ↓
trading/api/execution_routes.py -- 16 new endpoints
      ↓
Existing control-center auth/RBAC/audit stack (Stage 18) -- reused, not reinvented
```

This phase adds a new FastAPI router (`trading/api/execution_routes.py`) to the **existing** control-center backend under `trading/api/` (a pre-existing, already-live FastAPI application with its own auth, RBAC, audit-log, and DB conventions — Stage 18/19/20). Every requirement in this phase's brief is satisfied by **reusing** that existing machinery rather than building a parallel one:

| Requirement | How it's satisfied |
|---|---|
| Authentication | Router-level `Depends(get_principal)` (existing, `trading/api/deps.py`) — Bearer JWT for humans, `X-API-Key` for the one machine identity. No new auth mechanism. |
| Authorization | Per-route `Depends(require_permission(Permission.X))` (existing) — reuses the existing 6-permission model (`VIEW/START/STOP/RESTART/TRADING_CONTROL/ADMIN`) and 4 fixed roles (`viewer/trader/operator/admin`). No new `Permission` was added — see "Permission mapping" below for the rationale. |
| Validation | Pydantic request/response models (`AssignmentIn`, `KillSwitchIn`, ...) + explicit error mapping from the Phase 1-10 domain exceptions to HTTP status codes. |
| Audit logging | Every mutation calls the existing `audit.record(db, ...)` helper (`trading/api/security/audit.py`), writing to the same `audit_log` table every other control-center action already uses. Five new action constants added (additive). |
| Rate limiting | Router-level `Depends(enforce_rate_limit)` (existing), same as every other control-center router. |

---

## Endpoints

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/api/strategies` | VIEW | All registered strategies (Phase 10's `StrategyRegistry`) |
| GET | `/api/strategies/{strategy_id}` | VIEW | 404 if unknown |
| POST | `/api/strategies/{strategy_id}/start` | START | Auto-`enable()`s from DISABLED/STOPPED/ERROR, then `start()`s. 409 if already active. |
| POST | `/api/strategies/{strategy_id}/stop` | STOP | 409 if not active |
| GET | `/api/accounts` | VIEW | The three example `TradingAccount`s (see below) |
| GET | `/api/accounts/{account_id}` | VIEW | 404 if unknown |
| GET | `/api/brokers` | VIEW | Broker-type availability + account count per broker |
| GET | `/api/assignments` | VIEW | Every strategy currently assigned |
| GET | `/api/assignments/{strategy_id}` | VIEW | 404 if unassigned |
| POST | `/api/assignments` | TRADING_CONTROL | Wraps `StrategyAssignment.assign()`; 404/409/422 mapped from its exceptions |
| GET | `/api/execution-modes` | VIEW | Static `LIVE`/`PAPER`/`SHADOW` enumeration |
| GET | `/api/risk/status` | VIEW | Kill-switch state + configured limits + assigned-strategy count |
| GET | `/api/risk/limits` | VIEW | Just the configured `RiskLimits` |
| POST | `/api/risk/kill-switch` | **ADMIN** | Engage/disengage the process-wide kill switch |
| GET | `/api/execution/orders` | VIEW | Empty today — see Known Limitations |
| GET | `/api/execution/positions` | VIEW | Empty today — see Known Limitations |
| GET | `/api/execution/pnl` | VIEW | Zeroed today — see Known Limitations |
| GET | `/api/system/status` | VIEW | Kill-switch flag, per-status strategy counts, account/broker counts |

**AUDIT LOGS**: satisfied by reuse of the pre-existing `GET /api/admin/audit` (ADMIN-only) — not duplicated. Every Phase 11 mutation writes into the same table that endpoint already reads.

### Permission mapping — deliberately reuses the existing 6, adds none

Adding a new `Permission` member would require editing `ROLE_PERMISSIONS` for every existing role in `trading/api/security/permissions.py` — a shared enum whose blast radius extends to every other route in the whole control-center API. Given every capability this phase needs already has a natural fit in the existing model, no new permission was added:

- Every **read** (`GET`) endpoint requires `VIEW`.
- Strategy `start`/`stop` reuse the existing `START`/`STOP` permissions — the exact same ones a `trader` already holds for algo control, since "start/stop a strategy" is the same class of action as "start/stop an algo process."
- Creating an assignment (`POST /api/assignments`) requires `TRADING_CONTROL` — matching that permission's existing definition ("heavier operations... registering or deleting servers and algos").
- The kill switch (`POST /api/risk/kill-switch`) requires **ADMIN** — the single most sensitive action this API exposes, deliberately given the strictest existing role rather than `TRADING_CONTROL`.

---

## ExecutionState — one instance per FastAPI app, not a module-level global

`trading/api/execution_state.py` builds one `BrokerManager` + `StrategyAssignment` + `RiskManager` + `StrategyRegistry` (with the three Phase 10 strategy adapters already registered) + a new in-memory `KillSwitch`, stored on `app.state.execution`. `create_app()` builds this fresh every time it's called — which matters for test isolation: `tests/conftest.py`'s `app` fixture calls `create_app()` once per test, so no test's strategy-lifecycle/assignment/kill-switch state can leak into another (the same class of hazard Phase 5A hit and fixed for module-level env mutation). Verified by `test_full_regression_isolation_between_tests`.

Three example accounts are seeded, mirroring the Phase 7 reference registry exactly:

| account_id | broker_id | execution_mode | availability |
|---|---|---|---|
| ANGEL_MAIN | angelone | SHADOW | available |
| DHAN_MAIN | dhan | SHADOW | **unavailable** (Phase 8 adapter not yet validated against a real account) |
| ICICI_MAIN | icici_breeze | SHADOW | **unavailable** (Phase 9 adapter not yet validated against a real account) |

Each account's `BrokerClient` is built via the same lazy `create_broker()` factory production would use — but **no route in this phase ever calls `BrokerManager.get_broker()`**, so that factory is never invoked and no broker SDK/network/credential is ever touched. `GET /api/accounts`/`GET /api/brokers` list static `TradingAccount` metadata only. Verified structurally by `test_execution_routes_module_never_calls_get_broker` (asserts `"broker_manager.get_broker("`, `".connect()"`, and `"place_order"` never appear in the router's source).

---

## Path-collision avoidance: `/api/execution/orders|positions|pnl`

`GET /api/positions` and `GET /api/pnl`(`/today`) **already existed** in `trading/api/routes.py` before this phase, serving the current, already-live production telemetry system (deployed algo processes POSTing real heartbeats/trades/positions/pnl — a different concept entirely from this phase's shadow-only, in-memory execution framework). Reusing those exact paths for different data would either silently shadow a live, already-depended-upon endpoint or collide outright. This phase's example brief said `GET /api/orders` / `GET /api/positions` / `GET /api/pnl`; the three equivalent endpoints here are namespaced `/api/execution/orders|positions|pnl` instead, to avoid touching the existing, live production routes at all — an explicit "do not change trading behavior" concern extended to "do not change existing API behavior" as well.

---

## Secret / credential exposure — explicit verification

- `AccountOut` (the `GET /api/accounts` response schema) **excludes** `TradingAccount.credential_reference` and `broker_client` entirely — even though `credential_reference` is itself documented as a non-secret pointer (e.g. `"env:ANGELONE_*"`), this phase excludes it anyway as defense-in-depth, per the explicit "do not expose secrets" instruction.
- No response schema in `execution_routes.py` contains a field named (or resembling) `password`, `secret`, `token`, `api_key`, or similar.
- `test_account_response_never_includes_a_credential_field` asserts this structurally against the live JSON response, not just by code review.
- No route ever calls `TradingConfig.credentials` or any `BrokerCredentials` field.

---

## No live trading enabled — explicit verification

- No route ever calls `BrokerManager.get_broker()` (the only method that would connect to a real broker) — verified structurally (`test_execution_routes_module_never_calls_get_broker`).
- No route ever calls `place_order`/`modify_order`/`cancel_order`, on any broker, real or shadow.
- No route ever calls `StrategyRegistry.generate_order_intents()` — strategy `start`/`stop` only flip this process's own in-memory `StrategyStatus` (exactly the same lifecycle Phase 10 already tested), never touching decision logic or the broker layer.
- All three example accounts are `execution_mode=SHADOW`; `Dhan`/`ICICI Breeze` remain broker-unavailable, matching Phase 7/8/9's existing posture unchanged.
- The kill switch is a genuine, settable flag (`GET /api/risk/status` reflects it), but is **not yet auto-consulted** by `RiskManager.validate()` — see Known Limitations below for why that's honest, not a bug.

---

## The Strategy Code Did Not Change

```
git status --short trading/algos/
 M trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py   (pre-existing, from Phase 3 — untouched this phase)
 M trading/algos/DoubleStraddelAlgo/nifty_token.csv               (pre-existing, a Phase 5A data refresh — untouched this phase)
```

No new modification to any file under `trading/algos/*`.

---

## Files Created
```
trading/api/execution_state.py
trading/api/execution_routes.py
tests/api/test_execution_routes.py
docs/phase-11-control-center-backend-report.md
```

## Files Modified (all additive)
```
trading/api/app.py                    (+execution_router include, +app.state.execution bootstrap)
trading/api/security/audit.py         (+5 new audit action constants: STRATEGY_STARTED, STRATEGY_STOPPED, ASSIGNMENT_SET, KILL_SWITCH_ENGAGED, KILL_SWITCH_DISENGAGED)
trading/common/broker_manager.py      (+broker_ids(), +broker_status() -- read-only, additive)
trading/common/risk_manager.py        (+get_limits() -- read-only, additive)
```
No `trading/api/routes.py`, `trading/api/security/permissions.py`, or any existing route's behavior was changed. No production/strategy files were modified.

---

## Tests
```
python -m pytest tests/api/test_execution_routes.py -v   → 43 passed
python -m pytest tests/ -q                                → full repository suite (see final count below)
```

`test_execution_routes.py` covers: authentication (401 unauthenticated, service key can read), every endpoint's RBAC boundary (viewer/trader/operator/admin/service, matching existing role bundles), validation (unknown strategy/account → 404, invalid/mismatched execution_mode → 422, unavailable broker → 409, already-active/inactive strategy → 409), audit logging (start/stop/assignment/kill-switch engage+disengage/permission-denial all produce the expected `audit_log` row), the credential-exclusion structural check, the no-`get_broker()`-call structural check, and per-app state isolation between tests.

---

## Known Limitations

1. **`GET /api/execution/orders`/`positions` are empty and `pnl` is zeroed, honestly** — no strategy generates a non-empty `OrderIntent` list yet (Phase 10's deliberate scope), so there is nothing real to report. The response schemas (`OrderOut`, `PositionOut`, `PnlOut`) already model the intended real shape for when a future phase wires actual intent flow through `RiskManager` → `StrategyExecutionEngine` and records outcomes somewhere this API can read.
2. **The kill switch is not yet auto-consulted by any live validation path** — `RiskContext.kill_switch_engaged` is caller-supplied by design (`trading/common/risk_manager.py`'s own architecture), and nothing in Phases 1-10 currently calls `RiskManager.validate()` automatically at all (`generate_order_intents()` returns `[]` for every strategy). The API's kill switch is therefore a genuine, observable, settable flag — not yet a functioning safety interlock, because there is no live intent flow for it to interlock against yet. Wiring `state.kill_switch.engaged` into a real `RiskContext` is natural follow-up work once such a flow exists.
3. **No persistence** — `ExecutionState` (accounts/assignments/risk limits/kill switch) lives only in the FastAPI process's memory, rebuilt fresh on every restart. This matches every Phase 1-10 object's own existing (lack of) persistence; a future phase could back `StrategyAssignment`/`RiskLimits`/kill-switch state with a DB table if durability across restarts is needed.
4. **Risk profile → RiskLimits mapping remains undefined**, exactly as Phase 7 already documented — `Assignment.risk_profile` is still a plain label, not yet resolved to a concrete `RiskLimits` preset.

## Recommendation for a Future Phase
Once a future phase wires `StrategyRegistry.generate_all_order_intents()` into a real `RiskManager` → `StrategyExecutionEngine` loop (Phase 10's own recommendation), extend that loop to read `state.kill_switch.engaged` into each `RiskContext`, and back the `/api/execution/orders|positions|pnl` endpoints with whatever store records real (shadow-only, initially) execution outcomes.

STOP after this report — no live trading was enabled at any point, and no live algo or existing API behavior changed.
