# Phase 15D.11 — Human Live-Canary Authorization Review

**Status: READY FOR HUMAN CANARY REVIEW**
**This is a review/preparation document only. No broker order was placed, no LiveAuthorization was created or consumed, no strategy was started, and no production code/configuration was modified to produce it.**

---

## 1. Production version

```
Deployed commit  = ff0d0f87a66a86beca19ff84b9d1e39c8395ef98
Image digest     = sha256:8f4b34709d2c083c4eeb6d3dc647f9dc564c2ce2451794dd79ff2607106c9056
Host             = i-0f344752a1ca2811b (algo-backend, ap-south-1)
Container status = running, healthy
Documentation follow-up commit = 67bafd623b72fe210d012c7b1821526a951d22bd
```

Re-verified fresh this phase (not assumed from Phase 15D.10-R's own timestamp) — commit and image both confirmed unchanged, container still `running`/`healthy`.

## 2. Current safety state (re-verified fresh this phase)

| Check | Result |
|---|---|
| `/api/health` | `200`, `{"status":"ok","database":"connected","environment":"production","clock_drift":"not_checked", ...}` |
| `/api/ready` | `200`, `{"application":"ready","broker":"not_configured","trading_authorized":false}` |
| Kill-switch persisted state | No file at `/app/data/kill_switch.json` — never engaged/disengaged since deployment; the switch is in its safe, disengaged default. (The file is written only by `engage()`/`disengage()`, neither of which this phase performed.) |
| Audit store | Available, hash chain `verify()=True`, 5 records, all `DEPLOYMENT_STARTUP`/`DEPLOYMENT_SHUTDOWN` — no trading/authorization event exists |
| Idempotency store | Code available (`SqliteIdempotencyStore`, `0600`-hardened) — **not currently wired into the production API's `ExecutionState`** (named limitation, unchanged since Phase 15D.10-R; a canary would use its own dedicated store, constructed by whatever script runs it, exactly as the historical canary did) |
| Reconciliation store | Same as idempotency — code available, not wired into the live API process; used by whatever script performs the canary |
| Legacy strategy processes | `DoubleStraddelAlgo`/`CombinedVwapNifty`/`Vwap_Algo_Nifty_hedge`: zero matching systemd units, containers, or processes on the production host |

## 3. Account A state

Real, read-only, credentialed verification (`AngelOneBroker(read_only=True)`; only `connect()`/`get_funds()`/`get_positions()`/`get_order_book()`/`get_open_orders()` called):

```
Authentication: PASS
Available cash: ₹100.00
Used margin:    ₹0.00
Positions:      0
Order book:     0
Open orders:    0
```

**Account A = READ_ONLY** (minimal-funds, zero-activity profile, unchanged across every check this engagement has performed).

## 4. Account B state

```
Authentication: PASS
Available cash: ₹2,949.6525
Used margin:    ₹101.5475
Positions:      2 returned, BOTH quantity=0 (NIFTY22SEP2622350PE, NIFTY22SEP2624350CE -- neither is the historical canary strike, which no longer appears at all)
Order book:     4 entries, ALL status=COMPLETE, remaining=0
Open orders:    endpoint rate-limited this specific call (BrokerRateLimitError) -- not retried a second time this phase, per the standing "do not hammer the API" rule
```

**Account B = READ_ONLY / FLAT.** The rate-limited "Open Orders" endpoint is the only signal not directly re-confirmed *this* call; it was independently, conclusively confirmed (`count=0`) in the immediately preceding Phase 15D.10-R deployment verification, alongside the same zero-quantity positions and all-complete order book seen again here. No order, position, or exposure has changed between that check and this one.

## 5. Broker read-only verification

Zero mutation calls were made this phase — confirmed both by the tool's own design (`is_read_only` asserted `True` before and after every call) and by Account A/B's own state being completely unchanged from every prior check.

## 6. Current risk limits (as actually configured, not invented)

Read directly from `trading/.env` and `trading/preflight/live_canary.py`'s own env-var names — the same mechanism used for the original, already-completed successful canary:

```
CANARY_ACCOUNT_ID          = ANGEL_ACCOUNT_B
CANARY_MAX_ORDER_QUANTITY  = 65
CANARY_MAX_ORDER_VALUE     = 2000
CANARY_MAX_DAILY_LOSS      = 2000
CANARY_MAX_STRATEGY_LOSS   = 2000
CANARY_MAX_ORDERS_PER_DAY  = 1
```

- **Allowed broker/account**: Angel One, `ANGEL_ACCOUNT_B` only — `LiveCanaryGuard`'s `DEDICATED_ACCOUNT` check rejects any other account outright (`CanaryLimits` has no default account and cannot be constructed without one).
- **Allowed instrument/type restrictions**: none beyond the value/quantity caps above — any instrument is structurally permitted as long as the resulting order respects the quantity/value/loss/count limits. `LiveCanaryGuard` does not itself restrict to options vs. equity vs. index.
- **Authorization expiration**: `LiveAuthorization`'s default TTL is 600 seconds (10 minutes) from creation (`DEFAULT_TTL_SECONDS` in `trading/common/live_authorization.py`) unless a caller overrides it — durable, checked on every read, not merely computed once.
- **Single-use**: yes, structurally — `try_consume()`'s atomic `UPDATE ... WHERE status='AUTHORIZED'` compare-and-swap guarantees exactly one successful consumption ever, verified by dedicated tests in Phase 15D.5/15D.6/15D.7.
- **Kill-switch behavior**: checked unconditionally, first, before every other gate (`execution.py` step 0) — engaging it blocks execution regardless of any valid authorization.
- **Idempotency behavior**: an atomic `claim()` immediately before the broker call (step 5, inside the try_consume block) — a replayed intent with the same key never reaches the broker twice.
- **Reconciliation behavior**: order-level only (no position-level corroboration — a standing, named, non-blocking limitation since Phase 15D.4), run as a separate, subsequent step after execution, not inline in the execution path itself.

No limit is missing, unlimited, ambiguous, or fail-open. **No blocker found in this step.**

## 7. Canary proposal

```
CANARY PROPOSAL

Account:                     ANGEL_ACCOUNT_B  (fixed by CANARY_ACCOUNT_ID configuration -- the
                              only account LiveCanaryGuard's DEDICATED_ACCOUNT check permits;
                              this is a structural constraint, not a choice made in this report)
Broker:                      Angel One (angelone)
Instrument:                  NOT PROVIDED -- HUMAN INPUT REQUIRED
Exchange:                    NOT PROVIDED -- HUMAN INPUT REQUIRED
Expiry:                      NOT PROVIDED -- HUMAN INPUT REQUIRED
Strike:                      NOT PROVIDED -- HUMAN INPUT REQUIRED
Option Type:                 NOT PROVIDED -- HUMAN INPUT REQUIRED
Side:                        NOT PROVIDED -- HUMAN INPUT REQUIRED
Quantity:                    NOT PROVIDED -- HUMAN INPUT REQUIRED (must be a multiple of the
                              instrument's lot size, and must not exceed 65)
Order Type:                  NOT PROVIDED -- HUMAN INPUT REQUIRED
Limit Price / Maximum Price: NOT PROVIDED -- HUMAN INPUT REQUIRED

Estimated Order Value:       Cannot be computed without Instrument + Quantity + Price above.
                              Must not exceed ₹2,000 (CANARY_MAX_ORDER_VALUE).

Purpose:                     NOT PROVIDED -- HUMAN INPUT REQUIRED (e.g. "second controlled
                              live-money verification of the deployed 15D.5-15D.9 authorization
                              chain in production," or whatever the actual intent is)
Expected position after execution: Cannot be determined without the above.

Risk Limits:
  Maximum Loss:               bounded by CANARY_MAX_DAILY_LOSS / CANARY_MAX_STRATEGY_LOSS = ₹2,000 each
  Maximum Order Value:        ₹2,000 (CANARY_MAX_ORDER_VALUE)
  Maximum Quantity:           65 units (CANARY_MAX_ORDER_QUANTITY)

Authorization Duration:      600 seconds (LiveAuthorization DEFAULT_TTL_SECONDS) unless a
                              shorter/longer TTL is explicitly requested at request-time
Single-use:                  YES (structurally enforced, atomic compare-and-swap)

Account A Protection:        READ_ONLY (confirmed, Section 3)
Strategies:                  STOPPED (confirmed, Section 2)
Kill Switch:                 ENABLED / disengaged-default, operational (confirmed, Section 2)

Required Human Approval:     EXPLICIT
```

## 8. Missing human inputs

Every order-specific field (instrument, exchange, expiry, strike, option
type, side, quantity, order type, limit/max price, and the purpose of
this specific canary) is **NOT PROVIDED**. None of these were guessed,
inferred, or defaulted in this report. A specific, complete order
specification from you is required before any `AuthorizationRequest`
could even be constructed — `validate_request()` and `CanaryLimits`
both fail closed on missing/empty required fields by design (confirmed
in Phase 15D.6/15D.7's own test suites), so this is not merely a
documentation gap — the system itself cannot proceed without this input
either.

## 9. Dry-run / preflight result

`trading/preflight/live_canary.py::run_preflight()` — the same existing,
already-proven, non-mutating dry-run mechanism used before the original
successful canary — was re-run fresh this phase (locally, with real
Account B credentials so every check could actually evaluate, not skip):

```
CONFIGURATION:                     PASS
DEDICATED_ACCOUNT:                 PASS
BROKER ADAPTER VALIDATED:          PASS  (validated against a real account, Phase 5A)
REAL ACCOUNT VALIDATION:           PASS  (trading.validation.angel_readonly re-run live)
RISK MANAGER:                      PASS  (synthetic dry-run intent only, never sent anywhere)
CANARY GUARD DRY RUN:              PASS  (synthetic dry-run intent only, never sent anywhere)
KILL SWITCH:                       PASS  (engage/disengage exercised on a throwaway local
                                          instance, never the production kill switch)
EMERGENCY SHUTDOWN:                PASS
OBSERVABILITY WIRED:               PASS  (hash chain verified on a throwaway local trail)
CONTROL CENTER BACKEND IMPORTABLE: PASS
BROKER ADAPTER IS SOLE ORDER CALLER: PASS
NO AUTOMATIC ORDER PLACEMENT:      PASS

PHASE 14 = PASS
```

This tool never imports a broker adapter capable of placing an order in
its own process, never constructs a real `OrderIntent` that reaches
`StrategyExecutionEngine`, and its own final line states plainly: *"This
tool never enables live trading on its own -- passing preflight is a
readiness statement, not an action."* Zero mutation calls occurred.

## 10. Execution-gate verification (traced from code, not assumed)

`StrategyExecutionEngine.execute()` (`trading/common/execution.py`),
confirmed by direct grep of its own step markers this phase:

```
step 0 (line 640): CentralKillSwitch      -- unconditional, first, every mode
step 1 (line 649): Idempotency replay      -- authoritative, persistent
step 2 (line 666): TradingAccount + AuthorizationState gate
step 3 (line 732): RiskManager.validate()  -- always all 14-15 checks
step 4 (line 755): Mode-specific gate      -- LiveCanaryGuard for LIVE_CANARY
step 5 (line 793): Broker resolution
                     -> LiveAuthorization.try_consume() (exact-scope + operator_id match)
                     -> Idempotency claim() (atomic, immediately pre-broker-call)
                     -> BrokerClient.place_order()
step 6 (line 891): Broker response validation -- unconditional, every mode
step 7 (line 899): Persist the definitive idempotency outcome
```

Specific properties re-confirmed:

- **Authorization is single-use**: `try_consume()`'s atomic CAS (`WHERE status='AUTHORIZED'`) — a second attempt against the same authorization always fails, proven by dedicated tests in Phase 15D.5/15D.6/15D.7.
- **Authorization expires**: `is_expired()`, durably transitioned to `EXPIRED` on `get()`, not merely computed.
- **Failed validation does not consume authorization incorrectly**: `LiveAuthorizationWorkflow.confirm()` revokes (never leaves PENDING/AUTHORIZED dangling) on a failed preflight, a denied operator, or a declined/invalid human confirmation — it never reaches `try_consume()` in any of those cases.
- **Broker rejection does not trigger automatic retry**: a clean, confirmed rejection persists a terminal `STATUS_REJECTED` outcome (Phase 15D.3-R); an ambiguous one persists `STATUS_AMBIGUOUS` and requires manual reconciliation — neither is silently retried as a fresh attempt.
- **Successful execution is not mistaken for observability failure**: `ObservabilityHealth`/`_obs()` wrapping (Phase 14.6 Blocker C) ensures a metrics/audit/alert failure is recorded separately and never overwrites or masks a genuine broker success.
- **Duplicate execution is prevented**: the idempotency `claim()` (step 5) is atomic and immediately pre-broker-call; a second concurrent or replayed attempt with the same key is rejected before any broker call.
- **Reconciliation occurs after execution**: `ReconciliationService` is a separate, subsequent step (not inline in `execute()`) — this is by design, matching Phase 15D.4's own documented architecture, and remains order-level only (position-level corroboration is not implemented, a standing, non-blocking, named limitation).
- **Kill switch remains effective**: checked first (step 0), before account resolution, before risk, before authorization — nothing in the chain can be reached if it is engaged.

No modification was made to any of this code to produce this report.

## 11. Identified risks / limitations (named, not silently resolved)

- Idempotency, reconciliation, and LiveAuthorization stores remain
  unwired into the live API process (`execution_state.py`) — any
  canary continues to require a dedicated script (exactly like the
  original historical canary and `trading/preflight/live_canary.py`),
  not an HTTP endpoint on the deployed backend.
- Reconciliation is order-level only; no position-based corroboration.
- `git_sha` in `/api/health` still reports `"unknown"` (cosmetic,
  `.dockerignore` interaction, named in Phase 15D.10-R, not a safety
  gap).
- `KILL_TRADING` permission remains unwired to `CentralKillSwitch.engage()`
  (Phase 15D.7's own deliberate, unchanged decision — the switch itself
  is not permission-gated, by design, so it stays available in an
  emergency regardless of who is holding what role).
- The kill-switch persistence file does not exist on the production
  host yet (Section 2) — this is the correct, safe, untouched default,
  not a defect, but it also means the very FIRST real `engage()`/
  `disengage()` call on production will be the first time that specific
  file-write path is exercised for real, outside of local tests. Worth
  a human's awareness, not a blocker.
- No order-specific parameters have been supplied (Section 8) — this
  alone is sufficient to prevent any authorization request from being
  constructed today, independent of every other check passing.

None of these block a **technical readiness** verdict — they are the
same, already-named, accepted limitations carried since Phase 15D.8/
15D.9/15D.10-R, none of which regressed.

## 12. Exact next human action

This report ends here, deliberately, before any authorization step.
The next action is exclusively yours:

1. Supply the missing order specification (Section 8) — instrument,
   exchange, expiry, strike, option type, side, quantity, order type,
   and limit/maximum price — for the specific canary you want to run.
2. Separately, explicitly state that you are authorizing that exact,
   fully-specified action (not this report, and not "proceed" in the
   abstract) — matching this engagement's own established precedent
   from the original canary's authorization
   ("*I authorize the exact ... order: BUY 65 units of
   NIFTY22SEP2623550CE, MARKET, INTRADAY, in Account ANGEL_ACCOUNT_B,
   estimated value ₹1,790.75.*").
3. Only after that explicit authorization would a future phase
   construct the `AuthorizationRequest`, run `validate_request()`/
   `run_preflight()`, obtain your confirmation through
   `LiveAuthorizationWorkflow.confirm()`, and — only then — call
   `StrategyExecutionEngine.execute()` against the real, deployed
   production system.

---

## Final gate

```
PHASE 15D.11 = READY FOR HUMAN CANARY REVIEW

NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
NO REAL BROKER MUTATION PERFORMED.
STRATEGIES REMAIN STOPPED.
HARD STOP.
```
