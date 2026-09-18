# Phase 15C.2.1 — Angel One Funds Mapping Diagnostic and Fix

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` API was ever called. No strategy was started. No account was promoted to `CANARY_READY`/`LIVE_AUTHORIZED`.**

## 1. Objective

Resolve the Phase 15C.2 blocker: `get_funds()` returned `available_cash=0.0`/`used_margin=0.0`, and it was unclear whether this reflected the real account or a broker-response field-mapping problem.

## 2. Previous blocker

From `docs/phase-15c-2-angel-samir-live-read-only-report.md`: funds could not be reliably verified because `AngelOneBroker.get_funds()` had been documented since Phase 2 as "best-effort... unverified against a live account," and a `0.0`/`0.0` reading was equally consistent with a genuinely empty account or a wrong field-name assumption.

## 3. Raw response structure

The real, authenticated session's `rmsLimit()` call was captured directly (read-only; no code path other than this diagnostic ever inspected it):

```json
{
  "status": true,
  "message": "SUCCESS",
  "errorcode": "",
  "data": {
    "net": "0.0000",
    "availablecash": "0.0000",
    "availableintradaypayin": "0.0000",
    "availablelimitmargin": "0.0000",
    "collateral": "0.0000",
    "m2munrealized": "0.0000",
    "m2mrealized": "0.0000",
    "utiliseddebits": "0.0000",
    "utilisedspan": null,
    "utilisedoptionpremium": null,
    "utilisedholdingsales": null,
    "utilisedexposure": null,
    "utilisedturnover": null,
    "utilisedpayout": "0.0000"
  }
}
```

No API key, secret, password, PIN, TOTP, access token, session token, or authorization header appears anywhere in this response — `rmsLimit()`'s response contains only numeric account-balance fields, none of which are credentials.

A parallel, real `getProfile()` call on the **same session** was also captured, to rule out a session/authentication problem:

```json
{
  "status": true,
  "message": "SUCCESS",
  "errorcode": "",
  "data": {
    "clientcode": "AA***21",
    "name": "<present, masked in this report>",
    "email": "",
    "mobileno": "",
    "exchanges": ["nse_fo", "nse_cm", "cde_fo", "bse_fo", "bse_cm", "mcx_fo"],
    "products": ["MARGIN", "MIS", "NRML", "CNC", "CO", "BO"],
    "lastlogintime": "",
    "broker": ""
  }
}
```

`getProfile()` succeeded and returned a real, populated account profile (name present, valid exchange/product entitlements) on the exact same session used for `rmsLimit()`. This decisively rules out a session/authentication problem: the session is valid and fully authenticated; it simply reports zero funds.

## 4. Funds field mapping

| Raw broker field | Current mapping | Correct? |
|---|---|---|
| `availablecash` | `available_cash` | **YES** — exact real field name, confirmed present in the live response |
| `utiliseddebits` | `used_margin` | **YES** — exact real field name, confirmed present in the live response |
| `availablelimitmargin` | *(not previously mapped)* → now `available_margin` | **YES** (newly added) — a direct field, confirmed present, distinct in meaning from `availablecash` |

**`funds_mapping_status = VERIFIED_ZERO`**

The pre-existing mapping (`availablecash`→`available_cash`, `utiliseddebits`→`used_margin`) was already correct — both field names match the real SmartAPI response exactly. This is **Case A**, not Case B: the account genuinely reports zero across every fund-related field in the entire response (`net`, `availablecash`, `availableintradaypayin`, `availablelimitmargin`, `collateral`, `m2munrealized`, `m2mrealized`, `utiliseddebits`, `utilisedpayout` all read `"0.0000"`; the remaining fields are `null`). A field-mapping bug would typically show a discrepancy between one specific mapped field and its true semantic counterpart — here, every candidate field is uniformly zero, which is exactly what a genuinely empty trading account looks like, not what a targeting-the-wrong-field bug looks like. Per this phase's own rule ("if Angel One genuinely reports zero available cash and zero used margin... do not invent or substitute another value"), zero is reported as zero.

## 5. Unit/semantic validation

- All `rmsLimit()` funds values are **decimal strings** (e.g. `"0.0000"`), not native numbers — confirmed directly from the raw response. `float()` conversion (already present in `get_funds()`) correctly parses this format; no paise/rupee conversion is needed (Angel's own values are already in rupees, matching the format of every other rupee-denominated field observed, e.g. `m2munrealized`).
- `available_margin` is a **direct field** (`availablelimitmargin`), not derived/computed — confirmed by cross-referencing Angel's own field naming (`available` + `limitmargin`, i.e. the account's total remaining tradeable limit) against the response structure, and verified in a new test (`test_available_margin_is_a_direct_field_not_derived`) that it is NOT computed as `available_cash - used_margin` or any other formula.
- Several fields (`utilisedspan`, `utilisedoptionpremium`, `utilisedholdingsales`, `utilisedexposure`, `utilisedturnover`) are `null` rather than `"0.0000"` — these are not currently mapped to anything and remain out of scope; the existing `or 0` fallback already handles a `None` value safely for any field that IS mapped.

## 6. Code changes

`trading/common/brokers/angelone.py`:
- `FundsSnapshot` gained a new field, `available_margin: float = 0.0` (defaulted — every existing construction site and equality-based test using only `available_cash`/`used_margin` is unaffected).
- `get_funds()` now also maps `availablelimitmargin` → `available_margin`.
- `get_funds()`'s float conversions are now wrapped in `try/except (TypeError, ValueError)`, raising `BrokerConnectionError` with a descriptive message on a malformed numeric field — previously this would have raised an uncaught, unclassified `ValueError`. This is a **safety hardening**, not a bug fix for the zero-funds finding itself (no bug was found there) — it closes a real, separate gap: a malformed value could previously crash the caller with an unhelpful traceback instead of a clear, classified error consistent with every other failure mode this adapter already reports.
- Module docstring and `FundsSnapshot`'s docstring updated to record that these three fields are now verified against a real response, while every other `rmsLimit()` field remains unmapped/unverified.

No change was made to `TradingAccount`, `TradingAccountRouter`, `BrokerAdapterFactory`, `BrokerType`, `BrokerCapabilities`, `AccountState`, credential resolution, or any Phase 14.6/14.7 safety gate.

## 7. Tests added

`tests/common/test_angelone_funds_mapping.py` (10 new tests, all passing), covering the full required matrix:
1. Normal valid funds response (all three fields, realistic values)
2. Zero funds response (reported as zero, not fabricated)
3. Missing funds field (defaults to 0.0)
4. Malformed numeric value (`"N/A"` → raises `BrokerConnectionError`, never a silent 0.0)
5. Null value (treated the same as missing — 0.0, not an error)
6. Broker error response (`status: false` → raises, never read as zero funds)
7. Broker exception during the `rmsLimit()` call itself (classified, not swallowed)
8. Decimal-string-to-float unit conversion (confirmed correct for realistic values)
9. Each normalized field individually (`available_cash`, `used_margin`, `available_margin`)
10. `available_margin` confirmed to be a direct field, not a derived formula

Every existing `AngelOneBroker` test (`tests/common/test_angelone_broker.py`, including the pre-existing `test_get_funds_normalization`) still passes unchanged, confirming the new field/behavior is fully backward compatible.

## 8. Real read-only validation (re-run after the change)

The Phase 15C.2 real read-only validation script was re-run against the live, real Angel One session with the updated code:

```
Authentication:          SUCCESS
Account identity:        MATCHED (client_id masked AA***21)
available_cash:          0.0
used_margin:             0.0
Positions:               0
Orders:                  0
Mutation calls:          place_order/modify_order/cancel_order all BLOCKED (ReadOnlyModeError)
Authorization state:     READ_ONLY (unchanged)
```

**Note**: this re-run script still prints its own older `"ambiguous": true` note (written before this diagnostic existed) — that note is now **superseded** by the direct raw-response evidence captured in Section 3 above, which is far stronger: it shows *every* fund-related field in the entire response reading zero, plus independent confirmation via `getProfile()` that the session itself is fully valid. The script's own leftover wording should not be read as still-open ambiguity.

## 9. Account identity

Client ID: masked `AA***21` — identical across both the original Phase 15C.2 validation and this diagnostic's `getProfile()`/`rmsLimit()` calls, on the same real, single configured Angel One credential set (`credential_reference=""` → legacy `ANGELONE_*` env vars). No cross-account data was possible in this run: only one `TradingAccount` (`ANGEL_SAMIR`) was registered in the `BrokerManager` instance used for this diagnostic, and `get_funds()` operates only on `self._smart_api`, the session belonging to that same account — there is no global mutable credential state anywhere in this adapter.

## 10. Safety validation

```
Authorization state:      READ_ONLY (before, during, and after)
CANARY_READY:              false
LIVE_AUTHORIZED:           false
Real mutation calls:       0
Real orders placed:        0
Kill switch:                unchanged (not engaged, never toggled)
RiskManager:                unchanged
LiveCanaryGuard:             unchanged
Idempotency:                unchanged
Execution mode:             unchanged
Broker response validation: unchanged
```

## 11. Regression results

```
Total collected: 1226
Passed:          1223
Failed:          3   (pre-existing baseline, unrelated -- unchanged from every prior phase)
Skipped:         0
```

```
FAILED tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[CombinedVwapNifty]
FAILED tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[DoubleStraddelAlgo]
FAILED tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[Vwap_Algo_Nifty_hedge]
```

Same environment-specific root cause documented since Phase 15B (the real local `trading/.env` leaks through each algo's own `dotenv` loader). No new regression was introduced by this phase's changes.

## 12. Known limitations

1. Only the three funds fields in scope for this diagnostic (`available_cash`, `used_margin`, `available_margin`) have been verified against a real response — every other `rmsLimit()` field (`collateral`, `m2munrealized`, `m2mrealized`, `utilisedspan`, etc.) remains unmapped and unverified.
2. The account's real balance is confirmed to be genuinely zero **at the time of this diagnostic** (2026-09-15, live market hours) — this is a point-in-time fact, not a permanent guarantee; a future validation should re-check if the account's funding status is expected to have changed.
3. No second real broker account exists to cross-validate this mapping against a different, non-zero real balance.

## 13. Final status

**PHASE 15C.2.1 STATUS: PASS**

```
Raw broker funds response: VERIFIED
available_cash: 0.0
used_margin: 0.0
available_margin: 0.0
Mapping status: VERIFIED_ZERO
```

The mapping is confirmed correct, not merely assumed. The zero balance is now a **confirmed real fact**, not an unresolved ambiguity — the Phase 15C.2 blocker is closed.

---

```
PHASE 15C.2.1 STATUS: PASS

Raw broker funds response: VERIFIED (captured directly from the real, authenticated rmsLimit() call)

Funds mapping status: VERIFIED_ZERO

available_cash: 0.0
used_margin: 0.0
available_margin: 0.0

Account identity: MATCHED (client_id masked AA***21), session validity independently confirmed via getProfile()

Authorization state: READ_ONLY (unchanged)
CANARY_READY: false
LIVE_AUTHORIZED: false

Real mutation calls: 0
Real orders placed: 0

Regression: 1226 collected, 1223 passed, 3 failed (pre-existing baseline, unrelated), 0 skipped

Known limitations: only 3 of rmsLimit()'s ~14 fields verified; zero balance is a point-in-time fact; no second real account to cross-validate against

NEXT PHASE: 15C.3 is eligible given this diagnostic's PASS result, but is NOT started automatically. No further phase begins without explicit user instruction.
```

**STOP.** No second account was configured. No canary was authorized. No order was placed. No strategy was started. No live execution permission was modified.
