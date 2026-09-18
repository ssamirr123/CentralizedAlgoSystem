# Phase 15D.1 — Controlled Single-Account Live Canary: Preflight & Readiness

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` API was ever called. No strategy was started. Neither account was transitioned to `CANARY_READY` or `LIVE_AUTHORIZED`.**

## Baseline

```
Phase 15C.5 = PASS
Previous regression = 1249 passed / 0 failed / 0 skipped
```

Confirmed unchanged at the start of this phase: `docs/phase-15c-5-final-multi-account-read-only-gate-report.md`.

## Selected Account

```
Account = A (ANGEL_SAMIR)
Broker = Angel One
Credential reference = env:ANGELONE_A
Authorization = READ_ONLY (confirmed before and after this phase's every real call)
```

Account A was selected per this phase's own default instruction (no explicit reason existed to prefer Account B). Account B (`ANGEL_ACCOUNT_B`) was never authenticated, never queried, and never referenced by any canary-specific object (`LiveCanaryGuard`, `StrategyAssignment`) constructed in this phase — see "Account B Protection" below.

## Real Read-Only Validation

```
Authentication = PASS   (real generateSession() succeeded)
Identity       = PASS   (client_id masked AA***21, matches every prior phase)
Funds          = FAIL — see "Decisive Finding" below
Positions      = PASS   (count = 0)
Orders         = PASS   (count = 0)
Market data    = PASS   (NIFTY LTP = 23118.6, live at time of check; market was CLOSED at check time — see below)
```

### Market status at time of check

```
Date/time: 2026-09-15, 16:44 IST (Tuesday)
Market session: 09:15-15:30 IST
Market status: CLOSED (session already ended for the day)
```

Per this phase's Step 5, this does not block the preflight itself — non-mutating checks continued — but it does mean no order could be meaningfully submitted right now even if every other gate passed.

### Decisive finding: funds

```
available_cash:   0.0
used_margin:      0.0
available_margin: 0.0
```

This is the exact same, now twice-independently-verified figure from Phase 15C.2.1 (`funds_mapping_status = VERIFIED_ZERO`, confirmed via direct raw-response inspection) and Phase 15C.3/15C.4/15C.5's repeated real checks. This is **no longer an ambiguous or unverified reading** — it is a confirmed, genuine zero balance, re-checked fresh at the start of this specific phase.

Per this phase's own explicit Step 4 rule: **"If funds are insufficient for the proposed canary: BLOCKED. Do NOT proceed by assuming sufficient funds."** A genuinely zero cash balance is insufficient to fund any order of any size, however small. This is treated as a hard blocker for this phase's overall result, independent of how well every other system-readiness check performs.

## Proposed Canary

**PROPOSED ONLY — NOT SUBMITTED.**

```
Account:              ANGEL_SAMIR
Strategy:              DoubleStraddelAlgo
Symbol:                NIFTY <nearest-expiry ATM CE, resolved dynamically at real authorization time>
Exchange:              NFO
Side:                  BUY   (caps downside to premium paid -- same reasoning as Phase 15A's proposal)
Quantity:              65    (exactly 1 lot -- trading/algos/DoubleStraddelAlgo/config.py's LOT_QTY, the
                              minimum tradable unit; not chosen merely because it clears the limits)
Order type:            LIMIT
Price:                 <not fixed here -- would be taken from a live quote at actual authorization time;
                              a conservative illustrative figure of 60.0 was used only to size-check
                              against CanaryLimits/RiskLimits in this preflight's tests>
Estimated value:       65 x 60.0 = 3,900.00 (illustrative; well within max_order_value)
Maximum allowed loss:  bounded by max_daily_loss/max_strategy_loss = 5,000.00 each (CanaryLimits, below)
Expected purpose:      prove the full LIVE_CANARY pipeline executes exactly once, safely, with real money
                              at the smallest practical size -- not a strategy deployment
```

This specification exists only as text in this report and as literal values inside `tests/common/test_phase_15d_1_canary_preflight.py` — it was never constructed as a live `OrderIntent` passed to any real `StrategyExecutionEngine`, and never passed to any broker API.

## Safety

```
RiskLimits            = PASS (synthetic LIVE-ready limits validated the proposed order; RiskManager.validate() → ALLOW, no broker call)
CanaryLimits          = PASS (every field explicit/positive; proposed order within max_order_quantity and max_order_value)
LiveCanaryGuard       = PASS (authorizes the exact proposed order; rejects wrong-account, kill-switch-engaged,
                              missing-idempotency-key, duplicate-intent, and oversized-quantity cases)
Authorization boundary = PASS (the Phase 15B.1 AuthorizationState gate rejects Account A's REAL current
                              state (READ_ONLY) before LiveCanaryGuard is ever reached -- re-confirmed with
                              this phase's own exact proposed intent)
Human approval gate    = PASS (structural proof: no production code path anywhere under trading/ assigns
                              authorization_state = CANARY_READY or LIVE_AUTHORIZED; grep + AST-scan found
                              zero such assignments outside the enum's own definition. The only route to
                              either state is a human manually constructing/mutating a TradingAccount --
                              exactly the explicit approval this step requires. No automatic approval path
                              exists anywhere in the codebase.)
Idempotency            = PASS (the proposed key is account- and intent-specific: hashes differ across
                              accounts and across a changed quantity; identical requests hash identically)
Broker response validation = PASS (unchanged; covered by tests/common/test_broker_response_validation.py --
                              not re-tested here, no code was modified)
Kill switch            = PASS (default disengaged; engages/disengages correctly in isolation; production
                              kill-switch state was never touched by this phase)
Account B protection   = PASS (see below)
Audit logging          = PASS (unchanged; StrategyExecutionEngine's existing AUTHORIZATION_STATE_GATE /
                              RISK_DECISION / LIVE_CANARY_AUTHORIZATION events already cover every field
                              this step requires -- timestamp, account_id, strategy_id, symbol, side,
                              quantity, order_type, risk/authorization decisions; no code changed)
Credential security    = PASS (no credential value appears anywhere in this report, the new test file, or
                              any script run this phase -- only masked client IDs and field names)
```

### Account B protection

Account B (`ANGEL_ACCOUNT_B`) was: never authenticated in this phase; never given a `StrategyAssignment`; never referenced by the `LiveCanaryGuard` constructed for Account A's proposed canary (that guard's own `CanaryLimits.account_id` is fixed to `ANGEL_SAMIR`, and `test_account_b_remains_read_only_and_not_referenced_by_the_canary_guard` proves an attempt to route Account B through it is rejected with `DEDICATED_ACCOUNT`); and remains `authorization_state = READ_ONLY` by its own class default, untouched by anything in this phase.

## Mutation Safety

```
Real mutation calls: 0
Real orders: 0
Strategies started: 0
```

Verified: the only mutating calls made anywhere in this phase were the three explicit `place_order`/`modify_order`/`cancel_order` negative-proof calls against Account A's real, `read_only=True` adapter — all three raised `ReadOnlyModeError` before any network request, exactly as in every prior phase.

## Regression

```
New test file this phase: tests/common/test_phase_15d_1_canary_preflight.py -- 16 tests, all passing
```

Full regression suite result recorded at closure (see Final Decision below) — run immediately before this report was finalized, no test was modified or weakened to pass.

## Final Decision

**PHASE 15D.1 STATUS: BLOCKED**

Every system-readiness check this phase requires — identity, credential/broker-factory/router correctness, RiskLimits, CanaryLimits, LiveCanaryGuard (including all its negative cases), the authorization boundary, the human-approval structural gate, idempotency, broker-response validation, kill switch, Account B exclusion, audit coverage, and credential security — **passed**. The full regression suite introduced no new failure.

However, per this phase's own explicit Step 4 rule, a genuinely, repeatedly-confirmed zero real cash balance in the selected account is a hard blocker to proceeding toward any live canary, however small. This is **not** the same finding as Phase 15C.2's original ambiguous blocker (which was later resolved to `VERIFIED_ZERO` in Phase 15C.2.1) — it is the same confirmed-genuine zero, now checked a further time and still zero, which is exactly why it blocks a *real-money* canary specifically, even though it did not block any of the purely read-only validation phases before it.

The system is **ready** the moment the account is funded. No code change, no test change, and no additional validation is required for readiness itself — only real capital in the real account.

---

# FRESH REVALIDATION (this run supersedes the original run above for final status purposes)

## 1. Previous blocker

The original Phase 15D.1 run (above) was BLOCKED: Account A's real `available_cash` was genuinely `0.0`.

## 2. Phase 15D.1.1 resolution

`docs/phase-15d-1-1-account-a-funds-reconciliation-report.md` (PASS): no software defect was found in the funds-reading path. Account A's balance genuinely changed from `0.0` to `100.0` due to a real deposit between checks, correctly and immediately tracked by the application, with zero caching and zero cross-account contamination (re-confirmed with a fresh `A → B → A` read). That diagnostic explicitly did **not** declare the account funds-sufficient — it deferred that determination to this fresh Phase 15D.1 rerun.

## 3. Fresh Account A authentication

```
credential_reference: env:ANGELONE_A
authenticated identity (masked): AA***21
matches every prior phase: YES
authorization_state: READ_ONLY (before and after)
```

## 4. Fresh funds response

```
available_cash:   100.0
used_margin:      0.0
available_margin: 0.0
```

Stable and consistent with Phase 15D.1.1's finding — not a new fluctuation, the same real, small balance.

## 5. Funds sufficiency calculation

Evaluated against the proposed canary order (Section "Proposed Canary" above: 65 qty, NIFTY ATM option, BUY):

```
Proposed quantity:                65 (1 lot, fixed by instrument lot size — not adjustable downward)
Realistic option premium range observed this project (multiple real checks, same expiry cycle): ₹40 - ₹130
Estimated order value range:      65 x 40  = ₹2,600   to   65 x 130 = ₹8,450
Account A available_cash:         ₹100
Account A available_margin:       ₹0
Coverage ratio (best case, lowest premium end): 100 / 2,600 ≈ 3.8%
Shortfall (best case):            ₹2,500+
```

**Funds are decisively, unambiguously insufficient** — not a borderline or close call. ₹100 does not cover even 4% of the lowest realistic estimated cost of a single, minimum-size (1 lot) NIFTY option purchase. No threshold was invented for this conclusion: the comparison is a direct order-value-vs-available-balance calculation using this project's own already-observed real premiums and the instrument's own fixed lot size (`LOT_QTY = 65`, not chosen or reduced to make the numbers work).

This determination did **not** modify `RiskLimits` or `CanaryLimits` — both remain exactly as configured; the failure is a real-world funds-availability fact, not a limits-configuration issue.

## 6. Proposed canary order

Unchanged from the original run's specification above (Section "Proposed Canary") — **PROPOSED ONLY — NOT SUBMITTED.**

## 7. RiskLimits result

**PASS (structurally)** — unchanged from the original run; the synthetic LIVE-ready `RiskLimits` used in `tests/common/test_phase_15d_1_canary_preflight.py` still validates the proposed order as a synthetic decision. RiskLimits do not themselves check account balance (that is a broker-level fact RiskManager does not fetch — see its own module docstring) — this is exactly why funds sufficiency needed a separate, explicit real-world check (Section 5), and why "RiskLimits passes" and "the account has enough money" are two different, independently-necessary conditions.

## 8. CanaryLimits result

**PASS (structurally)** — unchanged; every field remains explicit and positive, and the proposed order remains within `max_order_quantity`/`max_order_value`. Same caveat as RiskLimits: `CanaryLimits` bounds the maximum an order is *allowed* to be — it does not verify the account can actually *afford* it.

## 9. LiveCanaryGuard result

**PASS (structurally)** — unchanged; the guard's 9 pre-broker checks (dedicated account, quantity, order value, daily/strategy loss, order count, kill switch, duplicate protection, idempotency-required) all still pass for the exact proposed intent, as proven in `tests/common/test_phase_15d_1_canary_preflight.py`. `LiveCanaryGuard` likewise has no field for "does the account actually have this much cash" — it is a policy/limits gate, not an account-balance gate.

## 10. Account B protection

Unchanged and re-confirmed: Account B was not touched by this rerun's real calls at all (only Account A was authenticated/read this time). `authorization_state = READ_ONLY` by its own default, no `StrategyAssignment`, no reference from Account A's `LiveCanaryGuard`.

## 11. Idempotency

Unchanged — `tests/common/test_phase_15d_1_canary_preflight.py::test_proposed_idempotency_key_is_account_and_intent_specific` still passes; no code touched.

## 12. Broker-response validation

Unchanged — `tests/common/test_broker_response_validation.py` unaffected; no code touched.

## 13. Kill switch

Unchanged — default disengaged; production kill-switch state untouched by this rerun.

## 14. Audit

Unchanged — existing `AUTHORIZATION_STATE_GATE`/`RISK_DECISION`/`LIVE_CANARY_AUTHORIZATION` events already cover every required field; no code touched.

## 15. Regression results

```
Total collected: 1265
Passed:          1265
Failed:          0
Skipped:         0
```

Exact match to Phase 15D.1.1's baseline, as expected — no code or test was changed in this rerun (pure revalidation). No new regression.

## 16. Real mutation/order counts

```
Real mutation calls: 0
Real orders: 0
Strategies started: 0
```

Re-confirmed: `place_order`/`modify_order`/`cancel_order` were each called once against Account A's real, connected, `read_only=True` session specifically to prove `ReadOnlyModeError` is raised — none reached Angel One's real order-placement API.

## 17. Final authorization state

```
Account A authorization_state: READ_ONLY (unchanged throughout)
Account B authorization_state: READ_ONLY (unchanged throughout)
CANARY_READY entered: NO
LIVE_AUTHORIZED entered: NO
```

## FINAL STATUS (fresh rerun)

**PHASE 15D.1 STATUS: BLOCKED**

Every structural/system-readiness gate passed cleanly, exactly as in the original run — the only material change this rerun found is that Account A's zero-funds finding is now confirmed as a real (not merely persistent) balance of ₹100, and that balance is **decisively insufficient** for the smallest practical version of the proposed canary (a single 1-lot NIFTY option purchase, minimum realistic cost ~₹2,600). This is not a system defect and not something further diagnosis or code change can resolve — the account requires substantially more real capital before a live canary of this shape is fundable.

---

# SECOND FRESH REVALIDATION — funding landed in Account B, not Account A

A follow-up instruction asserted "Account A has now been funded." A fresh, real check performed specifically to verify this before proceeding found:

```
Account A (ANGEL_SAMIR), fresh real check:
  available_cash:   100.0   (UNCHANGED from every prior check)
  used_margin:       0.0
  available_margin:  0.0
  positions: 0   orders: 0
  identity: AA***21 (matches, session valid)
  mutation_block: place_order/modify_order/cancel_order all BLOCKED (ReadOnlyModeError)
```

Account A's real balance did **not** change. The user then clarified mid-session that the deposit had actually gone to **Account B**, not Account A. A fresh, real, read-only check of Account B (`ANGEL_ACCOUNT_B`, `env:ANGELONE_B`) confirms this directly:

```
Account B (ANGEL_ACCOUNT_B), fresh real check:
  available_cash:   3094.83   (up from 94.83 in every prior phase -- a real ₹3,000 deposit)
  used_margin:       0.0
  available_margin:  0.0
  positions: 0   orders: 4
```

## Conclusion for THIS phase (Account A)

**PHASE 15D.1 STATUS: BLOCKED (unchanged).** Account A remains exactly as insufficiently funded as every prior check found. Nothing in this phase's scope — a preflight specifically for Account A — changes as a result of Account B's new balance. Per this project's own standing rule (explicitly stated in Phase 15D.1.1's own safety rules: **"Do NOT use Account B funds to satisfy Account A requirements"**), Account B's funds cannot be used, borrowed, or substituted to satisfy Account A's canary preflight in any way, and this report does not do so.

## Note, not acted upon: Account B is now a real, funded candidate

This is reported for visibility only — **no action was taken on it**. Account B now holds a real, substantial balance (₹3,094.83) that would very plausibly cover the smallest practical canary order (~₹2,600–8,450 estimated range for a single 1-lot NIFTY option). Whether to run a dedicated Phase 15D.1-equivalent preflight *for Account B specifically* is a decision this report does not make unilaterally: the currently-authorized `LiveCanaryGuard`/`CanaryLimits` object built in this phase is explicitly dedicated to `ANGEL_SAMIR` (Account A) via `CanaryLimits.account_id` — attempting to route an Account B intent through it would itself be correctly rejected (`DEDICATED_ACCOUNT`), by design. A genuine Account-B canary preflight would need its own explicitly-constructed `CanaryLimits(account_id="ANGEL_ACCOUNT_B", ...)` and its own fresh walk through every step of this phase — not a reuse of Account A's artifacts — and only if and when explicitly requested.

```
Real mutation calls this check: 0
Real orders this check: 0
Account A authorization_state: READ_ONLY (unchanged)
Account B authorization_state: READ_ONLY (unchanged)
CANARY_READY entered: NO
LIVE_AUTHORIZED entered: NO
```
