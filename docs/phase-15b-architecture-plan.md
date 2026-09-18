# Phase 15B — Architecture Plan (read before any code change)

## 1. Current architecture

The broker-agnostic pipeline already exists, built across Phases 1-14.7:

```
Strategy -> OrderIntent -> StrategyAssignment (strategy_id -> account_id)
         -> RiskManager -> LiveCanaryGuard/CentralKillSwitch (LIVE_CANARY/LIVE only)
         -> StrategyExecutionEngine.execute() -> BrokerManager (account_id -> BrokerClient)
         -> BrokerClient (per-broker adapter) -> real broker
```

Key existing pieces (all read this phase, not modified yet):

- `trading/common/order_intent.py` — `OrderIntent` is already fully broker-independent (symbol/exchange/side/quantity/order_type, `account_id` as informational metadata only).
- `trading/common/trading_account.py` — `TradingAccount(account_id, account_name, broker_id, enabled, execution_mode, credential_reference, connection_state, broker_client)`. **Multiple accounts already conceptually supported**: `account_id` is the key, `broker_id` is just a string field, so two accounts can share `broker_id="angelone"`.
- `trading/common/broker_manager.py` — `BrokerManager` already resolves `account_id -> TradingAccount -> broker_id -> BrokerClient`, lazily connecting via a per-account factory. Already supports N accounts and N broker types.
- `trading/common/strategy_assignment.py` — `StrategyAssignment` already binds `strategy_id -> account_id` (never `strategy_id -> broker`), with validation that the account is enabled and its broker is available.
- `trading/common/broker.py` — `BrokerClient` ABC (connect/disconnect/is_connected/get_quote/place_order/cancel_order/get_positions), plus a string-keyed `create_broker(config) -> BrokerClient` factory that already fails closed (`ValueError`) on an unknown broker name.
- Adapters: `AngelOneBroker`, `DhanBroker`, `ICICIBreezeBroker`, `ZerodhaKiteBroker`, `PaperBroker`, plus test-only `ShadowBroker`/`ConnectedShadowBroker`. Every real adapter has a `read_only` flag checked before any mutating call, independent of `TRADING_MODE`.
- `trading/common/risk_manager.py` — `RiskLimits` already has an (unused-for-routing) `account_id` field on `RiskCheckResult`; `RiskManager` enforces `max_order_quantity/value/exposure/daily_loss/strategy_loss/orders_per_day`, LIVE-readiness (`is_live_ready()`), and is otherwise broker/account agnostic.
- `trading/common/live_canary.py`, `trading/common/kill_switch.py`, `trading/common/idempotency_store.py`, `trading/common/observability.py`, `trading/common/broker_response_validation.py` — Phase 14/14.6/14.7 safety gates. Not modified in this phase.

## 2. Current broker coupling

- Every real adapter constructor takes a single `TradingConfig` object (`trading/common/config.py`), and reads credentials from `TradingConfig.credentials` (`BrokerCredentials`), whose fields are populated **once, from fixed, broker-scoped env-var names** (`ANGELONE_API_KEY`, `ANGELONE_CLIENT_ID`, `ANGELONE_PASSWORD`/`ANGELONE_MPIN`, `ANGELONE_TOTP_SECRET`; similarly for Dhan/ICICI/Zerodha).
- `BrokerCredentials`/`TradingConfig` are plain (not singleton) dataclasses — nothing prevents constructing a second one — but **nothing today parameterizes *which* env-var prefix to read per account**. This is the one genuine architectural gap: two `TradingAccount`s with `broker_id="angelone"` would, if both built via `create_broker(config)` today, resolve to the *same* env vars and therefore the *same* real login. There is no `ANGEL_SAMIR` vs `ANGEL_WIFE` credential separation mechanism yet.
- `create_broker()` is keyed off `config.broker_name` (a single global string), not off a per-account `BrokerType`/`broker_id` — it assumes there is exactly one broker configured process-wide, which conflicts with "multiple brokers can exist simultaneously."

## 3. Current account assumptions

- `TradingAccount` already models one specific login (not "a broker" generically) — this part of the target model already exists.
- No `owner_id` field exists yet (who this account belongs to) — needed for cross-account isolation checks (Samir vs wife).
- No explicit authorization-state machine (`DISABLED/READ_ONLY/CANARY_READY/LIVE_AUTHORIZED/KILLED`) — today authorization is implicit, split across `enabled: bool`, `execution_mode`, an adapter's own `read_only` flag, and `TradingConfig.is_live`.
- `RiskManager` holds exactly **one** `RiskLimits` for its whole lifetime — not one per account. Account-specific risk isolation does not exist yet.
- No normalized `AccountState` — funds/positions/orders/trades are separate ad hoc adapter calls with broker-specific return types (`FundsSnapshot`, `Position`, adapter-specific order rows).
- No `BrokerCapabilities` registry — nothing declares "Dhan does not support live orders yet" structurally; that is only true today because `DhanBroker.read_only` defaults to `True` and its safety comments say so in prose.

## 4. Existing reusable components (keep, do not rewrite)

`OrderIntent`, `BrokerClient` ABC, `BrokerManager`, `StrategyAssignment`, `RiskManager`'s check pipeline, all Phase 14/14.6/14.7 safety gates, every adapter's internal Angel/Dhan/ICICI-specific logic, `create_broker()` (kept as-is for existing single-account callers/tests — never touched).

## 5. Required refactoring (minimal, additive)

1. **Per-account credential resolution** — a `credential_reference` -> `BrokerCredentials` resolver, so two accounts of the same broker type can hold two independent credential sets. Additive: a new function, `create_broker()` untouched.
2. **`BrokerType` enum + `BrokerCapabilities` registry** — formalizes broker identity and declares capabilities (`requires_static_ip`, `supports_live_orders`, ...) without touching adapter internals.
3. **`BrokerAdapterFactory`** — `BrokerType -> adapter class`, fails closed for unknown/unregistered types. Wraps `create_broker`'s existing string dispatch; does not replace it.
4. **`TradingAccount` additions** — `owner_id`, `display_name`, `account_type`, `read_only`, and a new `authorization_state` enum field (`AccountAuthorizationState`), defaulting to the safest state. Additive dataclass fields with defaults — no existing construction call breaks.
5. **`AccountState`** — a new normalized dataclass + a converter that any adapter's funds/positions/orders can be folded into. Additive.
6. **`TradingAccountRouter`** — a thin, explicit read-only-operation router on top of the existing `BrokerManager`, adding owner-scoped resolution and authorization-state checks. Additive; `BrokerManager` itself is untouched.
7. **Per-account risk limits in `RiskManager`** — an optional `account_limits: dict[str, RiskLimits]` alongside the existing single `limits`, resolved per intent's `account_id`, falling back to the existing single `limits` when not present. Zero behavior change for any caller that doesn't pass the new argument.

## 6. Proposed architecture

```
Strategy -> OrderIntent
         -> RiskManager (per-account RiskLimits, resolved via account_id)
         -> TradingAccountRouter (owner isolation, authorization_state gate)
         -> TradingAccount (credential_reference -> resolved BrokerCredentials)
         -> BrokerAdapterFactory (BrokerType -> adapter class, fail-closed)
         -> BrokerClient (existing Angel/Dhan/ICICI/Zerodha/Paper adapters, untouched internals)
         -> real broker
```

Existing `LiveCanaryGuard` / `CentralKillSwitch` / persistent idempotency / broker-response validation sit exactly where they already do, between `RiskManager` and the broker call, inside `StrategyExecutionEngine.execute()` — untouched.

## 7. Migration strategy

- No existing call site is required to change. `credential_reference=""` (the current default for every existing `TradingAccount`) resolves to exactly the same env vars `create_broker()` reads today — so all current tests/behavior are unaffected.
- New capability (a second real account of the same broker) is opt-in: only exercised by a caller that sets a non-empty, distinct `credential_reference` and constructs the account via the new `create_broker_for_account()` helper.
- `authorization_state` defaults to `READ_ONLY` for any newly constructed `TradingAccount` that doesn't specify it, which is at least as safe as today's implicit behavior (nothing today auto-authorizes live trading either).

## 8. Backward-compatibility risks

- Adding fields to `TradingAccount`/`RiskLimits`-adjacent structures: mitigated by giving every new field a default and never renaming/removing an existing field.
- `RiskManager`'s new `account_limits` param: mitigated by making it optional and never consulted unless populated.
- Risk: a future caller assumes `authorization_state=LIVE_AUTHORIZED` is required for `LIVE_CANARY`/`LIVE` execution, but `StrategyExecutionEngine.execute()` does not check it in this phase (that wiring is explicitly deferred — see Section 11 "Items intentionally NOT changed"). This is called out as a known limitation, not silently glossed over.

## 9. Test strategy

- Unit tests for every new module (`broker_types`, `credentials`, `broker_adapter_factory`, `account_state`, `account_router`, `trading_account` additions, `risk_manager` per-account limits).
- A credential-isolation test proving two fake/mocked Angel One accounts resolve to two disjoint credential sets, using synthetic env vars — never real secrets, never asking the user for any.
- A no-live-order test proving every adapter's mutating methods remain blocked through the new router/factory path.
- Full existing regression suite re-run unchanged.

## 10. Security risks

- `credential_reference` is a pointer, never a secret — enforced by the resolver only ever reading env vars, never accepting a literal secret value as the reference itself. Reviewed to ensure no new code path prints/logs a resolved `BrokerCredentials`.
- Cross-account credential leakage is the primary risk this phase must close (Samir's session must never be reachable from `ANGEL_WIFE`) — addressed by resolving credentials strictly from the requesting account's own `credential_reference`, with a test proving isolation.

## 11. Items intentionally NOT changed

- `StrategyExecutionEngine.execute()` is **not** modified to consult `authorization_state` — wiring the new state machine into the actual execution gate is real, safety-relevant execution-path surgery, which carries the same risk-of-regression the existing Phase 14.6/14.7 gates were hardened against, and is out of this phase's own stated scope ("architectural and read-only phase"). The new state machine is built and tested standalone; wiring it into `execute()` is left as documented future work, not silently deferred.
- Dhan/ICICI Breeze adapters' internal order-placement logic is not touched — only their capability declarations and how their credentials are resolved.
- No real order is placed, no live trading is enabled, no existing safety gate is weakened.

No architectural conflict was found that would require stopping before implementation — proceeding.
