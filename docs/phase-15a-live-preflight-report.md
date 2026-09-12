# Phase 15A — Live Preflight & Canary Proposal

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` (or equivalent) was called at any point in this phase.** Every broker interaction below used `AngelOneBroker(read_only=True)` explicitly, and mutation safety was independently re-confirmed at the end of the read-only query (all three mutating calls raised `ReadOnlyModeError`).

## Executive Summary

This phase ran the real, read-only Phase 5A validation and a dedicated read-only query against the user's actual Angel One account, then ran the Phase 14 `live_canary` preflight tool for real. All software/configuration-level gates pass. **However, two independent real-world conditions fail the broader Phase 15A preflight**, and no order is proposed for authorization as a result:

1. **Market is closed** — the query ran Saturday 21:34 IST; NSE trading hours are 09:15–15:30 IST, Monday–Friday.
2. **Available funds/margin read as ₹0.00** — either the account genuinely has no available funds/margin right now, or `AngelOneBroker.get_funds()`'s field mapping (already documented since Phase 2 as "best-effort, unverified against a live account") is wrong. This is the **first-ever real invocation** of `get_funds()` in this project; its output cannot be trusted without independent confirmation, and per this phase's own explicit instruction ("verify sufficient available funds/margin — do not assume"), an unverifiable reading is treated as a failed check, not a passed one.

Either condition alone is sufficient to fail the preflight per this phase's rules. Both failing together makes the result unambiguous.

**PHASE 15A: PREFLIGHT FAIL**
**PHASE 15 LIVE CANARY: NOT AUTHORIZED**

---

## Phase 14.6 Verification
`docs/phase-14-6-safety-hardening-report.md` → `PHASE 14.6: PASS` (confirmed by direct read, this phase).

## Phase 14.7 Verification
`docs/phase-14-7-final-live-readiness-validation-report.md` → `PHASE 14.7 STATUS: PASS` (confirmed by direct read, this phase).

Both prerequisites satisfied — proceeded to the real read-only preflight per this phase's own rules.

---

## Broker Verification

```
Broker: Angel One (angelone)
Account identifier (masked): AA***21
Environment: production (real Angel One account, live session)
Authentication status: PASS — real session established via generateSession()
```

No API key, access token, refresh token, password, OTP, or secret was printed or logged at any point — confirmed by direct inspection of every command's output in this phase.

## Account Verification

```
Account identity: VERIFIED (client_id resolved and masked above; name present; email not returned by this account/endpoint)
Account status: session authenticated successfully; no error/lockout indication
```

## Market Verification

```
Market status: CLOSED
Current timestamp (query time): 2026-09-12T16:04:27Z UTC = 2026-09-12T21:34:27 IST, Saturday
NSE trading session: 09:15–15:30 IST, Monday–Friday only
Conclusion: outside trading hours AND outside the trading week (weekend)
```

**This alone fails the preflight** (section 5 / section 13 of this phase's own rules) — proceeding on to the rest of the checks anyway, for completeness of the report and to leave no other question unanswered.

## Instrument Verification

```
Exchange (index): NSE
Exchange (option): NFO
NIFTY spot LTP: 23398.1  (provider timestamp 2026-09-12T16:03:36Z UTC)
Resolved trading symbol: NIFTY15SEP2623400CE
Instrument token: 47293
Expiry: 2026-09-15
Strike: 23400
Option type: CE
Option LTP: 127.8  (provider timestamp 2026-09-12T16:03:46Z UTC)
```

Instrument resolution used **two independent code paths** and cross-checked: `trading.validation.angel_readonly._pick_current_nifty_ce_symbol()` (downloads the public NFO scrip master directly, independent of the adapter's own resolver) and `AngelOneBroker.resolve_instrument()` (the adapter's own default resolver). Both agreed on `NIFTY15SEP2623400CE`. The instrument is unambiguously identified.

### Stale-data assessment (section 13)

```
Data timestamp (option LTP): 2026-09-12T16:03:46Z UTC
Current timestamp (at query): 2026-09-12T16:04:27Z UTC
Mechanical data age: ~41 seconds
```

**Mechanical staleness is fine (under a minute). Substantive staleness is not**: because the market is closed (see above), `127.8` is the **last traded price from the most recent active session**, not a live, tradeable quote. A LIMIT price set against this number today would be set against a number that has no relationship to what the market will actually do whenever it next opens. This is a second, independent reason the data is not fit to found an order proposal on, distinct from the raw age of the API response.

## Funds/Margin Verification

```
Available cash: 0.0
Used margin: 0.0
```

**Cannot be relied upon.** `AngelOneBroker.get_funds()` has been flagged as "best-effort, unverified against a live account" since Phase 2, and this is the literal first time it has ever been called against a real account in this entire project. A reading of exactly `0.0` for *both* fields is consistent with either (a) a genuinely empty account, or (b) `rmsLimit()`'s real response using different field names than `availablecash`/`utiliseddebits` (the names the adapter currently reads), in which case every field silently defaults to `0.0` rather than raising — the adapter has no way to distinguish "the account has ₹0" from "I'm reading the wrong keys." Per this phase's explicit instruction not to assume sufficiency, this check is recorded as **FAILED (indeterminate)**, not passed.

**This is also a legitimate secondary finding for a future phase**: `get_funds()`'s field mapping needs to be verified/corrected against a real `rmsLimit()` response before it can be trusted for a genuine order-sizing decision. This report does not attempt that fix now (out of scope for a preflight-and-proposal phase; fixing it purely to make this specific canary "pass" would be exactly the kind of gate-adjustment this phase is forbidden from doing).

## Existing Positions

```
Position count: 0
```
No conflicting real positions.

## Existing Orders

```
Order count: 0
```
No conflicting open real orders. (Angel's adapter has no separate "trades" endpoint distinct from the order book; the order book itself — empty — is the closest available proxy and was checked.)

---

## Risk Limits

Configured for this dry run (never applied to any real execution):

```
max_order_quantity:   65     (exactly 1 NIFTY lot — see LOT_QTY in
                               trading/algos/DoubleStraddelAlgo/config.py;
                               NIFTY options cannot trade in fractional-lot
                               quantities, so 65 — not 1 — is the smallest
                               PRACTICAL quantity, per this phase's own
                               instruction to use "the smallest practical
                               order size supported by the instrument")
max_order_value:      10000.0
max_daily_loss:       5000.0
max_strategy_loss:    5000.0
max_order_count:      1       (max_orders_per_day=1 — exactly as required)
```

`python -m trading.preflight.live_canary` (run for real, this phase) reported `CONFIGURATION: PASS` for these exact values.

**Note**: `LiveCanaryGuard`/`CanaryLimits` (Phase 14) does not itself carry exposure fields — only plain-LIVE's `RiskLimits.is_live_ready()` (Phase 14.6 Blocker E) requires `max_strategy_exposure`/`max_account_exposure`. For a `LIVE_CANARY`-mode account (which this proposal targets), only the five `CanaryLimits` fields above are the binding, enforced caps — this is by design (see `trading/common/live_canary.py`), not an oversight in this report.

## Execution Configuration

```
ExecutionMode:            LIVE_CANARY (proposed — not currently active anywhere)
Broker:                   angelone
TradingAccount:           ANGEL_CANARY (proposed account_id — see caveat below)
Strategy:                 DoubleStraddelAlgo
LiveCanaryGuard:          not yet instantiated as a live object (would be constructed
                          fresh, with the CanaryLimits above, only at actual
                          authorization time)
Central Kill Switch:      OFF (no CentralKillSwitch has been engaged anywhere in
                          this session; default-safe state)
RiskManager:              class verified functional (Phase 14 preflight's own
                          RISK MANAGER dry-run check: PASS)
Persistent Idempotency
  Store:                  HEALTHY — independently verified this phase via a direct
                          get/put round-trip against a real SqliteIdempotencyStore
                          file (see below); NOT itself checked by the Phase 14
                          preflight tool (a coverage gap — see Known Gaps)
Broker Adapter:           HEALTHY — real session established, real reads succeeded
                          (funds, quotes, positions, orders), mutation blocked
```

### ⚠️ Dedicated-account caveat — disclosed, not hidden

Requirement #1 (dedicated trading account) is satisfied **at the software level only**: `ANGEL_CANARY` is a distinct `account_id` string used exclusively for this canary's bookkeeping (idempotency records, `LiveCanaryGuard.CanaryLimits.account_id`, audit trail). It is **not** a separate broker login — there is exactly one real Angel One credential set configured in `trading/.env`, so `ANGEL_CANARY` and any other account label would resolve to the **same real underlying brokerage account**. True broker-level account segregation (a genuinely separate demat/trading account) does not exist for this user today. This is a real limitation of the current setup, not a bug in this phase's software, and is surfaced here explicitly rather than glossed over.

### `python -m trading.preflight.live_canary` — full real output

```
CONFIGURATION: PASS
DEDICATED_ACCOUNT: PASS  (software-level distinctness only -- see caveat above)
BROKER ADAPTER VALIDATED: PASS
REAL ACCOUNT VALIDATION: PASS  (live re-run of Phase 5A, this phase)
RISK MANAGER: PASS
CANARY GUARD DRY RUN: PASS
KILL SWITCH: PASS
EMERGENCY SHUTDOWN: PASS
OBSERVABILITY WIRED: PASS
CONTROL CENTER BACKEND IMPORTABLE: PASS
BROKER ADAPTER IS SOLE ORDER CALLER: PASS
NO AUTOMATIC ORDER PLACEMENT: PASS

PHASE 14 = PASS
```

Every check the Phase 14 tool itself performs passes. It does not check market-open status or real funds sufficiency — those are new, additional real-world checks this Phase 15A introduces, and they are the ones that fail.

## LiveCanaryGuard

Not instantiated as a live object during this phase (no order path was exercised) — its construction and `authorize()` logic were already exercised, correctly, by the preflight tool's own `CANARY GUARD DRY RUN` check above, against the exact `CanaryLimits` this proposal would use.

## Kill Switch
`CentralKillSwitch`: OFF. Never engaged in this session. No mechanism in this codebase can engage it automatically.

## Idempotency

```
Idempotency store file: none exists yet anywhere in this repository (confirmed —
  no prior real execution has ever happened; verified via a direct filesystem
  check this phase)
Idempotency store health: HEALTHY (independently verified this phase — a fresh
  SqliteIdempotencyStore round-tripped a get/put against a real temporary SQLite
  file)
Proposed idempotency key: PHASE15-CANARY-20260912-DoubleStraddelAlgo-NIFTY15SEP2623400CE-BUY-65
Prior execution check: this exact key cannot correspond to any previous real
  execution -- no idempotency store file has ever been created in this project
  until a real order is actually authorized and executed.
```

---

## Proposed Order

**This is informational only, per this phase's rules — not a ready-to-submit order, because the preflight above FAILED.** Presented for completeness so the full pipeline shape is visible, and because producing it was explicitly requested regardless of preflight outcome.

```
Strategy:                  DoubleStraddelAlgo
Broker:                    Angel One (angelone)
Trading account:           ANGEL_CANARY  (see dedicated-account caveat above)
Exchange:                  NFO
Symbol:                    NIFTY15SEP2623400CE
Instrument token:          47293
Expiry:                    2026-09-15
Strike:                    23400
Option type:               CE
Side:                      BUY
Quantity:                  65  (exactly 1 lot — the minimum tradable unit)
Order type:                LIMIT
Product type:              INTRADAY
Estimated price:           127.8  (Angel's last-traded price — STALE, market closed; see
                            Market Verification above. Not usable as a real limit price
                            without a fresh, live quote once the market reopens.)
Estimated order value:     127.8 x 65 = 8,307.00  (well within max_order_value=10000, IF
                            the price were current)
Maximum permitted exposure (this canary): 10,000.00 (max_order_value), no separate
                            exposure cap for LIVE_CANARY by design (see above)
```

### Why BUY, not SELL — a deliberate, disclosed departure from the live strategy's own logic

`DoubleStraddelAlgo`'s real, live trading logic **sells** straddles (SELL CE + SELL PE) — every reference to this strategy throughout this project's prior phases used SELL as the illustrative example for exactly that reason. For this proposal, **BUY** is recommended instead: a long option's maximum loss is capped at the premium paid (≈₹8,307 here, already within the configured `max_order_value`), whereas a naked short option carries theoretically large, margin-dependent risk that is a poor fit for "prove the pipeline works with the smallest possible real-money exposure." This is a deliberate choice specific to de-risking a first-ever real canary, **not** a claim that this is what `DoubleStraddelAlgo` would normally do — flagged here explicitly for human review rather than silently substituted.

## OrderIntent Preview

```
OrderIntent
{
    strategy_id:       "DoubleStraddelAlgo",
    account_id:        "ANGEL_CANARY",
    exchange:           "NFO",
    symbol:             "NIFTY15SEP2623400CE",
    side:               OrderSide.BUY,
    quantity:           65,
    order_type:         OrderType.LIMIT,
    product_type:       ProductType.INTRADAY,
    limit_price:        <NOT SET -- no current, live, tradeable price exists;
                         market is closed. A real limit price can only be set
                         from a live quote once the market reopens.>,
    idempotency_key:    "PHASE15-CANARY-20260912-DoubleStraddelAlgo-NIFTY15SEP2623400CE-BUY-65",
}
```

This object was constructed **only as text in this report** — it was never instantiated as a real `trading.common.order_intent.OrderIntent` object passed to any `RiskManager`, `LiveCanaryGuard`, or `StrategyExecutionEngine`, and it was never passed to any broker API.

---

## Preflight Result

```
PHASE 15A: PREFLIGHT FAIL
```

Failing checks:
1. **Market status** — CLOSED (Saturday, outside trading hours).
2. **Funds/margin sufficiency** — indeterminate/failed (reads ₹0.00; first-ever real check of this field, cannot be trusted without independent verification per this phase's own "do not assume" instruction).

All other checks passed (broker auth/connectivity, account identity, instrument resolution, zero conflicting positions/orders, risk-limit configuration validity, execution-configuration health, idempotency-store health, kill switch OFF, and the full Phase 14 software preflight).

## Order-Placement Verification

```
Real orders submitted: 0
Broker order-placement calls: 0
```

Verified by: (a) direct, explicit re-confirmation this phase — `place_order`/`modify_order`/`cancel_order` were each called once against the real, connected session and each raised `ReadOnlyModeError` before any network request; (b) the Phase 14 preflight tool's own `BROKER ADAPTER IS SOLE ORDER CALLER` and `NO AUTOMATIC ORDER PLACEMENT` structural checks, both PASS; (c) no code path in this entire phase ever constructed a real `StrategyExecutionEngine` or called `.execute()`.

## Final Human Authorization Requirement

**Not applicable this run** — no order proposal reached an authorizable state, because the preflight failed. Per this phase's own rules, `PREFLIGHT FAIL` must never be interpreted as, or silently upgraded into, an authorization request. None is being made.

Were the preflight to pass on a future run (market open, funds genuinely verified sufficient), the human authorization block below is the exact format that would be produced — shown here for reference only, **not as a live request**:

```
========================================================
PHASE 15 LIVE CANARY — HUMAN AUTHORIZATION PROPOSAL
========================================================

Broker:              Angel One (angelone)
Account:             ANGEL_CANARY (same underlying real account as all others --
                     see dedicated-account caveat)
Strategy:            DoubleStraddelAlgo

Exchange:            NFO
Symbol:              NIFTY15SEP2623400CE
Instrument Token:    47293
Expiry:              2026-09-15
Strike:              23400
Option Type:         CE

Side:                BUY
Quantity:            65
Order Type:          LIMIT
Product Type:        INTRADAY

Current LTP:         <would be refreshed live -- 127.8 is STALE, market closed>
Proposed Price:      <NOT SET -- requires a live quote>
Estimated Order Value: <requires live price>

Maximum Order Value:    10,000.00
Maximum Exposure:       10,000.00 (no separate exposure cap for LIVE_CANARY)
Maximum Daily Loss:     5,000.00
Maximum Strategy Loss:  5,000.00

Idempotency Key:     PHASE15-CANARY-<date-of-actual-run>-DoubleStraddelAlgo-NIFTY15SEP2623400CE-BUY-65

Execution Mode:
LIVE_CANARY

Current Kill Switch:
OFF

Preflight:
FAIL  (this run)

========================================================
IMPORTANT
========================================================

THIS IS A PROPOSAL ONLY.

NO BROKER ORDER HAS BEEN SUBMITTED.

NO PLACE-ORDER API HAS BEEN CALLED.

HUMAN AUTHORIZATION REQUIRED:
YES

========================================================
```

---

## Known Gaps Surfaced by This Phase (for a future phase, not fixed now)

1. `AngelOneBroker.get_funds()`'s field mapping is unverified against a real response shape and must be confirmed/corrected before any real order-sizing decision can trust it.
2. `python -m trading.preflight.live_canary` (Phase 14) does not itself check `IdempotencyStore` health or market-open status — both were checked manually in this phase instead. A future revision of that tool could incorporate both checks directly.
3. True broker-level account segregation for the canary does not exist (single real Angel One login) — see the dedicated-account caveat above.

None of these were fixed in this phase, per its own scope (preflight-and-proposal only) and its explicit prohibition on modifying gates to make the canary succeed.

---

PHASE 15A: PREFLIGHT FAIL

PHASE 15 LIVE CANARY: NOT AUTHORIZED
