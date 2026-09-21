# Phase 16.11 — TCC Operations Dashboard, Monitoring, Alerts & Audit Integration

## 1. Objective

Turn the Trading Control Center into a single, read-only operational
console over the entire distributed trading platform (workers, strategies,
accounts, portfolio risk, alerts, audit, deployment) before real
multi-EC2 deployment (Phase 16.12). This phase is observability/operations
only: no live trading, no real worker networking, no strategy auto-start,
no weakened safety gate.

## 2. Discovered architecture (read before implementing)

- **`/api/health` / `/api/ready`** (`trading/api/health.py`) already
  compute full deployment metadata via `trading/common/deployment_info.py`
  `get_deployment_info()` (app_version, git_sha, deployment_id,
  environment, startup_timestamp) and a DB-reachability check
  (`_check_database()`). Reused verbatim — no second deployment-metadata
  computation was written.
- **`WorkerRegistry`** (`trading/common/worker_registry.py`) already
  lazily recomputes ONLINE→OFFLINE on heartbeat timeout
  (`_recompute_status_locked`) but had **no transition hook at all** — no
  audit call, no alert, nothing. This phase adds an optional `audit_trail`
  parameter and audits exactly the transitions Section 14 asked for, at
  the exact point each transition already happens in the existing code —
  no new state machine.
- **`StrategyLifecycleView`** (`trading/common/strategy_lifecycle.py`) has
  no `worker_id`/`runtime_state`/`market_data_status` fields — those are
  bolted on only at the `trading/api/execution_routes.py` DTO layer for
  the existing `/api/strategy-lifecycle` route. This phase's
  `operations_snapshot.py` follows the exact same enrichment pattern
  (`check_all_lifecycles()` + `StrategyRuntime.get_status()` +
  `WorkerRegistry.get_strategy_owner()`), never duplicating the
  underlying lifecycle computation.
- **`AlertManager`** (`trading/common/alerts.py`, Phase 13) is an
  append-only log of 8 fixed alert types with no active/resolved concept
  at all — confirmed no equivalent "is this currently active" model
  exists anywhere. This is exactly the gap Section 18 asks to fill, via a
  new, separate `OperationalAlertStore` (see Section 5) rather than
  changing `AlertManager`'s own, different responsibility.
- **`AuditTrail`/`PersistentAuditTrail`** (`trading/common/observability.py`,
  `audit_store.py`) already give `.records()`/`.trace()`/time-range and
  event-type queries; `PersistentAuditTrail` already auto-stamps every
  record with `deployment_id`/`app_version`/`git_sha`/`environment`. No
  `by_worker_id`/true SQL pagination exists — the existing
  `/api/observability/audit` route's own pattern (fetch, then Python
  `[-limit:]` slice) is reused as-is for the new operations routes, with
  the SAME known scalability caveat inherited, not introduced.
- **`ExecutionState`** (`trading/api/execution_state.py`) already holds
  every collaborator an operations snapshot needs (workers, strategies,
  accounts via broker_manager, kill_switch, strategy_runtime,
  portfolio_risk_manager) — confirmed zero new shared state was required
  beyond the new `OperationalAlertStore` itself.
- **`Permission.VIEW`** is the correct, existing permission for every new
  read-only route (matches every other GET route in the file).
- **`STRATEGY_COMMAND_ACCEPTED/REJECTED/NOOP/FAILED`** (Phase 16.4) already
  fully audit the modern `/api/strategy-lifecycle/{id}/command` endpoint's
  outcomes, and `STRATEGY_RUNTIME_EVALUATED` (Phase 16.5) already records
  evaluation success/failure via its `outcome=` field. Section 15's
  requirements were therefore **already satisfied** except for one real
  gap: the older, legacy `/api/strategies/{id}/start|stop` routes audited
  only success, never a 409 rejection. Fixed by reusing the EXISTING
  `STRATEGY_COMMAND_REJECTED` constant on that path too — no new audit
  constant was invented.
- **Frontend**: `frontend/src/routes.tsx`'s flat `NAV_ROUTES` array is the
  only place pages are registered (no separate sidebar component);
  `Tcc<Name>Page.tsx` + `.test.tsx` is the established naming pattern;
  `POLL_INTERVAL_MS` (`frontend/src/lib/config.ts`) is the one centralized
  polling constant, reused by every new hook here exactly as
  `useRiskStatus`/`usePortfolioRisk` already do.

## 3. OperationsSnapshot design

`trading/common/operations_snapshot.py`'s `build_operations_snapshot()` is
a pure assembly function: it takes the exact collaborator objects
`ExecutionState` already holds (never `ExecutionState` itself, to keep
this a `trading.common` module with no dependency on the `trading.api`
layer) and returns an immutable `OperationsSnapshot` dataclass with
`system` / `workers` / `strategies` / `accounts` / `portfolio_risk` /
`safety` / `active_alerts` sections. It reuses, rather than recomputes:

- `check_all_lifecycles()` for lifecycle/authorization/execution facts.
- `StrategyRuntime.get_status()` for runtime state / market-data status
  (Phase 16.5/16.6, unchanged).
- `WorkerRegistry.list_workers()` / `get_strategy_owner()` for worker
  placement (Phase 16.9, unchanged).
- `PortfolioRiskManager.get_snapshot()` for portfolio risk numbers (Phase
  16.10's own side-effect-free read, unchanged).
- `get_deployment_info()` for version/environment/uptime.

No new position/P&L/exposure computation was introduced. The ONE thing
this module writes is the `OperationalAlertStore` reconciliation (Section
7) — deliberately NOT authoritative trading state (see the module's own
docstring): it can only ever raise or resolve a diagnostic alert about
facts that are already true elsewhere, never make anything become true.

## 4. Worker monitoring

`WorkerHealthView` (in the snapshot) and its `WorkerHealthOut` API DTO
carry exactly the fields the brief asked for: `worker_id`, `name`,
`session_id`, `status`, `last_heartbeat_at`, `heartbeat_age_seconds`
(computed fresh from `now - last_heartbeat_at`), `heartbeat_timeout_seconds`,
`assigned_strategy_ids`, `version`, `git_sha`, `host_identity`,
`started_at`, and a derived `version_mismatch` boolean (worker's own
`git_sha` vs. the TCC's own `git_sha` from `deployment_info`). Status
remains exactly Phase 16.9's five-value vocabulary
(REGISTERED/ONLINE/OFFLINE/DEGRADED/STOPPED) — no second worker state
machine was created.

## 5. Strategy monitoring

`StrategyHealthView` keeps Lifecycle / Runtime / Authorization / Execution
as four visibly separate fields (`lifecycle_state`, `runtime_state`,
`account_authorization_state`, `execution_active`), exactly matching the
brief's explicit instruction never to merge them into one generic
"ACTIVE" value — the frontend renders each in its own table column.
`worker_id`/`worker_status`/`account_id`/`market_data_status`/
`last_market_data_at` make placement and market-data health visible per
strategy, all read from existing sources (Section 3).

## 6. Market-data monitoring

Reuses `MarketDataStatus`'s existing five-value vocabulary
(AVAILABLE/NO_DATA/STALE/INVALID/PROVIDER_ERROR, confirmed via
`trading/common/market_data_gateway.py`) via `StrategyRuntime`'s own
`_last_market_data_status`/`_last_market_data_at` bookkeeping (already
computed and already exposed at the `/api/strategy-lifecycle` DTO layer
since Phase 16.6) — no new market-data health model was built. Alerts
`MARKET_DATA_STALE`/`MARKET_DATA_MISSING`/`MARKET_DATA_ERROR` map
directly onto STALE / NO_DATA / (INVALID, PROVIDER_ERROR) respectively.

## 7. Portfolio-risk monitoring

`OperationsSnapshot.portfolio_risk` is Phase 16.10's own
`PortfolioRiskSnapshot`, embedded verbatim (not re-derived). The
dashboard explicitly labels Unrealized P&L and delta-adjusted exposure as
**UNSUPPORTED** in its own copy (matching Phase 16.10's own documented
limitations), rather than ever substituting zero for unavailable data.

## 8. Risk status classification

`risk_status: "HEALTHY"` is hard-coded in `PortfolioRiskOut`/account/
strategy risk DTOs today (Phase 16.10's own pre-existing behavior,
unchanged by this phase) — it is explicitly a presentation label, never a
second risk-decision engine; the authoritative decision remains
`PortfolioRiskManager`/`RiskManager`, both fully unmodified by this
phase's monitoring layer. A future phase could compute WARNING/
LIMIT_REACHED/BLOCKED from utilization ratios without touching either
decision engine — left as documented future work (Section 18).

## 9. OperationalAlertStore

`trading/common/operational_alerts.py`'s `OperationalAlertStore` is a new,
small, separate model (see its own docstring for why it does not replace
`AlertManager`): identity = `(code, source_type, source_id)`,
`raise_alert()` is idempotent (an already-active identity is returned
unchanged — no duplicate, no timestamp bump, which is exactly what
prevents "Worker offline #1/#2/#3..." spam under repeated polling),
`resolve()` is a safe no-op when nothing is active. `active_alerts()`
gives the current dashboard view; `all_alerts(limit=...)` gives a bounded
(500-entry cap), newest-first history including resolved alerts. When an
`audit_trail` is supplied, every raise/resolve is durably recorded there
as `ALERT_RAISED_<code>`/`ALERT_RESOLVED_<code>`.

## 10. Alert deduplication and resolution

Deduplication is structural (Section 9's identity key), proven by a
dedicated test (`test_raise_alert_is_idempotent_no_duplicate_spam`) that
calls `raise_alert()` 10 times and asserts exactly one active alert with
a stable `alert_id`. Resolution happens two ways:

- **Poll-driven** (`operations_snapshot._reconcile_alerts()`, called on
  every `build_operations_snapshot()`): `WORKER_HEARTBEAT_LOST`/
  `WORKER_OFFLINE`/`WORKER_VERSION_MISMATCH`/`KILL_SWITCH_ENGAGED`/
  `SYSTEM_NOT_READY`/`STRATEGY_RUNTIME_FAILED`/`MARKET_DATA_*`/
  `ACCOUNT_DISABLED`/`ACCOUNT_KILLED` are raised or resolved purely from
  current authoritative facts — every call either raises exactly the
  alerts whose condition is currently true and resolves every other
  tracked alert whose condition is no longer true.
- **Event-driven** (`WorkerCoordinator`, Section 11): `PORTFOLIO_RISK_BLOCKED`/
  `EXECUTION_REJECTED`/`EXECUTION_FAILED` are raised/resolved exactly at
  the moment a real decision is made (an allowed submission resolves both
  `PORTFOLIO_RISK_BLOCKED` and `EXECUTION_REJECTED`/`FAILED` for that
  strategy).

## 11. Portfolio-risk and execution-outcome audit integration (Section 13)

`WorkerCoordinator` gained two new optional constructor parameters,
`audit_trail` and `operational_alerts` (both default `None`, so every
pre-existing caller/test sees zero behavior change). `_record_portfolio_risk_decision()`
appends a new `EVENT_PORTFOLIO_RISK_DECISION` (added to
`trading/common/observability.py`'s existing `EVENT_*` vocabulary, exactly
alongside `EVENT_RISK_DECISION`) recording timestamp (implicit via
`AuditTrail.append`), worker_id, strategy_id, account_id, idempotency_key,
the decision (allowed/reason_code/message), violations, conflict
classifications, and every projected exposure figure — for BOTH allowed
and rejected decisions, never only failures. This never influences the
decision itself: `PortfolioRiskManager` remains completely unaware
`WorkerCoordinator` is observing it.

## 12. Worker audit events (Section 14)

`WorkerRegistry` gained an optional `audit_trail` parameter and now
audits exactly the transitions requested — `WORKER_REGISTERED`,
`WORKER_ONLINE` (both on register, and again on a heartbeat-driven
recovery from a non-ONLINE state), `WORKER_HEARTBEAT_LOST` +
`WORKER_OFFLINE` (together, exactly once, at the lazy-recompute moment a
worker's status flips due to timeout — proven never to re-fire on a
second read while still OFFLINE), `WORKER_OFFLINE` (manual
`mark_offline()`), `WORKER_SESSION_REPLACEMENT_REJECTED` (both at
register-while-ONLINE and at heartbeat-session-mismatch), and
`STRATEGY_WORKER_ASSIGNED`/`UNASSIGNED`. A dedicated test
(`test_normal_heartbeats_produce_zero_audit_events`) proves 20 consecutive
normal heartbeats create exactly zero audit records — satisfying the
explicit "do not create excessive heartbeat audit noise" instruction.

## 13. Strategy operational audit (Section 15)

Already substantially satisfied by existing code (Section 2) — the only
gap found and fixed was the legacy `/api/strategies/{id}/start|stop`
routes' missing rejection audit, closed by reusing the existing
`STRATEGY_COMMAND_REJECTED` constant rather than inventing a new one.

## 14. OrderIntent / execution observability (Sections 16–17)

No native "list recent order intents/executions, bounded, ordered"
capability exists anywhere in `IdempotencyStore`/`execution.py` (confirmed
by research). Rather than inventing a second, competing record of
intents, `GET /api/operations/intents` and `GET /api/operations/executions`
are thin, bounded, filtered reads over the SAME `AuditTrail` every
`StrategyExecutionEngine.execute()`/`WorkerCoordinator` call already
writes to — filtering by `event_type == "PORTFOLIO_RISK_DECISION"` and
`"EXECUTION_RESULT"` respectively, newest-first, capped at `limit`
(default 50, max 200). No broker payload is ever exposed — the `detail`
dict is exactly what was already recorded.

## 15. Alert API and audit API (Sections 35–36)

`GET /api/operations/alerts` supports `active_only` (default `True`),
`severity`, and `category` filters, reconciling from current state on
every call before answering. `GET /api/operations/audit` is a convenience
alias over the SAME audit trail `/api/observability/audit` already
serves — not a second audit mechanism — adding a `strategy_id` filter the
underlying store has no dedicated query for today (Python-side filter
after `.records()`, same as the existing route's own pattern). No
alert-resolution mutation endpoint was added: resolution remains
entirely condition-driven (Section 36's own allowance).

## 16. Operations dashboard

`frontend/src/pages/TccOperationsPage.tsx` (new, registered at
`/execution/operations` in `routes.tsx`) renders, from ONE
`GET /api/operations/summary` poll: a top banner (System READY/NOT READY,
Kill Switch ENGAGED/DISENGAGED, Execution mode, Live trading
DISABLED/ENABLED, active-alert counts by severity, deployment
environment/version/git-SHA, uptime), a Workers table, a Strategies table
(Lifecycle/Runtime/Authorization/Execution kept as four separate
columns), a Portfolio Risk summary (with the UNSUPPORTED-metrics caveat
printed on the page itself), a bounded Recent Activity feed (from
`/api/operations/audit`), and an Active Alerts table. No BUY/SELL/PLACE
ORDER/CLOSE POSITION/AUTHORIZE LIVE/GO LIVE/LIVE ORDER control exists
anywhere on the page — proven by a dedicated test that also asserts the
page renders **zero buttons at all**.

## 17. Dashboard refresh and stale-dashboard detection

Polling reuses the existing `usePollInterval(POLL_INTERVAL_MS)` +
`refetchInterval` pattern every other TCC page already uses (Phase 11+) —
no WebSocket/SSE was introduced, matching the explicit instruction not to
add one merely for this phase. Stale-dashboard detection (Section 29) is
handled explicitly: when the summary query's `isError` is true, the page
renders an "OPERATIONS DATA UNAVAILABLE" banner with the last successful
refresh time (`dataUpdatedAt`) instead of continuing to render the
previous (now-stale) summary — proven by a dedicated test that also
confirms the stale strategy table does NOT render underneath the banner.

## 18. Version mismatch (Section 33)

`WorkerHealthView.version_mismatch` compares each worker's own `git_sha`
against the TCC's own (`deployment_info.get_deployment_info().git_sha`);
a mismatch raises `WORKER_VERSION_MISMATCH` (WARNING) via the poll-driven
reconciliation and is shown inline in the Workers table — purely
observational, never blocking execution (per the explicit instruction).

## 19. Security / RBAC (Section 41)

Every new `/api/operations/*` route uses `Permission.VIEW` via the
existing `require_permission()` dependency — the same permission every
other read-only route in this file already uses; no new permission was
introduced, and Phase 15D.7's operator authorization model is untouched.
`test_operations_summary_requires_authentication` proves an
unauthenticated request is rejected (401); every account/worker DTO was
checked for credential leakage (`test_operations_accounts_never_exposes_credentials`,
`test_worker_response_never_contains_a_credential_field`-style patterns).

## 20. Structural safety (Section 42)

`test_operations_modules_never_import_a_broker_adapter_or_sdk`,
`test_operations_modules_never_import_live_authorization`,
`test_build_operations_snapshot_never_mutates_risk_limits_or_starts_a_strategy`
(runs `build_operations_snapshot()` five times and confirms the target
strategy's `StrategyStatus` and the `PortfolioRiskManager`'s own limits
are byte-for-byte unchanged), and
`test_operations_snapshot_module_never_calls_engine_execute_directly`
(source-scans for `.execute(`, `place_order`, `modify_order`,
`cancel_order`) together prove the monitoring layer cannot mutate trading
state, execute an order, or reach a broker. A recording-broker spy test
(reused from Phase 16.9/16.10's own pattern) additionally proves zero
real broker calls occur through the full worker/portfolio-risk/audit/alert
path this phase extended.

## 21. Three-worker operations scenario (Section 38)

`test_three_worker_operations_scenario_isolation_and_reconnect` runs the
exact scenario requested: three workers/strategies, initial snapshot
shows 3 ONLINE / SHADOW execution / zero alerts; Worker B's heartbeat is
allowed to go stale while A and C keep heartbeating — the snapshot then
shows A/C ONLINE, B OFFLINE, a `WORKER_OFFLINE` alert scoped to B only
(A/C unaffected, and A's own strategy remains fully submittable); Worker
B's OLD session is rejected for a new submission attempt; Worker B then
re-registers (new `session_id`, prior assignment preserved) — the
`WORKER_OFFLINE` alert resolves, StrategyB's own lifecycle status is
proven completely untouched by the reconnect (no auto-start), and the
OLD session_id is still rejected afterward (no replay). Zero real broker
mutation throughout (both accounts remain on `PaperBroker`).

## 22. Failure-injection tests (Section 37)

Dedicated tests for each required scenario: worker failure (heartbeat
timeout → OFFLINE + `WORKER_HEARTBEAT_LOST`), market-data failure (a
strategy declaring `required_instruments()` with no quote configured →
`NO_DATA` + `MARKET_DATA_MISSING`), kill-switch engagement (blocks a
worker submission AND is visible in the snapshot + alert), and strategy
failure (a strategy whose `generate_order_intents()` raises → `RuntimeState.FAILED`
+ `STRATEGY_RUNTIME_FAILED`) — plus the pre-existing portfolio-risk/
RiskManager rejection tests (Section 11) covering the "risk rejection"
scenario. No real broker mutation in any of them.

## 23. Tests

- `tests/common/test_operational_alerts.py` (8 tests): idempotent raise,
  resolve, re-raise-after-resolve, distinct identities don't collide,
  bounded newest-first history, audit-trail integration, safe default
  with no audit_trail.
- `tests/common/test_phase_16_11_operations.py` (19 tests): WorkerRegistry
  audit events (register/heartbeat-timeout/mark_offline/duplicate-session/
  recovery/assign/unassign, zero heartbeat-spam), portfolio-risk decision
  audit on both allow and reject, execution-outcome alerts
  (PORTFOLIO_RISK_BLOCKED and EXECUTION_REJECTED raise-then-resolve),
  four failure-injection scenarios, the three-worker operations scenario,
  and four structural-safety tests.
- `tests/api/test_execution_routes.py`: 13 new tests for the
  `/api/operations/*` routes (authentication required, summary reports
  real state, workers/strategies/accounts slices, credential-leakage
  check, kill-switch alert appears/disappears via the API, worker-offline
  alert appears/resolves via the API, severity/category filters,
  audit strategy_id filter, bounded intents/executions, no mutation verb
  in any response body) plus the pre-existing `ExecutionState
  .__dataclass_fields__` structural test updated for the new
  `operational_alerts` field (the same incremental update every phase
  since 16.5 has made to this test).
- `frontend/src/pages/TccOperationsPage.test.tsx` (9 tests): loading
  state, stale-dashboard "OPERATIONS DATA UNAVAILABLE" banner (with last
  successful refresh time, and proof the stale table does not render
  underneath it), system/kill-switch/execution-mode/deployment rendering,
  worker placement with heartbeat age, lifecycle/runtime/authorization/
  execution kept visually distinct, portfolio risk with the UNSUPPORTED
  caveat printed, empty and populated active-alerts states, and a
  structural test proving the page renders zero buttons at all.

## 24. Test results

- **Targeted Phase 16.11 sweep**: all new/updated files, `EXIT=0` (one
  structural test — `ExecutionState.__dataclass_fields__` — initially
  failed after adding the `operational_alerts` field and was fixed
  immediately, the same incremental-update pattern every prior phase
  since 16.5 has followed for this exact test).
- **Full backend regression**: **1982 passed, 6 skipped, 0 failed, 0
  errors**, `PYTEST_EXIT=0` — 39 more than the stated pre-Phase-16.11
  baseline of 1943, consistent with the new test files; skip count (6)
  unchanged. The recurring `PytestUnhandledThreadExceptionWarning` lines
  from the pre-existing fake `AngelOne.orderBook()` polling thread are
  the same known, benign, pre-existing artifact seen in every prior
  phase's full run.
- **Frontend**: 10 files, 78 tests, all passed cleanly (no flake observed
  this run).
- `npx tsc --noEmit`: clean, exit 0.
- `npm run build`: succeeded, exit 0.

## 25. Limitations / remaining Phase 16.12 prerequisites

- **Alert persistence**: the active/resolved alert LIST itself is
  in-memory only for this phase (matches every other `ExecutionState`
  collaborator's default) — it resets on restart. When `AUDIT_DB_PATH` is
  configured (Phase 15D-AUDIT's existing `PersistentAuditTrail`), every
  raise/resolve IS durably recorded there, so the HISTORY survives a
  restart even though the "is it currently active" view does not. No
  Redis/Kafka/etc. was added, per the explicit instruction.
- **Alert notifications** (Telegram/email/SMS): not implemented — this
  phase builds detect/deduplicate/display/resolve/audit only, exactly the
  core requirement the brief specified; external delivery remains future
  work.
- **`risk_status` is a hard-coded "HEALTHY"** label today (Phase
  16.10's own pre-existing behavior) — a real WARNING/LIMIT_REACHED/
  BLOCKED classification derived from utilization ratios is future work,
  and must remain presentation-only when built (never a second decision
  engine).
- **`/api/operations/audit`'s `strategy_id` filter is a Python-side
  filter** over `AuditTrail.records()` (inherits the existing route's own
  pattern and its known scalability caveat for `PersistentAuditTrail` —
  not introduced by this phase, not fixed by it either).
- **This phase makes the future EC2 topology observable, but does not
  build it**: no real worker network transport, no real separate worker
  process, no EC2 resources — exactly the Phase 16.12 boundary the brief
  drew. Phase 16.12 should be able to point real, network-connected
  workers at this same `WorkerRegistry`/`WorkerCoordinator`/
  `OperationalAlertStore` and have this dashboard already show them
  correctly, with no changes needed to this phase's own code.

## Safety statement

No live order was placed. No LiveAuthorization was granted or consumed.
No real broker connection or mutation occurred (proven by a
recording-spy broker test). No strategy was automatically started (proven
explicitly in the three-worker reconnect scenario). No risk limit was
mutated by reading the dashboard (proven by a dedicated test). Nothing
was deployed to production, and no AWS/EC2 resource was created. Every
new alert/audit side effect is diagnostic bookkeeping only — none of it
can influence a RiskManager, PortfolioRiskManager, kill switch, or
authorization decision.
