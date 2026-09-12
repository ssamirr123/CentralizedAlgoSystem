# Phase 9 — ICICI Breeze Broker Adapter

```text
DoubleStraddelAlgo
      ↓
OrderIntent
      ↓
TradingAccount = ICICI_MAIN
      ↓
BrokerManager
      ↓
ICICIBreezeBroker (NEW — replaces the Phase 0/2 stub)
      ↓
Shadow execution (ConnectedShadowBroker + ShadowBroker, Phase 5B — reused, not duplicated)
```

`trading/common/brokers/icici_breeze.py` — `ICICIBreezeBroker` — replaces the original `NotImplementedError` stub with a real implementation of `BrokerClient`, following the **exact same structure** as `AngelOneBroker` (Phase 2/4/5A) and `DhanBroker` (Phase 8): same method set, same lazy-SDK-import pattern, same injectable factory/resolver for testability, same read-only guard checked first in every mutating method, same error-classification vocabulary, same optional adapter-specific extras (`resolve_instrument`, `get_account_info`, `get_funds`, `get_order_book`, `get_open_orders`, `get_order`, `modify_order`).

---

## Read Operations (implemented first, as instructed)

| Capability | Method | Notes |
|---|---|---|
| Authentication | `connect()` | `BreezeConnect(api_key)` → `generate_session(api_secret, session_token)`, matching the auth flow already verified live by the separate, pre-existing `trading/market_data/providers/icici_breeze.py` market-data provider (a different layer, consulted only as a reference — see the module docstring). |
| Instruments | `resolve_instrument()` | **Parses** the same Angel-style tradingsymbol string (`"NIFTY15SEP2623400CE"`) OrderIntent already carries everywhere in this repo, directly into Breeze's descriptive `(stock_code, exchange_code, product_type, expiry_date, right, strike_price)` fields — no security-master download, no network call, unlike Angel/Dhan's token-lookup approach. This is possible because Breeze's own API addresses an option contract by these fields directly, with no separate opaque token/security-id to resolve. |
| Market data / LTP | `get_quote()` | Real translation against Breeze's `get_quotes()`-shaped call. |
| Positions | `get_positions()` | Normalizes into the shared, broker-independent `Position` model. |
| Orders | `get_order_book()` / `get_open_orders()` / `get_order()` | Breeze's order-status vocabulary (`Executed`/`Ordered`/`Pending`/`Cancelled`/`Rejected`/`Expired`) mapped onto the generic `COMPLETE`/`OPEN`/`CANCELLED`/`REJECTED` vocabulary `trading.common.execution` understands. |
| Funds / account info | `get_funds()` / `get_account_info()` | Extra, adapter-specific (not part of the `BrokerClient` ABC), matching Angel/Dhan's own pattern exactly. |

## Simulated Execution (implemented second, as instructed)

`place_order()`, `modify_order()`, `cancel_order()` all have real translation logic against Breeze's descriptive order-placement fields, gated behind the same double-check every other adapter uses.

**Matching Dhan's elevated-caution posture for a brand-new, unvalidated adapter (explicit per your "no real orders" instruction)**: `ICICIBreezeBroker`'s `read_only` flag **defaults to `True`** — the opposite of `AngelOneBroker`'s default. `ICICI_BREEZE_READ_ONLY` must be explicitly set to a falsy value to disable it, and `TRADING_MODE=live` is still separately required — two independent, both-explicit opt-ins.

For genuine simulated execution (no real order API involved at all), `ICICIBreezeBroker` composes with `ShadowBroker`/`ConnectedShadowBroker` **exactly as built in Phase 5B, with zero new production glue** — the same proof Phase 8 established for Dhan, now confirmed for a third, structurally different broker (Breeze addresses contracts by descriptive fields rather than a token, and none of that difference leaked into `ConnectedShadowBroker`).

---

## Verification: ICICI_MAIN vs ANGEL_MAIN, Same Intent

`tests/algos/test_icici_vs_angel_shadow.py` builds two independent stacks —

```text
DoubleStraddelAlgo → OrderIntent → ICICI_MAIN  → BrokerManager → ICICIBreezeBroker → ConnectedShadowBroker → Shadow execution
DoubleStraddelAlgo → OrderIntent → ANGEL_MAIN  → BrokerManager → AngelOneBroker     → ConnectedShadowBroker → Shadow execution
```

— and runs the **same** `OrderIntent` (SELL 65 `NIFTY15SEP2623400CE` @ 125.5, `MORNING_ENTRY`) through both:

| | ICICI_MAIN | ANGEL_MAIN |
|---|---|---|
| `ExecutionResult.status` | `COMPLETE` | `COMPLETE` |
| `ExecutionResult.success` | `True` | `True` |
| Simulated position quantity | `-65` | `-65` |
| Real order API reached | **No** (`fake.place_order` never called) | **No** (`fake.placeOrder` never called) |

Both real broker doubles' order-placement mocks are asserted `not_called()`.

## Adapter Contract Tests

`tests/common/test_broker_adapter_contract.py` (NEW) parametrizes a shared assertion set over all three real adapters (`AngelOneBroker`, `DhanBroker`, `ICICIBreezeBroker`), each constructed against its own fake SDK double: `BrokerClient` interface conformance, connect/disconnect lifecycle, `get_quote()`/`get_positions()` return the shared generic types, operations require `connect()` first, `place_order()`/`cancel_order()` are blocked identically by read-only mode and by `TRADING_MODE != live`, and no adapter's `repr()` leaks a credential. 36 tests total (12 assertions × 3 adapters).

---

## The Strategy Code Did Not Change

`git diff --stat -- trading/algos/DoubleStraddelAlgo trading/algos/CombinedVwapNifty trading/algos/Vwap_Algo_Nifty_hedge` shows only the two files already modified in earlier phases (`broker/execution_bridge.py` from Phase 3, `nifty_token.csv` as a data refresh from Phase 5A's live validation run) — **zero new changes** to any live algo file in this phase. `STRATEGY_ID = "DoubleStraddelAlgo"` is used only as a label in the comparison test — the actual strategy process/files are never imported, started, or referenced.

---

## Files Created
```
tests/common/test_icici_breeze_broker.py
tests/algos/test_icici_vs_angel_shadow.py
tests/common/test_broker_adapter_contract.py
docs/phase-9-icici-adapter-report.md
```

## Files Modified
```
trading/common/brokers/icici_breeze.py   (stub → real implementation; still constructed as ICICIBreezeBroker(config), signature-compatible with the Phase 7 registry test)
```
No `trading/common/config.py` or `trading/common/broker.py` changes were needed — `BrokerCredentials.icici_breeze_*` fields and `create_broker()`'s `"icici_breeze"` branch already existed from Phase 0/2.

No production/strategy files were modified.

---

## Tests
```
python -m pytest tests/common/test_icici_breeze_broker.py -v          → 45 passed
python -m pytest tests/algos/test_icici_vs_angel_shadow.py -v         → 5 passed
python -m pytest tests/common/test_broker_adapter_contract.py -v      → 36 passed
python -m pytest tests/common/test_multi_account_routing.py -v       → 15 passed (Phase 7 test unaffected by the stub→real swap)
python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ full suite passing (exit code 0, all green across 7 batches of dots, no failures/errors)
```

`test_icici_breeze_broker.py` mirrors every category `test_angelone_broker.py`/`test_dhan_broker.py` already cover: authentication success/failure/missing-credentials, connection lifecycle, read-only default (and its Dhan-matching stronger default), funds/account normalization, LTP normalization + malformed/failure responses, symbol-parsing instrument resolution (call/put, malformed symbol), order translation (MARKET/LIMIT, BUY/SELL), rejected-order handling, order lifecycle (`get_order`/`modify_order`/`cancel_order`, partial fill, Breeze status-vocabulary mapping), rate-limit/timeout/generic error classification, and no-credential-leakage.

---

## Safety Verification

- **No real ICICI Breeze order was placed, modified, or cancelled** — `read_only` defaults to `True`; every test constructs a `FakeBreezeApi` double (pure Python, no network); `place_order`/`cancel_order`'s underlying mocks are asserted never called in every relevant test.
- **No real network call anywhere in the automated suite** — `breeze_client_factory` is always injected in tests, exactly as `AngelOneBroker`/`DhanBroker`'s tests already do.
- **No files under `trading/algos/*` were modified in this phase.**
- **No credentials were hardcoded, printed, or committed.**
- The Phase 7 `test_multi_account_routing.py` suite (which already constructs `ICICIBreezeBroker` for the still-unavailable `ICICI_MAIN` account) continues to pass unmodified — the stub-to-real swap is signature-compatible.

## Known Limitations

1. **Unverified against a real ICICI Breeze account** — no real Breeze credentials were used anywhere in this phase. The exact field names for `get_portfolio_positions()`, `get_order_list()`, `get_funds()`, and `get_customer_details()` are modeled on Breeze's publicly documented REST shape from general knowledge, not confirmed live — the same caveat Phase 2/Phase 8 already carry for `AngelOneBroker`/`DhanBroker`'s account/funds/positions calls.
2. **Instrument resolution only covers NSE F&O (options)**, matching Angel/Dhan's own documented limitation.
3. **`ICICI_MAIN` is not yet registered anywhere production-facing** — this phase proves the adapter and the shadow-comparison pattern work; making `ICICI_MAIN` genuinely *available* in the Phase 7 registry (superseding its current "unavailable, not implemented" status) is a natural next step once this adapter is validated against a real account.
4. **The known `execution.py` limitation flagged in Phase 4/5B** (`place_limit()` never uses `intent.limit_price`, always recomputes its own price from `get_quote()` + slippage) applies identically here — not specific to this adapter, not fixed in this phase.

## Recommendation for a Future Phase
Once real ICICI Breeze credentials are available, run a Phase-5A-equivalent real-account read-only validation (`trading/validation/angel_readonly.py`'s exact pattern, retargeted at Breeze) to confirm the field-name assumptions in Known Limitation #1 before ever considering `ICICI_BREEZE_READ_ONLY=false`.

STOP after this report — no real ICICI Breeze order placement was enabled at any point.
