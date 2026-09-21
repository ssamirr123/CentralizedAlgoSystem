# Phase 15C.2 — Angel Samir Live Read-Only Validation

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` API was ever called.**

## Date / Time

```
Date:      2026-09-15 (Tuesday)
Time:      10:14:42 IST
Timezone:  Asia/Kolkata
```

## Market Status

```
is_trading_day (weekday rule):        True
market_is_open (09:15-15:30 IST):     True
```

Computed via `trading.market_data.market_hours.market_is_open()` against the real, timezone-aware current timestamp — not assumed from system time alone. **Caveat, disclosed rather than hidden**: this computation consults no exchange holiday calendar (an empty holiday set), so it is a weekday+session-window determination only, not a confirmation against NSE's official trading calendar. Given today's date is a plain Tuesday, this caveat does not change the conclusion, but is recorded for honesty.

## TradingAccount

```
Account ID:            ANGEL_SAMIR
Broker:                angelone (ANGEL_ONE)
Authorization State:   READ_ONLY
Read Only:             true
Live Trading Enabled:  false (is_live_authorized() = False)
Credential Reference:  configured (env:ANGELONE, legacy default)
```

**Note on account identity** (disclosed per the same precedent as Phase 15A/15B): `ANGEL_SAMIR` is a label applied for this validation only. There is exactly one real Angel One credential set configured in this environment (`trading/.env`'s `ANGELONE_*` variables); no second, genuinely separate `ANGEL_SAMIR`-specific credential set exists. `credential_reference=""` resolves to that same legacy credential set via `trading.common.credentials.resolve_credentials()`. This is the same real account exercised throughout Phases 5A/14/15A.

Account was confirmed `READ_ONLY` and `read_only=True` before any broker call was made — per Step 2's explicit rule, execution would have stopped here had it not been.

## Authentication

```
Status:                    SUCCESS
Account Identity Verified: True (client_id masked: AA***21)
```

Real `generateSession()` call succeeded against the real Angel One account. No API key, password, PIN, TOTP, access token, or refresh token was printed at any point.

## Funds

```
Available Cash:    0.0
Used Margin:       0.0
Available Margin:  not separately exposed by this adapter
Mapping Verified:  NOT VERIFIED -- see below
```

**This is the decisive blocking finding.** `available_cash` and `used_margin` both read as exactly `0.0` — identical to the Phase 15A result, now reconfirmed during live market hours. Per Step 5's explicit instruction ("do NOT assume 0.0 = sufficient" and "do NOT assume unknown = sufficient"), this was investigated rather than accepted at face value:

1. **Genuinely zero funds** — possible, cannot be ruled out from this reading alone.
2. **Broker response mapping problem** — `AngelOneBroker.get_funds()` has been documented since Phase 2 as "best-effort... no existing code in this repo exercises SmartAPI's `rmsLimit()` calls, so their exact response-field mapping is unverified against a live account." This is still true — this validation did not (and, per its own read-only/no-code-change scope, should not) reach into `get_funds()`'s internals to compare against the raw SmartAPI response shape.
3. **Authentication/session problem** — ruled out: authentication succeeded, and other authenticated calls (positions, orders, quotes, instrument resolution) all succeeded normally on the same session, so the session itself is valid.
4. **API response parsing problem specific to funds** — cannot be ruled out; this is the same class of concern as (2) and is the most likely explanation given every other authenticated call worked normally.

Because this cannot be reliably resolved one way or the other without either (a) real capital genuinely being in the account (unverifiable from outside) or (b) inspecting/fixing `get_funds()`'s field mapping (out of scope for a read-only validation phase), **funds are not reliably verified**.

## Positions

```
Status: SUCCESS
Count:  0
```

Retrieved via `TradingAccountRouter.get_positions("ANGEL_SAMIR")` → `BrokerManager` → the real `AngelOneBroker` instance. Zero open positions — consistent with an account with no active exposure right now.

## Orders

```
Status: SUCCESS
Count:  0
```

Retrieved via `TradingAccountRouter.get_orders("ANGEL_SAMIR")` (routes to `get_order_book()`). Zero open orders.

## Trades

```
Status: NOT AVAILABLE (adapter limitation)
Count:  n/a
```

`AngelOneBroker` has no distinct trades endpoint separate from the order book — a documented limitation carried over from Phase 15A's report, not a new gap introduced here.

## Market Data

```
NIFTY:
    LTP:        23388.85
    Timestamp:  2026-09-15T04:44:49Z UTC (10:14:49 IST)

ATM Option (resolved dynamically, not hardcoded):
    Symbol:     NIFTY15SEP2623400CE
    Expiry:     2026-09-15  (0DTE -- expires TODAY)
    Exchange:   NFO
    Token:      47293
    LTP:        58.7
    Timestamp:  2026-09-15T04:45:14Z UTC (10:15:14 IST)
```

Resolution used the existing `trading.validation.angel_readonly._pick_current_nifty_ce_symbol()` (downloads the public NFO scrip master independently and picks the nearest-expiry ATM strike) exactly as Phase 5A/15A already did — no option symbol was hardcoded. **Note**: today's resolved contract expires today (0DTE) — flagged for awareness, not acted on, since this phase places no order.

## Account Isolation

```
Credential Isolation:    N/A this run -- only one real credential set exists in this environment;
                         the mechanism itself was already proven with synthetic accounts in Phase 15B
                         (tests/common/test_credentials.py) and was not re-exercised with a second
                         real account here because none exists.
Account State Isolation: VERIFIED -- AccountState.account_id == "ANGEL_SAMIR" and
                         AccountState.broker_id == "angelone" on every read; no cross-account
                         leakage possible since only one account was registered in this run's
                         BrokerManager instance.
Owner Context Isolation: N/A this run -- no owner_context was supplied (system-level validation,
                         not a per-owner request); TradingAccountRouter's owner-isolation logic
                         itself was already proven in Phase 15B (tests/common/test_account_router.py).
```

## Safety

```
place_order calls:    1 attempted, 0 reached the broker (BLOCKED -- ReadOnlyModeError)
modify_order calls:   1 attempted, 0 reached the broker (BLOCKED -- ReadOnlyModeError)
cancel_order calls:   1 attempted, 0 reached the broker (BLOCKED -- ReadOnlyModeError)
real orders placed:   0
```

All three mutation methods were called explicitly (against dummy arguments) specifically to prove they raise `ReadOnlyModeError` before any network request — the same negative-test pattern used in Phase 15A. None reached Angel One's real order-placement API.

Authorization state was re-checked after every operation and remained `READ_ONLY` throughout — nothing in this validation (or anywhere in the codebase) can silently promote an account to `CANARY_READY`/`LIVE_AUTHORIZED`. The central kill switch (`CentralKillSwitch`) was instantiated only to confirm its default state (`engaged=False`) and was never toggled.

## Tests

```
Collected: 1217
Passed:    1214
Failed:    3   (pre-existing baseline, unrelated -- see below)
Skipped:   0
```

Targeted Phase 15B/15B.1 suites (`test_account_router.py`, `test_broker_adapter_factory.py`, `test_broker_types.py`, `test_credentials.py`, `test_account_authorization_state.py`, `test_authorization_state_gate.py`, `test_phase_15b_1_safety_regression.py`, `test_risk_manager_account_isolation.py`, `test_phase_15b_no_live_orders.py`, `test_observability_wiring.py`) all pass. Full regression suite re-run confirms no new regression.

### The 3 pre-existing failures (unchanged, not introduced this phase)

```
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[CombinedVwapNifty]
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[DoubleStraddelAlgo]
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[Vwap_Algo_Nifty_hedge]
```

Same environment-specific root cause documented since Phase 15B (real local `trading/.env` leaking through each algo's own `dotenv` loader). Not touched this phase.

## Known Issues

1. **`AngelOneBroker.get_funds()` field-mapping remains unverified against a real response** — this is the second time (Phase 15A, now Phase 15C.2) this exact ambiguity has been hit on live/near-live checks. A future phase should compare `get_funds()`'s parsed output directly against the raw `rmsLimit()` JSON response (logged internally, never printed with secrets) to determine whether `0.0`/`0.0` reflects a real empty account or a wrong field name.
2. Today's dynamically-resolved ATM contract is 0DTE (expires today) — noted for context only; no order was proposed or considered.
3. No second real broker account exists to exercise credential/owner isolation against genuinely different live credentials (mechanism already proven with synthetic accounts in Phase 15B).

## Final Status

**PHASE 15C.2 STATUS: BLOCKED**

Every check this phase requires — authentication, account identity, positions, orders, live market data, account-state consistency, mutation blocking, authorization-state stability — passed. Funds are the sole blocking condition: they could not be reliably distinguished between "genuinely zero" and "broker field-mapping ambiguity," and per this phase's own explicit rule, an unverifiable funds reading must not be resolved by assumption in either direction.

---

```
PHASE 15C.2 STATUS: BLOCKED

Market status: OPEN (Tue 2026-09-15, 10:14 IST, within 09:15-15:30 session; no holiday calendar consulted)

Account identity verified: YES (client_id masked AA***21)

Funds verified: NO -- available_cash=0.0, used_margin=0.0, ambiguous (genuine zero vs. unverified field mapping)

Positions verified: YES (count=0)

Orders verified: YES (count=0)

Trades verified: NOT AVAILABLE (adapter has no distinct trades endpoint -- documented limitation)

Live market data verified: YES (NIFTY LTP=23388.85; ATM option NIFTY15SEP2623400CE, LTP=58.7)

Credential isolation: N/A this run (only one real credential set exists; mechanism proven in Phase 15B with synthetic accounts)

Authorization state: READ_ONLY (unchanged throughout)

Real broker read-only calls: multiple (connect, get_account_info, get_funds, get_positions, get_order_book, get_quote x2, resolve_instrument, disconnect)

Real mutation calls: 0

Real orders placed: 0

Regression tests: 1217 collected, 1214 passed, 3 failed (pre-existing baseline, unrelated), 0 skipped

Known limitations: get_funds() field-mapping remains unverified against a real response; no second real account exists for live credential-isolation testing

NEXT PHASE: NOT AUTHORIZED to proceed toward a live canary until funds can be reliably verified (either a confirmed real balance or a corrected/verified get_funds() field mapping). Stopping here.
```
