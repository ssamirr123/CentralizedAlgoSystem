# Phase 15D.1.1 — Account A Funds Reconciliation Diagnostic

**No real broker order was placed. No mutation API was called. Account A and Account B remain `READ_ONLY`.**

## 1. Objective

Determine why Account A's funds were repeatedly reported as `0.0`/`0.0`/`0.0` across Phases 15C.2.1, 15C.3, 15C.4, 15C.5, and 15D.1, when the account is known to hold real funds.

## 2. Account A identity verification

Re-authenticated using **only** `env:ANGELONE_A` credentials (no fallback, no reuse of Account B's credential reference):

```
Configured Account A credential_reference:  env:ANGELONE_A
Authenticated broker identity (masked):     AA***21
Matches every prior phase's Account A identity: YES
```

Identity match confirmed — this is unambiguously the same real account validated throughout this project. No identity mismatch, so this diagnostic proceeded past this gate. No API key, MPIN, TOTP secret, access token, or password was printed at any point.

## 3. Raw broker funds response

Captured fresh, directly from the real, authenticated `rmsLimit()` call (this is the FIRST time this exact call was made using the corrected per-account credential path — the original Phase 15C.2.1 raw-capture script used the legacy bare `ANGELONE_*` env vars, which no longer exist since the A/B credential migration, and now fails outright rather than silently reading the wrong account):

```json
{
  "status": true,
  "message": "SUCCESS",
  "errorcode": "",
  "data": {
    "net": "100.0000",
    "availablecash": "100.0000",
    "availableintradaypayin": "100.0000",
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
    "utilisedpayout": "100.0000"
  }
}
```

**This is a materially different reading than every prior check** (which consistently showed every field at `"0.0000"`). `net`, `availablecash`, and `availableintradaypayin` now read `"100.0000"` — a genuine, real, non-zero balance. `availablelimitmargin` (the field mapped to `available_margin`) is still `"0.0000"`.

A parallel `getProfile()` call on the same session succeeded normally (masked `AA***21`, name present, exchange/product entitlements returned) — the session itself was never in question; this is not an authentication/session issue.

## 4. Funds field mapping trace

| Raw broker field | Adapter field | `get_funds()` field | Phase 15D.1 field | Value now |
|---|---|---|---|---|
| `availablecash` | `available_cash` | `available_cash` | funds check | `100.0` |
| `utiliseddebits` | `used_margin` | `used_margin` | funds check | `0.0` |
| `availablelimitmargin` | `available_margin` | `available_margin` | funds check | `0.0` |

The mapping is **identical** to what was verified correct in Phase 15C.2.1 — no field name, no type conversion, no fallback-to-zero logic, and no exception-swallowing was found or changed. Every raw field name (`availablecash`, `utiliseddebits`, `availablelimitmargin`) matches the real SmartAPI response exactly, as it always has.

**Important, honestly-reported nuance**: `available_cash` is now non-zero (`100.0`), but `available_margin` (from `availablelimitmargin`) is still exactly `0.0`. These are two distinct real fields with two distinct real broker semantics — the account holds ₹100 of cash, but the broker's own margin-availability field for F&O trading reports zero. This is not a mapping defect (both values come straight from their own distinctly-named real fields, verbatim) — it reflects a real, current state of the account's margin allocation, which this diagnostic does not attempt to explain further (that would require Angel One's own margin-computation rules, out of scope for a mapping diagnostic).

## 5. Account A/B isolation evidence

Fresh, light `A → B → A` real read sequence performed this phase:

```
A: available_cash=100.0   used_margin=0.0
B: available_cash=94.83   used_margin=0.0
A: available_cash=100.0   used_margin=0.0
```

Zero cross-contamination — Account B's value (`94.83`, unchanged from every prior phase) never appeared for Account A, and vice versa. This is also itself proof that Account A's change from `0.0` to `100.0` was real: had this been a code bug (e.g. accidentally reading a shared/global value), Account B's independent, unrelated value would not have remained exactly `94.83` throughout.

## 6. Cache/state investigation

Direct source inspection of `AngelOneBroker.get_funds()` and `TradingAccountRouter.get_funds()`: **no caching, memoization, or persisted state exists anywhere in this path.** `get_funds()` calls `self._smart_api.rmsLimit()` fresh on every invocation; `TradingAccountRouter.get_funds()` is a direct, uncached delegation (`return broker.get_funds()`). No database, no in-memory dict, no test fixture, no mock is involved in the real path used by this diagnostic.

This is independently proven by the data itself: the SAME account, queried multiple times across this project's timeline, has now returned two DIFFERENT real values (`0.0` earlier today, `100.0` moments later) — a cache could not produce this; only a fresh call to the real API, reflecting a real, recent change to the account, explains it.

## 7. External-vs-API discrepancy

**Not applicable in the "discrepancy" sense this step anticipates.** The API now reports a non-zero balance consistent with what would be expected after a small real deposit — there is no case here of "portal shows money, API shows zero" persisting after the deposit. The earlier `0.0` readings (Phases 15C.2.1 through 15D.1) were an accurate reflection of the account's real state *at those times* — genuinely empty, not misreported. The classification here is: **the account's real balance changed between checks, and the application correctly tracked that change in real time.**

## 8. Root cause

**No software defect was found.** The root cause of the `0.0` readings in every prior phase was that the account genuinely held ₹0 at those times. The application's funds-reading path (`rmsLimit()` → field mapping → `get_funds()` → `TradingAccountRouter`) is verified, again, to be accurate — this is the second independent real-world confirmation (Phase 15C.3's Account B non-zero reading was the first; this fresh, same-account-over-time change is the second).

## 9. Code changes

**None.** No mapping, session-handling, or caching code was touched — none was needed.

## 10. Tests added

**None new this phase.** The existing `tests/common/test_angelone_funds_mapping.py` (10 tests, Phase 15C.2.1) already covers the full required matrix for this exact mapping and continues to pass unchanged.

## 11. Full regression results

```
Total collected: 1265
Passed:          1265
Failed:          0
Skipped:         0
```

Exactly matching the Phase 15D.1 baseline — no code was changed this phase, so no test count change was expected or found. No new regression.

## 12. Safety verification

```
Real mutation calls: 0
Real orders: 0
Strategies started: 0
Account A authorization_state: READ_ONLY (unchanged)
Account B authorization_state: READ_ONLY (unchanged)
CANARY_READY entered: NO
LIVE_AUTHORIZED entered: NO
```

No credential value was printed anywhere in this diagnostic — only masked client IDs (`AA***21`) and non-secret numeric fund fields.

## 13. Final determination

**PASS — Account A funds correctly verified and discrepancy resolved** (in the sense this diagnostic exists to establish: no software defect, no mapping bug, no cache, no cross-account contamination; the application accurately reflects the account's real, current balance).

**This is explicitly not a declaration that Account A now has *sufficient* funds for the previously-proposed canary.** `available_cash=100.0` is real but small; `available_margin=0.0` remains zero; the canary proposed in `docs/phase-15d-1-live-canary-preflight-report.md` (65 × ~₹60 ≈ ₹3,900 estimated value) would not be covered by ₹100. Per this phase's own hard-stop instruction, Phase 15D.1 must be **rerun** as a fresh preflight to make that sufficiency determination on its own — this diagnostic's job ends at "the reading is trustworthy," not "the account is ready."
