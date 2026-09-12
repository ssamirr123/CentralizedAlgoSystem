# Phase 13 — Observability

```text
Strategy -> OrderIntent -> Risk Decision -> Execution -> Broker Order -> Fill -> Position -> P&L
   |             |               |               |             |          |         |        |
   +----- every stage appends to ONE hash-chained AuditTrail, keyed by intent.correlation_id ----+
```

Two new, pure `trading/common/` modules — `observability.py` (metrics + tamper-evident audit trail) and `alerts.py` (the 7 named alert types) — instrument the existing Phase 1-12 pipeline. Every collaborator that can emit an event (`StrategyExecutionEngine`, `BaseStrategy`, `BrokerManager`) takes `metrics`/`audit_trail`/`alerts` as **optional, keyword-only constructor parameters defaulting to `None`** — Phase 13 is purely additive instrumentation. No existing test, no existing caller, and no live algo file needed to change for this phase to exist.

---

## `trading/common/observability.py`

- **`MetricsRegistry`** — thread-safe counters/gauges/status-strings/bounded-latency-samples, with one named helper per metric this phase's brief lists: `record_strategy_heartbeat`, `record_broker_heartbeat`, `record_execution_latency`, `record_order_intent`, `record_order_approved`, `record_order_rejected`, `record_simulated_fill`, `record_real_fill`, `record_error`, `record_pnl`, `record_exposure`, `record_position`, `record_account_status`. Every counter/gauge is recorded both per-entity (`order_intents:DoubleStraddelAlgo`) and as a fleet-wide `_all` aggregate.
- **`AuditTrail`** — append-only, **hash-chained** event log: every record's SHA-256 hash commits to its own content *and* the previous record's hash (the same construction git commits and blockchains use). `verify()` recomputes every hash from scratch and returns `False` if any record was modified, reordered, or deleted after the fact.

### "An audit trail that cannot be silently modified" — precisely what this guarantees

This is an in-process Python object; no purely in-memory, single-process structure can make it *physically impossible* for that same process to mutate its own memory. What the hash chain provides is **tamper evidence**: any modification, reordering, or deletion of a past record is immediately, deterministically detectable via `verify()` — the realistic, standard meaning of "cannot be silently modified" for an application-level audit trail (the exact guarantee a git history provides: rewriting history is detectable, not physically prevented). This is proven, not just asserted: `tests/common/test_observability.py` directly mutates a past record's `detail` dict, reorders two records, and deletes a record — `verify()` returns `False` in all three cases, and `verify_or_raise()` raises `TamperDetectedError`.

`GET /api/observability/audit/integrity` exposes this check live over the API.

---

## `trading/common/alerts.py` — the 7 named alert types

`AlertManager` — one method per requested alert, each a thin wrapper over a shared `_raise()` core: `strategy_stopped`, `broker_disconnected`, `risk_breach`, `daily_loss_limit`, `unexpected_position`, `execution_error`, `kill_switch`. Every alert carries a severity (`INFO`/`WARNING`/`CRITICAL`), is logged through the structured JSON logger, and — when the `AlertManager` was constructed with an `AuditTrail` — is **also** appended there as `ALERT_<TYPE>`, so an alert is never a dead end: it's traceable by `correlation_id` alongside the rest of that order's lifecycle and survives after `AlertManager`'s own bounded in-memory deque rotates it out.

Alerts are purely observational — raising one never blocks, retries, or changes the action that triggered it, mirroring `trading/api/security/audit.py`'s own "auditing must never break the request" discipline.

---

## Wiring: every stage of the requested lifecycle

| Stage | Where | What's recorded |
|---|---|---|
| Strategy heartbeat | `BaseStrategy.start()`/`generate_order_intents()` | `strategy_heartbeat:<id>` gauge; `STRATEGY_STARTED`/`STRATEGY_STOPPED` audit events; `strategy_stopped` alert on `stop()` |
| Broker heartbeat / account status | `BrokerManager.get_broker()` | `broker_heartbeat:<broker_id>` gauge; `account_status:<id>` on connect/error; `BROKER_CONNECTED`/`BROKER_DISCONNECTED` audit events; `broker_disconnected` alert on connect failure |
| OrderIntent count | `StrategyExecutionEngine.execute()` | `order_intents:<strategy>` counter; `ORDER_INTENT_CREATED` audit event |
| Risk decision (approved/rejected) | `execute()`, right after `RiskManager.validate()` | `orders_approved`/`orders_rejected` counters; `RISK_DECISION` audit event (every one of the 14 named checks' pass/fail, not just the summary); a `risk_breach` (or `kill_switch`/`daily_loss_limit`) alert per **failed** check |
| Execution latency | `execute()` | `execution_latency:<strategy>` samples, timed from intent receipt to order placement/failure |
| Broker order placed | `execute()` | `BROKER_ORDER_PLACED` audit event, tagged `simulated=<bool>` |
| Fill (simulated vs real) | `execute()` | `simulated_fills`/`real_fills` counters; `FILL` audit event — classified via a new `BrokerClient.is_simulated` class attribute (`False` by default, `True` on `ShadowBroker`/`ConnectedShadowBroker`) rather than an `isinstance` check, so the generic execution engine never has to import a specific broker adapter |
| Errors | `execute()`'s failure paths, `BaseStrategy._mark_error()` | `errors:<component>` counters; `execution_error` alert |
| P&L / exposure / position | `MetricsRegistry.record_pnl/record_exposure/record_position` | Available for a future phase to call once real position/P&L tracking exists (Phase 11's Known Limitation #1) — the metric surface exists and is tested today; nothing currently calls it automatically because nothing in this system tracks live P&L/exposure/positions yet |
| Kill switch | `execute(intent, context)` (new optional `context` param) and `POST /api/risk/kill-switch` | `kill_switch` alert (`CRITICAL` engaged / `INFO` disengaged); `ALERT_KILL_SWITCH` audit event |

**Correlation IDs were already threaded through this pipeline since Phase 1** (`OrderIntent.correlation_id`, carried onto `ExecutionResult` and `RiskCheckResult`) — Phase 13 simply makes every observability event key off the same field, so `AuditTrail.trace(correlation_id)` reconstructs one order's complete journey.

### `StrategyExecutionEngine.execute()` gains an optional `context` parameter

`execute(self, intent, context: RiskContext | None = None)` — passed straight through to `RiskManager.validate()`, which already defaulted it to `RiskContext()` internally. Every existing `execute(intent)` call site is unaffected (identical to passing `context=None`). This is what makes the `KILL_SWITCH` alert **actually reachable**: without a context, `RiskContext()`'s own default (`kill_switch_engaged=False`) means that check always passes, exactly as before. A future phase wiring Phase 11's `ExecutionState.kill_switch.engaged` into a real `RiskContext` at call time is the natural next step (see Known Limitations).

---

## Proof: the full lifecycle is traceable by correlation_id

`tests/common/test_observability_wiring.py` builds the exact same `ConnectedShadowBroker` + `AngelOneBroker(read_only=True)` stack Phase 5B/8/9 already established (fake SmartAPI double, no real network), wires `MetricsRegistry`/`AuditTrail`/`AlertManager` through every collaborator, and runs one `OrderIntent` through `execute()`:

```
audit_trail.trace(intent.correlation_id) == [
    ORDER_INTENT_CREATED, RISK_DECISION, EXECUTION_RESULT, BROKER_ORDER_PLACED, FILL,
]
```

— five stages, one correlation_id, in the exact pipeline order. A separate test proves a risk-rejected intent's trace honestly stops after `RISK_DECISION` (plus its `ALERT_RISK_BREACH`, which shares the same correlation_id). A separate test proves `audit_trail.verify()` stays `True` after a real run, and that the hash chain catches tampering (see above). `fake.placeOrder.assert_not_called()` confirms — again — that no real Angel order API is ever reached.

---

## Observability API (Phase 11 extension)

| Method | Path | Permission |
|---|---|---|
| GET | `/api/observability/metrics` | VIEW |
| GET | `/api/observability/alerts` (optional `?alert_type=`) | VIEW |
| GET | `/api/observability/trace/{correlation_id}` | VIEW |
| GET | `/api/observability/audit` (newest first, `?limit=`) | VIEW |
| GET | `/api/observability/audit/integrity` | VIEW |

All read-only, VIEW-gated (no secret, no mutation — matching every other read endpoint in this router). `POST /api/risk/kill-switch` (Phase 11) now also raises the Phase 13 `kill_switch` alert, and `POST/GET /api/strategies/{id}/start|stop` (Phase 11) now produce real `STRATEGY_STARTED`/`STRATEGY_STOPPED`/`strategy_stopped`-alert events automatically, since `ExecutionState`'s three registered strategies are now constructed with `metrics_registry`/`audit_trail`/`alerts` wired through. These are the two alert types genuinely reachable via the live API today; the rest (`risk_breach`, `daily_loss_limit`, `unexpected_position`, `execution_error`, `broker_disconnected`) are demonstrated by direct tests since no API route yet drives a real `execute()` call or `get_broker()` connection (Phase 11's own documented scope).

`ExecutionState` (Phase 11) gained three new fields — `metrics: MetricsRegistry`, `audit_trail: AuditTrail`, `alerts: AlertManager` — one instance per FastAPI app, matching every other field's test-isolation rationale.

---

## Files Created
```
trading/common/observability.py
trading/common/alerts.py
tests/common/test_observability.py
tests/common/test_alerts.py
tests/common/test_observability_wiring.py
tests/api/test_observability_routes.py
docs/phase-13-observability-report.md
```

## Files Modified (all additive — new optional kwargs / new class attribute / new endpoints)
```
trading/common/broker.py                          (+BrokerClient.is_simulated = False)
trading/common/brokers/shadow_broker.py            (+is_simulated = True)
trading/common/brokers/connected_shadow_broker.py  (+is_simulated = True)
trading/common/execution.py                        (+metrics/audit_trail/alerts kwargs, +context param on execute(), instrumented execute())
trading/common/strategy.py                         (+metrics_registry/audit_trail/alerts kwargs on BaseStrategy, instrumented start/stop/generate_order_intents/_mark_error)
trading/common/strategies/double_straddle.py       (passthrough kwargs)
trading/common/strategies/combined_vwap_nifty.py   (passthrough kwargs)
trading/common/strategies/vwap_algo_nifty_hedge.py (passthrough kwargs)
trading/common/broker_manager.py                   (+metrics_registry/audit_trail/alerts kwargs, instrumented register_account/get_broker)
trading/api/execution_state.py                     (+metrics/audit_trail/alerts fields, wired into BrokerManager + the three registered strategies)
trading/api/execution_routes.py                    (+5 GET /api/observability/* routes; kill-switch route also raises the alert)
```
No `trading/api/routes.py`, `trading/api/security/permissions.py`, or any existing route/behavior was changed beyond the additive kill-switch-alert line. No production/strategy files were modified.

---

## Tests
```
python -m pytest tests/common/test_observability.py -v          → 17 passed
python -m pytest tests/common/test_alerts.py -v                 → 11 passed
python -m pytest tests/common/test_observability_wiring.py -v   → 8 passed
python -m pytest tests/api/test_observability_routes.py -v      → 10 passed
python -m pytest tests/common/ tests/algos/ -q                  → all passed (verified after every Phase 13 wiring change)
python -m pytest tests/api/test_execution_routes.py -v          → 43 passed (unaffected by ExecutionState's new fields)
python -m pytest tests/ -q                                       → full repository suite (see final line below)
```

`test_observability.py` covers every `MetricsRegistry` helper, snapshot immutability, bounded latency samples, and the `AuditTrail` hash chain (linkage, correlation-id trace filtering, and three distinct tamper scenarios: modified detail, reordering, deletion). `test_alerts.py` covers all 7 alert types, severity assignment, the bounded deque, sequential numbering, and the `AuditTrail` integration. `test_observability_wiring.py` is the end-to-end proof described above. `test_observability_routes.py` covers the API surface and its VIEW-only gating.

---

## Safety Verification

- **No live order was placed, modified, or cancelled** — this phase only observes; nothing here calls a broker or changes what any existing code path already does with an order.
- **No existing caller's behavior changed** — every new parameter across `execution.py`/`strategy.py`/`broker_manager.py`/the three strategy adapters is optional, keyword-only, and defaults to `None`; the full `tests/common/` + `tests/algos/` suite (which constructs these classes hundreds of times without any of the new parameters) passes unchanged.
- **No live algo file was modified** — `git status --short trading/algos/` shows only the two files already modified in earlier phases.
- **No credential or secret is ever recorded** — every event/alert/metric name and detail field is drawn from `OrderIntent`/`RiskCheckResult`/`TradingAccount` fields already established as non-secret in Phases 1-11; nothing here touches `BrokerCredentials`.
- **The audit trail's tamper-evidence is proven, not just claimed** — see the three tamper-detection tests above.

## Known Limitations

1. **`record_pnl`/`record_exposure`/`record_position` are not yet called automatically anywhere** — there is still no live position/P&L tracking anywhere in this system (Phase 11's own Known Limitation #1: `generate_order_intents()` returns `[]` for every strategy today). The metric helpers exist and are tested; wiring them to real data is follow-up work for whichever future phase adds real position/P&L tracking.
2. **`risk_breach`, `daily_loss_limit`, `unexpected_position`, `execution_error`, and `broker_disconnected` are not yet reachable through the live API** — no current route drives a real `execute()` call or a `get_broker()` connection (unchanged from Phase 11's own scope). They are fully implemented and tested directly against `execute()`/`BrokerManager`, ready the moment a future phase wires real intent flow or account connection through the API.
3. **The kill switch is now *reachable* by `execute()`, but still not *automatically* wired** — a caller must explicitly pass a `RiskContext(kill_switch_engaged=...)`; nothing today automatically sources that from `ExecutionState.kill_switch.engaged`. This remains an honest, incremental step past Phase 11's "not yet consulted at all," not a claim of full automatic enforcement.
4. **The audit trail is in-memory only, per FastApi app instance** — like every other Phase 11 `ExecutionState` field, it does not survive a process restart. A future phase adding durable storage (e.g. an append-only table, or periodic export) would extend the same hash-chain construction to also resist a compromised process, not just verify an honest one.

## Recommendation for a Future Phase
Once a future phase wires `StrategyRegistry.generate_all_order_intents()` into a real `execute()` loop (Phase 10/11's own recommendation), pass `RiskContext(kill_switch_engaged=state.kill_switch.engaged, daily_pnl=..., strategy_pnl=...)` at each call site — at that point every alert type in this phase becomes live-reachable through the API with no further code changes, since the wiring already exists end-to-end.

STOP after this report — no live order was enabled at any point.
