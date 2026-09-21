# Phase 15D.2 — Human-Authorized Controlled Live Canary

**No real order was placed. No mutation API was called. No human authorization request was presented for an order that could not actually execute. Account A and Account B remain `READ_ONLY`.**

## Preconditions checked (fresh, real, read-only)

```
Timestamp of this check: 2026-09-16, 15:48-15:49 IST
Market session: 09:15-15:30 IST
Market status: CLOSED (session ended ~18-19 minutes before this check)
```

```
Account B authentication: PASS
Account B identity: PASS (masked AA***01, consistent with every prior phase)
Account A isolation: PASS (Account A not touched by any real call this phase)
Credential isolation: PASS (env:ANGELONE_B used exclusively)
```

### Fresh funds

```
available_cash:   3019.4003
used_margin:      75.4297
available_margin: 0.0
positions: 2 (both flat, unchanged)
orders: 8 (unchanged from the prior check)
```

Unchanged from the Phase 15D.1-B OTM preflight — `FUNDS PRECHECK` would have passed on its own numeric merits.

### Fresh instrument re-validation

```
Symbol:    NIFTY22SEP2623600CE
Token:     57031 (matches the preflight's token — consistent, not a different instrument)
Resolved:  YES
Fresh LTP: 29.40 (preflight LTP was 30.20 — a small, expected intraday-style move, still real and current)
```

`INSTRUMENT PRECHECK` would also have passed on its own — the contract is still valid, non-expired, and correctly mapped.

## Decisive blocker: market is closed

```
FUNDS PRECHECK: would have PASSED
INSTRUMENT PRECHECK: would have PASSED
MARKET STATUS: CLOSED — DECISIVE BLOCKER
```

The regular NSE trading session ended at 15:30 IST. This check was performed at ~15:49 IST — after close. The approved canary order's product type (`INTRADAY`) cannot be accepted by the broker outside the regular session regardless of how current the quote data looks; Angel's `ltpData` endpoint continuing to return a same-moment timestamp reflects the last traded price, not that new orders are currently acceptable.

Per this project's own consistent rule across every prior phase (Phase 5A, 15A, 15C.2, 15D.1, 15D.1-B): a market-closed condition is always treated as blocking for anything execution-adjacent, never as something to route around. Attempting to present the Section 9 "final human authorization" gate for an order that cannot actually be submitted right now would be misleading, not merely premature — so it was not presented. No human authorization was requested for a specific order this phase, and none was needed to be, since no order could have proceeded regardless of the answer.

## Human authorization

```
Human authorization: NOT REQUESTED (would have been misleading given market-closed; see above)
Authorization timestamp: N/A
Authorized account: N/A
```

Per this phase's own explicit rule ("Do NOT interpret any of the following as authorization: Phase 15D.1-B PASS, previous conversation approval, configuration value, environment variable, ... a previous authorization, automated test success"), nothing about this task's own detailed instructions was treated as authorization for the specific order, and no fabricated or assumed confirmation was used.

## Order

```
Not constructed as a submittable OrderIntent this phase -- the preconditions gate stopped before Section 6.
```

## Broker result / Reconciliation

```
Not applicable — no order was submitted.
```

## Safety

```
RiskLimits:            Not exercised this phase (gate stopped before Section 7) — unchanged from Phase 15D.1-B
CanaryLimits:          Not exercised this phase — unchanged from Phase 15D.1-B
LiveCanaryGuard:        Not exercised this phase — unchanged from Phase 15D.1-B
Idempotency:            Not exercised this phase (no intent was constructed to key)
Kill switch:            Unchanged — default disengaged, production state untouched
Account A isolation:    PASS
Credential isolation:   PASS
Audit logging:          N/A — no execution event occurred to audit
```

## Execution count

```
Real mutation calls: 0
Real orders submitted: 0
Strategies started: 0
Additional orders: 0
```

## Regression

Not re-run this phase — no code was changed (only real, read-only diagnostic calls were made). Last confirmed baseline: `1306 passed / 0 failed / 0 skipped` (Phase 15D.1-B OTM preflight).

## FINAL STATUS

**PHASE 15D.2 FINAL STATUS: BLOCKED**

Reason: market closed (15:30 IST session end; checked at ~15:49 IST). Funds and instrument preconditions both would have passed independently — this is purely a timing blocker, not a capital, risk, or instrument defect.

## What must happen before this can be re-attempted

1. Re-run during live market hours (09:15–15:30 IST, a trading day).
2. Re-verify funds and instrument freshness at that time (do not reuse this session's numbers — they will be stale by the next trading session).
3. Only then present the Section 9 final human-authorization gate with genuinely fresh, live, actionable numbers, and wait for an explicit, separate confirmation before any mutating call.

No workaround was attempted for the market-closed condition, per this project's established practice throughout every prior phase.

---

# ATTEMPT 2 — 2026-09-17: Real, Human-Authorized Live Canary Attempt

Market was open this time (Thursday 2026-09-17, checked repeatedly between
10:23 and 11:00 IST, always within the 09:15–15:30 session, corroborated
live via two real NIFTY spot samples 8 seconds apart showing genuine price
movement each time). A full fresh preflight was run, a configuration
blocker (no `CANARY_*` limits existed anywhere) was found and reported to
the user rather than invented, the user supplied explicit limit values,
a final fresh validation gate was presented and re-verified, and the user
gave explicit, unambiguous authorization for the exact order shown. A real
order-placement call was then made. It was **rejected by the broker**.

## Configuration

```
CANARY_ACCOUNT_ID=ANGEL_ACCOUNT_B
CANARY_MAX_ORDER_QUANTITY=65        (units — verified against live_canary.py:222
                                      and angelone.py:355; NOT lots. 65 units =
                                      1 lot at the current NIFTY lot size, so the
                                      human-supplied value needed no adjustment.)
CANARY_MAX_ORDER_VALUE=2000
CANARY_MAX_DAILY_LOSS=2000
CANARY_MAX_STRATEGY_LOSS=2000
CANARY_MAX_ORDERS_PER_DAY=1
```

Supplied explicitly by the user (2026-09-17) after this session correctly
declined to invent these safety-critical, no-defaults-by-design values
itself. Appended to `trading/.env`.

## Fresh preflight (multiple re-validations, most recent immediately before submission)

```
Timestamp (final gate):        2026-09-17T10:52:47+05:30
Timestamp (submission-moment): 2026-09-17T11:00:15+05:30
Market status:                 OPEN (weekday + session-hours check; corroborated
                                by live spot-price movement across real samples)
Account B identity:            client_id AA***01, name "SAMIR KUILA" — confirmed live
Account A isolation:           PASS — no TradingAccount/broker ever constructed for
                                ANGEL_SAMIR in any script this phase; env:ANGELONE_A
                                never resolved
Credential isolation:          PASS — only env:ANGELONE_B resolved throughout
Open positions (B), pre:       0
Existing orders today (B), pre: 0
```

### Instrument (fresh, re-verified twice)

```
Symbol:   NIFTY22SEP2623600CE
Strike:   23600
Option:   CE
Expiry:   2026-09-22 (confirmed still tradable both times)
Lot size: 65 (from live broker scrip master)
```

One substitution occurred earlier in this session's own preflight
sequence (documented and reported at the time, not silently made): the
first fresh scan's only candidate in the user's preferred ₹25–35 premium
band (`NIFTY22SEP2623550CE`) exceeded the hard `CANARY_MAX_ORDER_VALUE`
limit and was correctly rejected, not overridden; `NIFTY22SEP2623600CE`
(just under the preferred band, but within every hard limit) was used for
every subsequent step, including this final authorized attempt — an
explicit, human-visible instrument, not an invented one.

### Fresh market data at the two most important moments

| Moment | Spot | LTP | Estimated value |
|---|---|---|---|
| Final gate presented | 23269.25 | 22.45 | ₹1,459.25 |
| Immediately before submission | 23264.90 | 21.75 | ₹1,413.75 |

Both comfortably under `CANARY_MAX_ORDER_VALUE=2000`. The option remained
genuinely OTM (spot < strike) at every check.

### Funds

```
Available cash (final gate):        3005.44
Available cash (immediately before): 3005.44
Sufficient: YES at every check
```

## Risk / Canary safety pipeline (all PASS)

```
RiskManager.validate(): ALLOWED — all 15 checks PASS
  (STRATEGY_ENABLED, ACCOUNT_ENABLED, EXECUTION_MODE_ALLOWED,
   INSTRUMENT_VALIDATION, MAX_ORDER_QUANTITY, MAX_POSITION_QUANTITY,
   MAX_STRATEGY_EXPOSURE, MAX_ACCOUNT_EXPOSURE, MAX_DAILY_LOSS,
   MAX_STRATEGY_LOSS, DUPLICATE_ORDER_PROTECTION,
   MARKET_SESSION_VALIDATION, KILL_SWITCH, ORDER_VALUE_LIMIT,
   MAX_ORDERS_PER_DAY)

CanaryLimits structural checks (pre-submission): DEDICATED_ACCOUNT,
  MAX_ORDER_QUANTITY, MAX_ORDER_VALUE, KILL_SWITCH_NOT_ENGAGED,
  NOT_SHUTDOWN, IDEMPOTENCY_KEY_PRESENT, ORDERS_TODAY_UNDER_CAP — all PASS

LiveCanaryGuard.authorize(): called for REAL, for the first and only time,
  at the moment of actual submission (deliberately never pre-invoked
  during preflight, since it has stateful side effects -- duplicate-key
  tracking and a daily order counter -- that must only be spent once)

Central kill switch: DISENGAGED throughout (checked before the final gate,
  immediately before submission, and after)
```

## Idempotency

```
Idempotency key: phase15d2-canary-NIFTY22SEP2623600CE-2026-09-22-20260917T105247
Verified novel at the final gate (get() -> None) and again immediately
before submission (get() -> None).
claim() deliberately NOT called during preflight (would have created a
self-inflicted STATUS_PENDING record blocking the real submission had
authorization been delayed or declined) -- claim() happened atomically
inside the real execute() call, exactly as designed.

Final idempotency record status: PENDING (see "Software observation" below
for why this is PENDING rather than REJECTED, and why that is still safe).
```

## Human authorization

```
Human authorization: EXPLICITLY GRANTED
Exact text: "I authorize the exact Phase 15D.2 live canary order: BUY 65
  units of NIFTY22SEP2623600CE, MARKET, INTRADAY, in Account
  ANGEL_ACCOUNT_B."
This matched the exact order presented at the final gate with zero
parameter drift (no re-validation loop was needed).
```

## Real submission

```
Account:      ANGEL_ACCOUNT_B
Broker:       Angel One
Symbol:       NIFTY22SEP2623600CE
Side:         BUY
Quantity:     65
Order type:   MARKET
Product:      INTRADAY
Submitted:    2026-09-17T11:00:31+05:30 (exactly once — max_retries=1,
              i.e. structurally no retry loop, honoring the "no retry"
              rule even for a class of outcome Phase 15D-DR's own
              analysis treats as normally safe to retry)
```

### Broker response

```
success:          False
status:           REJECTED
order_id:         "" (none issued)
broker error:     "122.171.23.1 is not a registered IP, please check
                   your registered IP." (errorCode AG7002)
```

**Root cause: an Angel One account-level security restriction — the API
key requires a registered/whitelisted IP address for order-placement
calls, and the outbound IP this session used is not registered in Angel
One's developer console.** This is an external, broker-side configuration
matter, not a defect in this project's safety logic, and not something
resolvable from this environment — only the account holder can register
an IP in Angel One's own portal.

## Immediate reconciliation (read-only, mandatory regardless of outcome)

```
Reconciliation ID: RECON-1dad23c4d1228f872695
RECONCILIATION_STARTED:   persisted (durable audit trail)
Broker query:             get_order_book() on Account B, read-only
Result:                   RECONCILED_NOT_FOUND — the order genuinely does
                          not exist anywhere in Account B's live order
                          history (independent confirmation the broker
                          never created anything, consistent with the
                          clean REJECTED response above)
RECONCILIATION_COMPLETED: persisted (durable audit trail)
```

## Post-canary safety verification

```
Account A (ANGEL_SAMIR):        untouched this entire phase; remains READ_ONLY
Account B authorization_state:  CANARY_READY (unchanged — execute() never
                                 mutates this field)
Central kill switch:            DISENGAGED (unchanged)
Open positions (B), post:       0
Existing orders (B), post:      0
Orders matching this symbol:    0 (no duplicate — none exists at all)
Durable audit events for this idempotency_key:
  ORDER_INTENT_CREATED, RECONCILIATION_STARTED, RECONCILIATION_COMPLETED
No automatic exit, hedge, average, or second order was attempted or will be.
```

## Software observation (found, documented, NOT fixed this phase)

`StrategyExecutionEngine.execute()`'s `order_result is None` branch
(`trading/common/execution.py`) calls `self._fail()`, which never persists
an idempotency outcome — its own comment says this is safe because
"every retry attempt failed BEFORE ever reaching the broker's
order-placement call itself." In this real attempt, that assumption did
not hold exactly: the broker WAS reached and gave a clean, definitive,
non-ambiguous `REJECTED`-equivalent outcome (`self._smart_api.placeOrder()`
returned a falsy order id after logging the IP-restriction error
internally, rather than raising a catchable exception — so
`AngelOneBroker.place_order()` returned a plain `OrderResult(status=
"REJECTED", ...)`, which `_call()` turned into a `RuntimeError` that
`_retry()`'s generic exception handler consumed before exhausting the
single configured attempt). The net effect: the idempotency record for
this key is left at `STATUS_PENDING` (from `claim()`) rather than being
updated to `STATUS_REJECTED`.

**This is still safe** — a `PENDING` record is treated identically to an
unresolved state by `_check_idempotency_replay()`, so any future reuse of
this exact key would correctly require manual reconciliation before
proceeding (which this phase already performed, independently confirming
`RECONCILED_NOT_FOUND`) — but it is a real completeness gap worth a future
fix: a confirmed, definitive broker-side rejection reached via the
retries-exhausted path should be persisted as `STATUS_REJECTED`, not left
indistinguishable from a genuinely unknown outcome. Not fixed here,
per this phase's own scope (validate and execute one canary, not modify
execution logic mid-attempt).

## Regression

No code was changed this phase. The safety-relevant test suites (Phase
14.6, 15C.4, 15C.5, 15D-DR, 15D-AUDIT, 15D-RECON — 157 tests) were re-run
as a sanity check before the final gate and passed cleanly (157 passed, 0
failed). Full-suite baseline unchanged: 1414 passed / 0 failed / 0 skipped.

## Broker-call counts

```
Real broker authentication calls: 2 (Account B connect, preflight + submission)
Real broker read-only calls: many (quotes, funds, positions, order book —
  all read-only, across preflight/gate/reconciliation)
Real broker mutation calls: 1 (the single placeOrder() attempt)
Real orders created: 0 (rejected before any order existed)
Strategies started: 0
```

## FINAL STATUS

**PHASE 15D.2 FINAL STATUS: CANARY REJECTED**

The canary was correctly, fully, and safely attempted end-to-end — fresh
validation, human-supplied risk limits, an honest instrument substitution
reported rather than hidden, explicit human authorization for the exact
order, exactly one submission attempt with no retry, immediate read-only
reconciliation, and a clean audit trail. The broker rejected the order for
an external, account-configuration reason (unregistered IP) unrelated to
any safety mechanism in this system. Zero real orders exist. Zero
duplicate risk. Zero automatic follow-up action was taken.

## What must happen before this can be re-attempted

1. The account holder must register the current outbound IP address with
   Angel One (via their developer/API console) for order-placement calls
   to succeed at all.
2. Re-run the complete fresh preflight (Phase 15D.2's own steps) again
   when ready — do not reuse this attempt's LTP/spot/funds/idempotency
   key; a new idempotency key will naturally be generated for a new
   attempt.
3. A fresh, separate, explicit human authorization is required again for
   whatever exact order is presented at that time — this attempt's
   authorization does not carry forward.

**HARD STOP.** Phase 15D.3 was not started. No second order was attempted.
No strategy was started. Waiting for explicit human instruction.

---

# ATTEMPT 3 — 2026-09-17: EXECUTED — First Real Live Order

Root cause of Attempt 2's rejection (`AG7002`, unregistered IP) was
external configuration, not a defect in this system. Two independent
verification phases (Phase 15D.2-IP, run twice) confirmed the runtime's
actual public outbound IPv4 (`122.171.23.1`, cross-checked via four
independent external services: api.ipify.org, ifconfig.me, icanhazip.com,
checkip.amazonaws.com) matched what the user then registered in Angel
One's developer console. A completely fresh Phase 15D.2 preflight was run
from scratch per the hard-stop rule that the prior authorization does not
carry forward — new market check, new instrument resolution, new LTP,
new funds check, new idempotency key. The user gave a new, explicit,
unambiguous authorization for the resulting exact order.

## Fresh preflight (this attempt)

```
Timestamp:              2026-09-17T11:33:37+05:30
Market status:           OPEN
Account B identity:      client_id AA***01, name "SAMIR KUILA"
Account A isolation:     PASS — untouched, no session, no credential resolution
Open positions/orders (pre): 0 / 0
```

### Instrument (dynamically re-resolved — NOT assumed from the prior attempt)

```
Symbol:   NIFTY22SEP2623550CE   (previous attempt: NIFTY22SEP2623600CE)
Strike:   23550
Expiry:   2026-09-22 (confirmed tradable)
Lot size: 65
```

Substitution reason (reported, not hidden): NIFTY spot had moved to
~23250 by this check, so `23600CE` was now further OTM and cheaper than
the preferred ₹25–35 band, while `23550CE` landed inside it and remained
within `CANARY_MAX_ORDER_VALUE`. This was a genuine, explained instrument
change driven by real market movement between attempts, not a forced
substitution.

### Fresh market data

```
Fresh spot (preflight gate):        23251.15    LTP: 27.55   value: ₹1,790.75
Fresh spot (submission moment):     23250.65    LTP: 27.60   value: ₹1,794.00
```

### Safety pipeline (all fresh, all PASS)

```
RiskManager.validate(): ALLOWED — all 15 checks PASS
CanaryLimits structural checks: DEDICATED_ACCOUNT, MAX_ORDER_QUANTITY,
  MAX_ORDER_VALUE, ORDERS_TODAY_UNDER_CAP — all PASS
LiveCanaryGuard.authorize(): called for real, first and only time, at
  submission moment
Central kill switch: DISENGAGED throughout
Idempotency key: phase15d2-canary-NIFTY22SEP2623550CE-2026-09-22-20260917T113337
  — new key, never reused from Attempt 2; confirmed novel at both the
  preflight gate and immediately before submission
```

## Human authorization

```
Exact text: "I authorize the exact Phase 15D.2 live canary order: BUY 65
  units of NIFTY22SEP2623550CE, MARKET, INTRADAY, in Account
  ANGEL_ACCOUNT_B, estimated value ₹1,790.75."
Matched the freshly presented order exactly — no parameter drift.
```

## Real submission and result

```
Submitted:    2026-09-17T11:36:50+05:30 (exactly once — max_retries=1,
              allow_market_emergency=True so the order was genuinely
              submitted as MARKET, not silently downgraded to LIMIT)

Broker response:
  success:          True
  status:           OPEN (Angel's placeOrder() only confirms acceptance,
                     not a fill — the engine's own honest status)
  order_id:         260917000350205
```

## Immediate reconciliation (read-only, mandatory)

```
Reconciliation ID: RECON-b517d0f007ce9fe841ce
RECONCILIATION_STARTED:   persisted
Broker query:             get_order() by broker_order_id, read-only
Result:                   RECONCILED_FILLED
  broker_status:    COMPLETE
  filled_quantity:  65 / 65
  average_price:    (not exposed by OrderState today — see position data below)
RECONCILIATION_COMPLETED: persisted
```

## Post-canary state (verified, read-only)

```
Position:          NIFTY22SEP2623550CE, quantity=65, average_price=27.40
Available cash:    1202.2138 (down from 3005.44 — consistent with the fill)
Orders today:      1 (order_id=260917000350205, status=COMPLETE,
                   filled=65, remaining=0)
Duplicate check:   1 matching order (no duplicates)
Idempotency:       COMPLETED (correctly finalized this time — unlike
                   Attempt 2's PENDING-persistence gap, which only
                   affected the retries-exhausted path, not this success
                   path)
Durable audit trail for this idempotency_key (5 events, in order):
  ORDER_INTENT_CREATED, EXECUTION_RESULT, BROKER_ORDER_PLACED,
  RECONCILIATION_STARTED, RECONCILIATION_COMPLETED
```

## Post-canary safety verification

```
Account A (ANGEL_SAMIR):       untouched this entire phase; remains READ_ONLY
Account B authorization_state: CANARY_READY (unchanged — execute() never
                                mutates this field)
Central kill switch:           DISENGAGED (unchanged)
No automatic exit, hedge, average, or second order was attempted or will be.
```

## Broker-call counts

```
Real broker authentication calls: 2 (preflight read-only connect + live submission connect)
Real broker read-only calls: many (quotes, funds, positions, order book,
  across preflight, freshness re-check, and reconciliation)
Real broker mutation calls: 1 (the single placeOrder() call)
Real orders created: 1
Strategies started: 0
```

## FINAL STATUS

**PHASE 15D.2 FINAL STATUS: CANARY EXECUTED**

The first real-money live canary order for this project was submitted,
accepted, and filled: 65 units of `NIFTY22SEP2623550CE` at an average
price of ₹27.40 in Account `ANGEL_ACCOUNT_B`. Exactly one order was
placed, with no retry. Immediate, independent, read-only reconciliation
confirmed the fill against the broker's own order and position data. The
full lifecycle is durably recorded in the audit trail. Account A was
never touched and remains `READ_ONLY`. No automatic follow-up action
(exit, hedge, average, second order, strategy start) was taken or will be
taken without a new, separate, explicit human decision.

**HARD STOP.** Phase 15D.3 was not started. No second order was
attempted. No strategy was started. The open position
(`NIFTY22SEP2623550CE`, qty=65, avg=27.40) remains open and untouched,
awaiting explicit human instruction on what to do next (hold, or a
separately authorized close).
