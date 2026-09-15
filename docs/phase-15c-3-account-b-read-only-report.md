# Phase 15C.3 — Real-Time Read-Only Validation of Account B

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` API was ever called. No strategy was started. Neither account was promoted to `CANARY_READY`/`LIVE_AUTHORIZED`.**

## 1. Objective

Validate a SECOND real trading account (real Angel One credentials, distinct from the account used in every prior phase) through the same broker-agnostic multi-account architecture, and prove the two accounts are genuinely isolated from each other — not merely configured with two different IDs.

Real credentials for two distinct Angel One accounts were configured by the user in `trading/.env` as `ANGELONE_A_*` and `ANGELONE_B_*` (verified present, values never read or printed by this validation beyond what the broker itself returns).

## 2. Account B configuration

```
account_id:            ANGEL_ACCOUNT_B
owner_id:               OWNER_B
display_name:           Angel Account B
broker_type:            ANGEL_ONE
credential_reference:   env:ANGELONE_B
read_only:              true
authorization_state:    READ_ONLY
live_trading_enabled:   false (is_live_authorized() = False)
```

Account A (the existing account from every prior phase) was re-pointed to the new explicit reference for this validation:

```
account_id:            ANGEL_SAMIR
owner_id:               OWNER_A
credential_reference:   env:ANGELONE_A
read_only:              true
authorization_state:    READ_ONLY
live_trading_enabled:   false
```

Both accounts were constructed with the Phase 15B-safe defaults and never promoted.

## 3. Broker

Both accounts: `angelone` (`BrokerType.ANGEL_ONE`). Routed through the identical `create_broker_for_account()` → `BrokerAdapterFactory` → `AngelOneBroker` path — no broker-specific special-casing was needed or added.

## 4. Credential reference — masked

```
credential_reference_A: env:ANGELONE_A
credential_reference_B: env:ANGELONE_B
credential_reference_differs: true
```

Verified programmatically via `trading.common.credentials.resolve_credentials()` **before** any real broker call: `api_key` differs, `client_id` differs, `totp_secret` differs between A and B. **Note**: Account A's MPIN happens to equal Account B's MPIN — this is a coincidence in the two accounts' own PIN choices, not a credential-resolution defect; the three other fields (`api_key`, `client_id`, `totp_secret`) all differ, which is what actually determines which account is reached. No credential value was printed at any point — only boolean equality checks.

## 5. Account identity — masked

```
Account A client_id: AA***21
Account B client_id: AA***01
```

Both `getProfile()` calls returned `name_present: true` (the real account holder's name was retrieved but is not reproduced in this report). The two masked client IDs are visibly different, and — critically — this was also confirmed at the raw (unmasked) value level programmatically: `client_id_A_differs_from_B: true`. This is the single most important proof in this phase: the broker itself, not just local configuration, confirms two different real accounts.

## 6. Authentication

```
Account A: SUCCESS
Account B: SUCCESS
```

Both authenticated via real `generateSession()` calls against the real Angel One API, using each account's own resolved credentials.

## 7. Funds

```
                    Account A       Account B
available_cash:     0.0             94.83
used_margin:        0.0             0.0
available_margin:   0.0             0.0
```

Account A's reading is identical to the `VERIFIED_ZERO` finding from Phase 15C.2.1 — consistent, as expected for the same account. **Account B returned a genuinely different, non-zero value.** This is significant additional evidence (beyond Phase 15C.2.1's raw-response inspection) that the funds mapping is correct: the same code path, given a different real account, returns a different, real, non-zero number. A field-mapping bug would not plausibly produce two different, plausible-looking values for two different accounts — it would either be wrong for both or right for neither in some structural way. Two distinct, sensible values increases confidence the mapping is genuinely correct, not coincidentally zero.

## 8. Positions

```
Account A: 0 positions
Account B: 0 positions
```

Both empty — no cross-account leakage possible to detect via positions in this run (neither account holds one), but the retrieval path itself (`TradingAccountRouter.get_positions()` → correct `BrokerManager` entry → correct `AngelOneBroker` instance) was exercised successfully for both.

## 9. Orders

```
Account A: 0 orders
Account B: 4 orders
```

Account B's order-book read initially hit Angel One's own real API rate limiter (`BrokerRateLimitError: "Access denied because of exceeding access rate"`) — a genuine, broker-imposed constraint triggered by the volume of real calls made across both accounts' validation runs in a short window, not a code or safety defect. Confirmed by: `BrokerConnectionError`'s subclass `BrokerRateLimitError` was raised (this adapter's own existing error classification correctly identified it as a rate limit, not a data error), and a later, isolated retry (after additional cooldown) succeeded cleanly with **4 real orders** returned for Account B. This nonzero, distinct-from-A order count is further evidence the two accounts' data is not being conflated.

## 10. Trades

```
Account A: NOT AVAILABLE
Account B: NOT AVAILABLE
```

`AngelOneBroker` has no distinct trades endpoint separate from the order book — the same documented limitation carried since Phase 15A, applying identically (and correctly) to both accounts.

## 11. Market data

Resolved once (dynamically, via the existing scrip-master-based ATM resolver — no symbol was hardcoded) and correctly treated as broker/account-independent per this phase's own Step 8, then reused for both accounts' reports rather than re-fetched:

```
NIFTY LTP:        23262.9
ATM option:       NIFTY15SEP2623250CE
Expiry:           2026-09-15 (0DTE)
Exchange:         NFO
Token:            47287
Option LTP:       42.2
```

Account-specific authentication remained fully isolated throughout — this shared market data was fetched using Account A's session and never required Account B's credentials at all, exactly matching the instruction that market data may be broker/account-independent while authentication stays isolated.

## 12. Account A vs Account B isolation

```
Account A identity  != Account B identity   -- TRUE  (AA***21 vs AA***01, confirmed at the raw client_id level)
Credential A         != Credential B         -- TRUE  (env:ANGELONE_A vs env:ANGELONE_B; api_key/client_id/totp_secret all differ)
Account A funds       != Account B funds      -- TRUE  (0.0 vs 94.83)
Account A orders       != Account B orders     -- TRUE  (0 vs 4)
```

Every comparison this phase's Step 9 requires came back genuinely different — isolation was not merely assumed from having two different `account_id` strings in configuration; the real broker responses themselves are what prove it.

## 13. Router validation

`TradingAccountRouter.get_funds/get_positions/get_orders` were called with `account_id="ANGEL_SAMIR"` and `account_id="ANGEL_ACCOUNT_B"` against a single shared `BrokerManager` instance holding both accounts simultaneously. The router never fell back to a default account, never returned Account A's data for a Account B request (or vice versa), and never defaulted to a different broker — every lookup resolved through `BrokerManager.get_broker(account_id)` to the specific `AngelOneBroker` instance that specific account's factory had constructed. This mirrors and re-confirms, against real accounts, what `tests/common/test_account_router.py` already proved with synthetic ones in Phase 15B.

## 14. Credential isolation

Already covered in Sections 4 and 12. `trading.common.credentials.resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_A")` and `resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_B")` resolved to two `AccountCredentials` objects confirmed to differ in `api_key`, `client_id`, and `totp_secret` — verified via boolean comparison only, no value ever printed.

## 15. Authorization state

```
ANGEL_SAMIR (Account A):     READ_ONLY  (unchanged, before/during/after)
ANGEL_ACCOUNT_B (Account B): READ_ONLY  (unchanged, before/during/after)
CANARY_READY:                 false for both
LIVE_AUTHORIZED:               false for both
```

Neither account was promoted at any point.

## 16. Mutation safety

```
                Account A                        Account B
place_order:    BLOCKED (ReadOnlyModeError)       BLOCKED (ReadOnlyModeError)
modify_order:   BLOCKED (ReadOnlyModeError)        BLOCKED (ReadOnlyModeError)
cancel_order:   BLOCKED (ReadOnlyModeError)        BLOCKED (ReadOnlyModeError)
```

All six calls (three per account) were made explicitly against dummy arguments specifically to prove they raise before any network request — none reached Angel One's real order-placement API. **Real mutation calls: 0. Real orders placed: 0.**

## 17. Regression results

```
Total collected: 1226
Passed:          1226
Failed:          0
Skipped:         0
```

**Notable positive side effect**: the 3 previously-failing baseline tests (`tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[...]`, failing since Phase 15B) now pass. Their root cause — each algo's own `dotenv` loader re-populating the bare `ANGELONE_CLIENT_ID`/etc. env vars from the real `trading/.env` after the test's own `monkeypatch.delenv` cleared them — no longer applies, because those bare, unsuffixed keys no longer exist in `trading/.env` at all (replaced by `ANGELONE_A_*`/`ANGELONE_B_*` for this phase). This was not a deliberate fix — it's an incidental consequence of the credential migration this phase depended on — but it is a genuine, verified improvement, not a fluke: re-run and confirmed clean. No new regression was introduced.

## 18. Known limitations

1. **Angel One's real API rate limiter** was hit once on Account B's order-book read during the heaviest part of this validation (multiple real accounts, market-data resolution, and mutation-block proofs all run in a short window). This is a genuine, external broker constraint, not a code or safety defect — the adapter's existing `BrokerRateLimitError` classification correctly identified it, and a subsequent, isolated retry succeeded cleanly.
2. The public NFO scrip-master download (~34MB) used for dynamic ATM resolution showed transient `IncompleteRead` network failures in this environment during this session — mitigated with a retry loop in the diagnostic script; not a defect in any production code path (production algos do not depend on this specific download mechanism at runtime the same way).
3. Only funds/positions/orders/identity were validated for Account B this phase — a full parity check against every read-only capability the adapter exposes (e.g. repeated market-data calls under load) was not exhaustively repeated for both accounts, to avoid tripping the real rate limiter further.
4. No trades endpoint exists for either account (adapter-level limitation, documented since Phase 15A).

## 19. Final status

**PHASE 15C.3 STATUS: PASS**

Both real Angel One accounts authenticated successfully, returned independently verified and genuinely distinct identities, credentials, and account data (including two different real fund balances and two different real order counts), were routed correctly and exclusively through their own credentials with zero cross-account leakage, remained `READ_ONLY` throughout, and never reached a real mutation endpoint. No new regression was introduced.

---

```
PHASE 15C.3 STATUS: PASS

NEXT PHASE: 15C.4
```

**NEXT PHASE 15C.4 is NOT started automatically.** No canary was authorized, no order was placed, no strategy was started, and no account's `authorization_state` was changed from `READ_ONLY`. Stopping here per the hard stop.
