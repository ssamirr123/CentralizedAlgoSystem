# Phase 15B — Multi-Account / Multi-Broker Trading Foundation

Architectural, read-only phase. **No real order was placed, no broker mutation API was called, no live trading was enabled.**

## 1. Architecture

```
Strategy -> OrderIntent (broker-independent, unchanged from Phase 1)
         -> StrategyAssignment (strategy_id -> account_id, unchanged from Phase 1/7)
         -> RiskManager (per-account RiskLimits -- NEW: set_account_limits())
         -> TradingAccountRouter (NEW: owner isolation, authorization_state gate, read-only ops)
         -> TradingAccount (NEW: owner_id, authorization_state, read_only, credential_reference)
         -> BrokerAdapterFactory (NEW: BrokerType -> adapter class, fail-closed)
         -> create_broker_for_account() (NEW: per-account credential resolution)
         -> BrokerClient (Angel/Dhan/ICICI Breeze/Zerodha/Paper -- internals unchanged)
         -> real broker
```

Everything left of "RiskManager" and everything at "BrokerClient" and below already existed (Phases 1-14.7) and was **not modified in its own internal logic** this phase. The new pieces sit strictly between them, additively.

## 2. TradingAccount model

`trading/common/trading_account.py` (extended, not replaced). Existing fields (`account_id`, `account_name`, `broker_id`, `enabled`, `connection_state`, `execution_mode`, `credential_reference`, `metadata`, `broker_client`) are unchanged. New fields, all defaulted so no existing construction call anywhere in the codebase needed to change:

| Field | Default | Purpose |
|---|---|---|
| `owner_id` | `""` | Who this account belongs to (e.g. `"SAMIR"`, `"WIFE"`) — consulted by `TradingAccountRouter` for cross-account isolation. |
| `display_name` | `""` | Human label, informational only. |
| `account_type` | `""` | Free-form label (e.g. `"individual"`, `"canary"`), informational only. |
| `read_only` | `True` | The account's own declared read-only posture — every new account starts safe by default. |
| `authorization_state` | `AccountAuthorizationState.READ_ONLY` | See below. |

`AccountAuthorizationState` (new enum): `DISABLED -> READ_ONLY -> CANARY_READY -> LIVE_AUTHORIZED`, plus `KILLED` (irreversible for that instance — `TradingAccount.set_killed()` has no corresponding "un-kill" method, mirroring `LiveCanaryGuard.emergency_shutdown()`'s own design from Phase 14). `TradingAccount.is_live_authorized()` returns `True` only for `LIVE_AUTHORIZED and enabled`.

Example accounts (illustrative only — **not** created anywhere in this codebase):

```
ANGEL_SAMIR: broker_id=angelone, owner_id=SAMIR, credential_reference=env:ANGELONE_SAMIR
ANGEL_WIFE:  broker_id=angelone, owner_id=WIFE,  credential_reference=env:ANGELONE_WIFE
DHAN_SAMIR:  broker_id=dhan,     owner_id=SAMIR, credential_reference=env:DHAN_SAMIR
ICICI_SAMIR: broker_id=icici_breeze, owner_id=SAMIR, credential_reference=env:ICICI_SAMIR
```

## 3. BrokerType and BrokerCapabilities

`trading/common/broker_types.py` (new). `BrokerType` enum (`ANGEL_ONE`, `DHAN`, `ICICI_BREEZE`, `ZERODHA`, `PAPER`) with an explicit `BROKER_ID_TO_TYPE` mapping from the existing free-form `broker_id` strings — `broker_type_for_id()` fails closed (`UnsupportedBrokerError`) for anything not in that map; there is no fallback broker.

`BrokerCapabilities` is a declarative, per-broker registry (`requires_static_ip`, `supports_websocket`, `supports_orders/positions/funds/options/market_data`, `supports_live_orders`). Every broker today has `supports_live_orders=False` — none of Angel One, Dhan, ICICI Breeze, or Zerodha has been verified to place a real order in this codebase yet, and the registry says so explicitly rather than only in prose comments. ICICI Breeze is the only broker with `requires_static_ip=True`, and that requirement is scoped to ICICI specifically — it is never assumed to apply to any other broker.

## 4. Credential isolation

`trading/common/credentials.py` (new) — the core gap this phase closes. Previously, `TradingConfig.credentials` (`trading/common/config.py`) read exactly one fixed set of env vars per broker (`ANGELONE_API_KEY`, `ANGELONE_CLIENT_ID`, ...), so two `TradingAccount`s with the same `broker_id="angelone"` would have resolved to the *same* real login.

`resolve_credentials(broker_type, credential_reference)` reads a **different** env-var prefix per reference:

- `credential_reference=""` (every existing account, unchanged) → the exact same legacy env vars as before (`ANGELONE_API_KEY`, etc.) — zero behavior change.
- `credential_reference="env:ANGELONE_SAMIR"` → `ANGELONE_SAMIR_API_KEY`, `ANGELONE_SAMIR_CLIENT_ID`, `ANGELONE_SAMIR_MPIN`/`ANGELONE_SAMIR_PASSWORD`, `ANGELONE_SAMIR_TOTP_SECRET`.
- `credential_reference="env:ANGELONE_WIFE"` → the equivalent `ANGELONE_WIFE_*` set.

`credential_reference` is never a secret itself — only a pointer to which env-var prefix to read (matching the existing module docstring's security note in `trading_account.py`). Only the `env:` scheme is implemented; anything else raises `ValueError` rather than being silently ignored.

`tests/common/test_credentials.py` proves two accounts of the same broker resolve to fully disjoint credential values, and that an unconfigured second account resolves to empty strings rather than silently falling back to the first account's credentials.

## 5. BrokerAdapterFactory and per-account construction

`trading/common/broker_adapter_factory.py` (new). `BrokerAdapterFactory.build(config, broker_type, read_only=...)` dispatches to the existing adapter classes (`AngelOneBroker`, `DhanBroker`, `ICICIBreezeBroker`, `ZerodhaKiteBroker`, `PaperBroker`) — **their internal implementations were not touched**. `create_broker_for_account(account, base_config)` is the actual per-account entry point: it resolves `account.broker_id -> BrokerType`, resolves `account.credential_reference` into a fresh `BrokerCredentials`, builds a new `TradingConfig` carrying only that account's credentials (via `dataclasses.replace`, since `TradingConfig` is frozen), and constructs the adapter with `read_only=account.read_only` (defaulting to `True`).

`trading.common.broker.create_broker(config)` (the existing single-global-config factory) is completely untouched and still used by every pre-Phase-15B caller.

## 6. Account routing

`trading/common/account_router.py` (new). `TradingAccountRouter` wraps the existing `BrokerManager` (also untouched) with:

- `get_funds(account_id, owner_context=None)`, `get_positions(...)`, `get_orders(...)`, `get_account_state(...)` — **read-only methods only**; there is no `place_order`/`modify_order`/`cancel_order` method on this class in this phase.
- Owner isolation: if `owner_context` is supplied and doesn't match the resolved account's own `owner_id`, `AccountAccessDeniedError` is raised — `router.get_funds("ANGEL_WIFE", owner_context="SAMIR")` cannot resolve to `ANGEL_WIFE`'s data at all, let alone `ANGEL_SAMIR`'s credentials.
- Authorization-state gate: `DISABLED` and `KILLED` accounts are refused for every operation, including reads.

`tests/common/test_account_router.py` proves two accounts resolve independently and that owner-context isolation actually blocks a mismatched request.

## 7. Risk isolation

`trading/common/risk_manager.py` (extended). `RiskManager` still holds one default `RiskLimits` (unchanged constructor signature is still valid — `limits` remains optional and positional-compatible), plus a new optional `account_limits: dict[str, RiskLimits]` and `set_account_limits(account_id, limits)`. Every one of the 8 limit-checking methods (`max_order_quantity`, `max_position_quantity`, `max_strategy_exposure`, `max_account_exposure`, `max_daily_loss`, `max_strategy_loss`, `max_order_value`, `max_orders_per_day`) now resolves its limit via `self._limits_for(intent.account_id)`, which returns the account's own override if one is registered, otherwise the manager's single default — exactly today's behavior for any account with no override. `get_limits()` remains valid with zero arguments (returns the default) and gained an optional `account_id` parameter.

`tests/common/test_risk_manager_account_isolation.py` proves a limit set for one account never leaks to another.

## 8. Strategy assignment

`trading/common/strategy_assignment.py` was **not modified** — it already binds `strategy_id -> account_id` (never `strategy_id -> broker`), already supports arbitrarily many accounts, and already validates the target account is enabled and its broker available. This already satisfied section 16 of the phase brief; no change was needed.

## 9. Angel One integration

`AngelOneBroker` (`trading/common/brokers/angelone.py`) was **not modified**. It already takes a `TradingConfig` and reads `self._config.credentials` — which is exactly the seam `create_broker_for_account()` exploits: pass it a `TradingConfig` built with a specific account's own resolved credentials, and it operates on that account, with zero internal changes. Verified via `tests/common/test_broker_adapter_factory.py`: two `AngelOneBroker` instances built for two different `credential_reference`s carry two independent `TradingConfig.credentials`.

## 10. Dhan integration

`DhanBroker` (`trading/common/brokers/dhan.py`) — not modified. Already has its own `read_only` flag (defaulting to `True`, stricter than Angel's environment-driven default), already isolated behind `BrokerClient`. Routed through the same `create_broker_for_account()` path. `BrokerCapabilities.ANGEL_ONE.supports_live_orders=False` mirrors Dhan's own already-documented "unverified against a real Dhan account" status — nothing new was implemented here, only formalized as a capability declaration.

## 11. ICICI Breeze integration

`ICICIBreezeBroker` (`trading/common/brokers/icici_breeze.py`) — not modified. Same pattern. `BrokerCapabilities.ICICI_BREEZE.requires_static_ip=True` is the one broker-specific compliance fact this phase formalizes generically (section 19): it is declared as data on this broker's own capability record, never assumed to apply to Angel One or Dhan, and the core platform (`BrokerAdapterFactory`, `TradingAccountRouter`) never special-cases it.

## 12. Security model

- Credentials are never logged, printed, or included in any `__repr__` anywhere in the new code (`AccountCredentials`, `BrokerCredentials`) — verified by inspection of every new module.
- `credential_reference` is validated to be a non-secret pointer (`env:<PREFIX>`); the resolver never accepts a literal secret value as the reference itself.
- Owner isolation (`TradingAccountRouter`) prevents one owner's context from resolving another owner's account, closing the specific risk named in the phase brief ("Samir's session must never be reachable via `ANGEL_WIFE`").
- `AccountAuthorizationState.KILLED` is irreversible per-instance — matches the existing `LiveCanaryGuard.emergency_shutdown()` precedent from Phase 14.

## 13. Read-only validation

No new real-account read-only validation script was added this phase — the existing ones (`trading.validation.angel_readonly`, the Phase 15A one-off diagnostic) already prove the single, real, currently-configured Angel One account works read-only end-to-end. Extending that to a second real account is not exercised here because **no second real broker account/credential set exists in this environment** — see Known Limitations. All isolation proofs in this phase use synthetic env vars and in-memory fake brokers (`tests/common/test_credentials.py`, `tests/common/test_account_router.py`), never real credentials, per this phase's explicit instruction not to ask the user for any.

## 14. Future live-execution architecture

**Update (Phase 15B.1 — closed):** `AccountAuthorizationState` is now wired into `StrategyExecutionEngine.execute()`. See `docs/phase-15b-1-authorization-enforcement-report.md` for the full implementation: a new hard gate runs immediately after `TradingAccount` resolution and before `RiskManager.validate()`, requiring `LIVE_AUTHORIZED` for `LIVE` and `CANARY_READY`/`LIVE_AUTHORIZED` for `LIVE_CANARY`, while `DISABLED`/`KILLED` block every mode unconditionally. This gate is strictly additional — `LiveCanaryGuard`, `CentralKillSwitch`, `RiskManager`, `RiskLimits.is_live_ready()`, and idempotency are all unchanged and still independently enforced below it.

## 15. Known limitations

1. ~~`StrategyExecutionEngine.execute()` does not consult `authorization_state` yet`~~ — **closed in Phase 15B.1**, see Section 14 above.
2. **No second real broker account exists to validate against** — the credential-isolation mechanism is proven with synthetic env vars and unit tests, not a second real Angel One/Dhan/ICICI login. It should be validated against a genuine second account before being relied upon for real money.
3. **`ZerodhaKiteBroker` remains a stub** (`connect()` raises `NotImplementedError`) — routed through the same factory for consistency, but not functional, exactly as before this phase.
4. **`AccountState.trades` is always empty** in `TradingAccountRouter.get_account_state()` — no adapter in this codebase exposes a distinct trades endpoint separate from the order book (documented already in Phase 15A's report for Angel One specifically).
5. **`BrokerCapabilities` is documentation-level, not an enforcement gate** — every real adapter still independently enforces its own `read_only`/`TRADING_MODE` checks regardless of what the capability registry declares; a future phase could wire capabilities into `BrokerAdapterFactory.build()` to refuse a request that a broker structurally cannot fulfill (e.g. attempting `get_quote` when `supports_market_data=False`), but that enforcement does not exist yet.
