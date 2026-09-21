# Phase 8 — Dhan Broker Adapter

```text
DoubleStraddelAlgo
      ↓
OrderIntent
      ↓
TradingAccount = DHAN_MAIN
      ↓
BrokerManager
      ↓
DhanBroker (NEW)
      ↓
Shadow execution (ConnectedShadowBroker + ShadowBroker, Phase 5B — reused, not duplicated)
```

`trading/common/brokers/dhan.py` — `DhanBroker` — implements `BrokerClient` following the **exact same structure** as `AngelOneBroker`: same method set, same lazy-SDK-import pattern, same injectable factory/resolver for testability, same read-only guard checked first in every mutating method, same error-classification vocabulary, same optional adapter-specific extras (`resolve_instrument`, `get_account_info`, `get_funds`, `get_order_book`, `get_open_orders`, `get_order`, `modify_order`).

---

## Read Operations (implemented first, as instructed)

| Capability | Method | Notes |
|---|---|---|
| Authentication | `connect()` | Dhan has **no TOTP/password login flow** (unlike Angel) — the access token is generated externally and supplied as a long-lived credential. `connect()` verifies it's still valid by calling a lightweight authenticated read endpoint (`get_fund_limits()`), since there is nothing to "generate a session" from. |
| Funds | `get_funds()` / `get_account_info()` | Extra, adapter-specific (not part of `BrokerClient` ABC), matching Angel's own pattern exactly. |
| Instruments | `resolve_instrument()` | Downloads Dhan's public scrip-master CSV, matches by trading symbol. **Unverified** exact column names — see Known Limitations. |
| LTP | `get_quote()` | Real translation logic against Dhan's `ticker_data()`-shaped call. |
| Positions | `get_positions()` | Normalizes into the shared, broker-independent `Position` model. |
| Orders | `get_order_book()` / `get_open_orders()` / `get_order()` | Dhan's order-status vocabulary (`TRADED`/`PENDING`/`TRANSIT`/`CANCELLED`/`REJECTED`/`EXPIRED`) mapped onto the generic `COMPLETE`/`OPEN`/`CANCELLED`/`REJECTED` vocabulary `trading.common.execution` understands — never leaks a Dhan-specific string upward. |

## Simulated Execution (implemented second, as instructed)

`place_order()`, `modify_order()`, `cancel_order()` all have **real translation logic** (matching the request Phase 8 makes: "follow exactly the same BrokerClient interface used by AngelOne", which does implement real logic, gated) — but reaching Dhan's real order API requires passing through the same `read_only`/`TRADING_MODE` double-gate `AngelOneBroker` already uses.

**Critical difference from Angel, explicit per your instruction ("do NOT enable real Dhan order placement yet")**: `DhanBroker`'s `read_only` flag **defaults to `True`** — the opposite of `AngelOneBroker`'s default (`False`, relying solely on `TRADING_MODE`). `DHAN_READ_ONLY` must be explicitly set to a falsy value to disable it, and `TRADING_MODE=live` is still separately required — **two independent, both-explicit opt-ins**, a stronger default posture than Angel's, matching the elevated caution a brand-new, unvalidated adapter warrants. Verified by `test_read_only_defaults_to_true_unlike_angelone` and `test_place_order_blocked_by_default_read_only`.

For genuine simulated execution (no real order API involved at all), `DhanBroker` composes with `ShadowBroker`/`ConnectedShadowBroker` **exactly as built in Phase 5B, with zero new production glue** — proof that "follow the same interface" pays off immediately: Dhan slotted into the existing hybrid-broker pattern without any changes to `connected_shadow_broker.py`.

---

## Verification: DHAN_MAIN vs ANGEL_MAIN, Same Intent

`tests/algos/test_dhan_vs_angel_shadow.py` builds two independent stacks —

```text
DoubleStraddelAlgo → OrderIntent → DHAN_MAIN  → BrokerManager → DhanBroker      → ConnectedShadowBroker → Shadow execution
DoubleStraddelAlgo → OrderIntent → ANGEL_MAIN → BrokerManager → AngelOneBroker  → ConnectedShadowBroker → Shadow execution
```

— and runs the **same** `OrderIntent` (SELL 65 `NIFTY15SEP2623400CE` @ 125.5, `MORNING_ENTRY`) through both:

| | DHAN_MAIN | ANGEL_MAIN |
|---|---|---|
| `ExecutionResult.status` | `COMPLETE` | `COMPLETE` |
| `ExecutionResult.success` | `True` | `True` |
| Simulated position quantity | `-65` | `-65` |
| Real order API reached | **No** (`fake.place_order` never called) | **No** (`fake.placeOrder` never called) |

Both real broker doubles' order-placement mocks are asserted `not_called()` — proving neither stack could have touched a real broker regardless of which adapter sits underneath.

---

## The Strategy Code Did Not Change

`git diff --stat` confirms **zero diff** under `trading/algos/DoubleStraddelAlgo/*` (beyond the already-existing, untouched-this-phase `broker/execution_bridge.py`), `CombinedVwapNifty/*`, and `Vwap_Algo_Nifty_hedge/*`. `STRATEGY_ID = "DoubleStraddelAlgo"` is used only as a label in the comparison test — the actual strategy process/files are never imported, started, or referenced.

---

## Files Created
```
trading/common/brokers/dhan.py
tests/common/test_dhan_broker.py
tests/algos/test_dhan_vs_angel_shadow.py
docs/phase-8-dhan-adapter-report.md
```

## Files Modified (both purely additive)
```
trading/common/config.py   (+dhan_client_id, +dhan_access_token on BrokerCredentials)
trading/common/broker.py   (create_broker() gains a "dhan" branch, alongside the existing paper/zerodha/angelone/icici_breeze ones)
```
No production/strategy files were modified.

---

## Tests
```
python -m pytest tests/common/test_dhan_broker.py -v
→ 41 passed

python -m pytest tests/algos/test_dhan_vs_angel_shadow.py -v
→ 5 passed

python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ all passed (382 total)
```

`test_dhan_broker.py` mirrors every category `test_angelone_broker.py` already covers: authentication success/failure/missing-credentials, connection lifecycle, read-only default (and its Dhan-specific stronger default), funds/account normalization, LTP normalization + malformed/failure responses, instrument lookup, order translation (MARKET/LIMIT, BUY/SELL), rejected-order handling, order lifecycle (`get_order`/`modify_order`/`cancel_order`, partial fill, Dhan status-vocabulary mapping), rate-limit/timeout/generic error classification, and no-credential-leakage.

---

## Safety Verification

- **No real Dhan order was placed, modified, or cancelled** — `read_only` defaults to `True`; every test constructs a `FakeDhanApi` double (pure Python, no network); `place_order`/`cancel_order`'s underlying mocks are asserted never called in every relevant test.
- **No real network call anywhere in the automated suite** — `dhan_client_factory`/`instrument_resolver` are always injected in tests, exactly as `AngelOneBroker`'s tests already do.
- **No files under `trading/algos/*` were modified.**
- **No credentials were hardcoded, printed, or committed.**

## Known Limitations

1. **Unverified against a real Dhan account** — no real Dhan credentials were used anywhere in this phase (none were provided, and Phase 8 didn't ask for a live validation the way Phase 5A did for Angel). The exact field names for `get_fund_limits()`, `get_positions()`, `get_order_list()`, `ticker_data()`, and the scrip-master CSV's columns (`SEM_TRADING_SYMBOL`, `SEM_SMST_SECURITY_ID`, ...) are modeled on DhanHQ v2's publicly documented shape from general knowledge, not confirmed live — the same caveat Phase 2 already carries for `AngelOneBroker.get_account_info()`/`get_funds()`.
2. **Instrument resolution only covers NSE F&O (options)** — matching `AngelOneBroker`'s own documented limitation; the default resolver doesn't attempt index/cash-segment symbols.
3. **`DHAN_MAIN` is not yet registered anywhere production-facing** — this phase proves the adapter and the shadow-comparison pattern work; wiring `DHAN_MAIN` into the Phase 7 multi-account registry as a genuinely *available* broker (superseding its current "unavailable, not implemented" status from Phase 7) is a natural next step once this adapter is validated against a real account.

## Recommendation for a Future Phase
Once real Dhan credentials are available, run a Phase-5A-equivalent real-account read-only validation (`trading/validation/angel_readonly.py`'s exact pattern, retargeted at Dhan) to confirm the field-name assumptions in Known Limitation #1 before ever considering `DHAN_READ_ONLY=false`.

STOP after this report — no real Dhan order placement was enabled at any point.
