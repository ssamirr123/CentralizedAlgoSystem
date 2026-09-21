# Phase 5A — Real Account Read-Only Validation

Command: `python -m trading.validation.angel_readonly`

Executed against a **real, authenticated Angel One account**, using credentials from the local environment/`trading/.env` only — never printed, never committed, never hard-coded.

---

## Result

```text
==================================================
PHASE 5A -- ANGEL ONE REAL ACCOUNT READ-ONLY VALIDATION
==================================================

ORDER PLACEMENT: DISABLED
ORDER MODIFICATION: DISABLED
ORDER CANCELLATION: DISABLED


==================================================
VALIDATION REPORT
==================================================
AUTHENTICATION: PASS  (real session established)
FUNDS: PASS  (account response received)
NIFTY INSTRUMENT: PASS  (exchange=NSE token=resolved ltp=23398.1)
OPTION INSTRUMENT: PASS  (symbol=NIFTY15SEP2623400CE expiry=2026-09-15 exchange=NFO token=resolved)
LTP: PASS  (symbol=NIFTY15SEP2623400CE ltp=133.6)
POSITIONS: PASS  (0 position(s) returned)
ORDERS: PASS  (0 order(s) returned)

BROKER CONNECTIVITY: PASS  (connect() reached Angel One and returned a session)
ERROR HANDLING: PASS  (invalid-symbol lookup was caught cleanly (ValueError))
MUTATION SAFETY: PASS  (place_order=BLOCKED, modify_order=BLOCKED, cancel_order=BLOCKED)

PHASE 5A = PASS
==================================================
```

## PHASE 5A = **PASS**

All 7 mandatory checks passed, plus all 3 supplementary checks (broker connectivity, error handling, mutation safety).

---

## Checklist Coverage (all 10 items)

| # | Item | Where validated |
|---|---|---|
| 1 | Authentication | `AUTHENTICATION` — real `generateSession()` succeeded |
| 2 | Session/token handling | Subsumed into `AUTHENTICATION`/`BROKER CONNECTIVITY` — a real session object was returned and used for every subsequent call |
| 3 | Account/funds | `FUNDS` — `rmsLimit()` returned real account data |
| 4 | NIFTY instrument lookup | `NIFTY INSTRUMENT` — resolved the NSE spot index (token `99926000`) and fetched a real LTP (23398.1) |
| 5 | NIFTY option instrument lookup | `OPTION INSTRUMENT` — resolved a real, current, non-expired contract (`NIFTY15SEP2623400CE`, expiry 2026-09-15) against the live NFO scrip master |
| 6 | LTP | `LTP` — real premium (133.6) on the resolved ATM option |
| 7 | Positions | `POSITIONS` — real `position()` call, 0 positions (expected, no live trading) |
| 8 | Existing orders | `ORDERS` — real `orderBook()` call, 0 orders |
| 9 | Broker connectivity | `BROKER CONNECTIVITY` — explicit pass/fail on `connect()` reaching Angel One |
| 10 | Error handling | `ERROR HANDLING` — deliberately looked up a symbol that cannot exist and confirmed the failure was caught and classified (`ValueError`), not raised as an unhandled crash |

---

## Mutation Safety — Proven, Not Assumed

`_verify_mutation_safety()` **actually calls** `place_order()`, `modify_order()`, and `cancel_order()` against the real, connected `AngelOneBroker` instance — this is safe because `AngelOneBroker._require_not_read_only()` is the literal first statement in each of those three methods and raises `ReadOnlyModeError` before any SmartAPI call is attempted. All three were confirmed `BLOCKED` against the real session. No `placeOrder`/`modifyOrder`/`cancelOrder` request ever reached Angel One's servers.

---

## Bug Found and Fixed During This Phase

The first run of the new tool succeeded on every check, but the selected option strike (21700) was ~1700 points in-the-money against a spot of 23398 — clearly not the intended near-ATM contract. Root cause: the strike-extraction regex (`\d+CE$`) captured the *entire* trailing digit run, but Angel's symbol format has no separator between the 2-digit-year expiry suffix and the strike (`...SEP26` + `21700` = the contiguous digits `2621700`) — so the regex silently extracted a meaningless composite number instead of the real strike. Fixed by switching to exact positional slicing (mirroring the same 4-digit→2-digit year transformation `DoubleStraddelAlgo/token_file.py` already performs, for the identical reason), verified independently against live data (now correctly resolves strike 23400 for spot 23398.1), and re-run for the final, correct result above. This was a bug in the **new validation tool only** — `AngelOneBroker.resolve_instrument()` and the rest of the adapter were never at fault; a wrong-but-valid symbol was simply handed to it.

---

## Safety Verification

- **No order was placed, modified, or cancelled** — verified both structurally (code inspection: the guard is the first statement in each mutating method) and empirically (the three calls were actually made and confirmed blocked).
- **Credentials** were read only from the environment/`trading/.env` — never printed. All report details were passed through `_sanitize()`, which withholds any text matching a credential-shaped marker.
- **No files under `trading/algos/DoubleStraddelAlgo/*`, `CombinedVwapNifty/*`, or `Vwap_Algo_Nifty_hedge/*` were modified** — confirmed via `git diff --stat`.
- **No strategy behavior was changed.** This tool is not imported by any live algo, `main.py`, or `orders.py`.

## Automated Tests
```
python -m pytest tests/validation/test_angel_readonly.py -v
→ 13 passed

python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ 244 passed
```

## Files Created
```
trading/validation/__init__.py
trading/validation/angel_readonly.py
tests/validation/__init__.py
tests/validation/test_angel_readonly.py
docs/phase-5a-report.md
```

No production/strategy files were modified.
