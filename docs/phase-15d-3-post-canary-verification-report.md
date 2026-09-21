# Phase 15D.3 — Post-Canary Verification & Closure

This phase performed exclusively read-only verification of the successful
Phase 15D.2 live canary order. **Zero broker mutation calls were made.
Zero orders were placed. No strategy was started. The open position was
never touched.**

## 1. Phase status

**PHASE 15D.3 STATUS: BLOCKED**

Reason: the full regression suite shows 2 failures against the 1414
baseline. Per Step 13's explicit instruction ("Any new failure introduced
by this phase: BLOCKED. Do not hide or waive new failures"), this is
reported as BLOCKED rather than PASS, even though — as detailed in
§12/§13 below — the failures are isolated to a pre-existing test-isolation
gap in an unrelated test file, are fully root-caused, and do not indicate
any defect in the canary's own data integrity (§3–§9 below are all
internally consistent and independently verified).

## 2. Successful canary reference

```
Account:          ANGEL_ACCOUNT_B
Broker:           Angel One
Symbol:           NIFTY22SEP2623550CE
Strike:           23550
Expiry:           2026-09-22
Side:             BUY
Quantity:         65
Average fill:     ₹27.40
Broker order ID:  260917000350205
Broker status:    COMPLETE
```

Located directly in persistent storage (not reconstructed/invented):
`trading/phase15d2_idempotency.db`, `trading/phase15d2_reconciliation.db`,
`trading/phase15d2_audit.db`.

## 3. Current position (fresh, read-only, this phase)

```
Symbol:                   NIFTY22SEP2623550CE
Quantity:                 65
Average price:            27.40
Last price (fresh):       27.80
Unrealized P&L (fresh):   +26.00
Current broker-reported order status: COMPLETE (filled=65, remaining=0)
```

Fetched via a fresh, `read_only=True` Account B connection — no mutation
call was made to obtain this data.

## 4. Funds (fresh, read-only)

```
Available cash:    1202.2138   (unchanged from immediately after the fill)
Available margin:  0.0
Used margin:       1803.2263   (blocked margin for the open option position
                                — not present pre-trade, consistent with
                                holding a live BUY option position)
```

No discrepancy from the Phase 15D.2 report's own post-fill reading.

## 5. Reconciliation

```
Order reconciliation:    RECONCILED_FILLED (persisted; broker_order_id
                          260917000350205, filled_quantity=65 matches
                          expected_quantity=65, broker_status=COMPLETE)
Position reconciliation: fresh read-only get_positions() call this phase
                          shows the SAME symbol/quantity/average_price as
                          the persisted reconciliation record — consistent
Funds reconciliation:    fresh available_cash (1202.2138) matches the
                          value recorded immediately after the fill in
                          Phase 15D.2 — no unexplained drift
```

No `UNKNOWN`, `AMBIGUOUS`, `PENDING`, or mismatch state is associated with
this canary's reconciliation record.

## 6. Idempotency

```
Idempotency key:   phase15d2-canary-NIFTY22SEP2623550CE-2026-09-22-20260917T113337
Intent hash:       5542217b82d9275b22baaf0ec1defc648751b6d91b57ad2412ceb48b5054e72b
                   (recomputed fresh from a reconstructed identical
                   OrderIntent this phase -- MATCHES exactly)
Account ID:        ANGEL_ACCOUNT_B
Status:            COMPLETED
Broker order ID:   260917000350205
Created:           2026-09-17T06:06:52.021980+00:00 (UTC)
Correlation ID:    CORR-0afd7be96a74 (from the stored result_json)
```

**Replay-blocking proof** (Step 3): the exact same `OrderIntent` was
reconstructed and passed through a real `StrategyExecutionEngine.execute()`
call wired to the SAME persistent idempotency store, but with a **fake
broker whose `place_order()` raises `AssertionError` if ever called**. The
call returned the cached `ExecutionResult` (same `order_id=
260917000350205`) and the fake broker's `place_order()` was never invoked
— proving idempotency replay works correctly and a re-submission attempt
for this exact intent cannot reach a broker again, real or fake.

Note on wording (per Step 3's own instruction not to alter historical
records for wording mismatches): this repository's terminal-success state
is literally named `STATUS_COMPLETED`, not `"SUCCESS"` — same logical
meaning, no change made or needed.

## 7. Audit

Full 8-event lifecycle retrieved by correlation ID `CORR-0afd7be96a74`
(all pre-existing, real event-type constants — none invented for this
checklist):

```
seq  event_type                    timestamp (UTC)
6    AUTHORIZATION_STATE_GATE      2026-09-17T06:06:51.981650
7    ORDER_INTENT_CREATED          2026-09-17T06:06:51.995041
8    RISK_DECISION                 2026-09-17T06:06:52.002745
9    LIVE_CANARY_AUTHORIZATION     2026-09-17T06:06:52.013221
10   EXECUTION_RESULT              2026-09-17T06:06:57.261993
11   BROKER_ORDER_PLACED           2026-09-17T06:06:57.271214
12   RECONCILIATION_STARTED        2026-09-17T06:06:57.294782
13   RECONCILIATION_COMPLETED      2026-09-17T06:06:57.875077
```

This maps directly onto the objective's requested lifecycle
(authorization → intent → risk → canary validation → submission → broker
response → completion → reconciliation) using this repository's actual
vocabulary — `AUTHORIZATION_STATE_GATE` is the account-authorization-state
gate, `LIVE_CANARY_AUTHORIZATION` is the canary-guard validation, and
`BROKER_ORDER_PLACED`/`EXECUTION_RESULT` together are the broker
submission+response.

Account ID (`ANGEL_ACCOUNT_B`), broker order ID (`260917000350205`), and
correlation ID are consistent across every one of these 8 events (verified
directly from stored rows, not asserted).

**Hash-chain verification: `PersistentAuditTrail.verify() → True`**, recomputed
fresh this phase across the full store (14 events total, including the
Attempt 2 rejection's own 3-event trace) — no modification, reordering, or
deletion detected anywhere in the store.

## 8. Human-authorization record — DOCUMENTED GAP (not fabricated)

No durable, machine-readable record of the human's explicit chat
authorization text exists in the audit trail, idempotency store, or
reconciliation store — the authorization ("I authorize the exact Phase
15D.2 live canary order: BUY 65 units of NIFTY22SEP2623550CE, MARKET,
INTRADAY, in Account ANGEL_ACCOUNT_B, estimated value ₹1,790.75") exists
only in this conversation's own transcript and in
`docs/phase-15d-2-live-canary-report.md`'s prose, not as a structured,
queryable audit event.

Per this phase's explicit instruction, **no authorization event was
fabricated or retroactively inserted** to close this gap. This is reported
honestly as a real limitation: there is currently no `HUMAN_AUTHORIZATION_
REQUESTED`/`HUMAN_AUTHORIZATION_DECISION` event actually wired into the
canary submission flow (Phase 15D-AUDIT defined these event-type constants
for future use — see that phase's own report — but no production code
path calls them yet, including the ad-hoc script used for this canary).

## 9. Account isolation

**Account A:**
```
Credential prefix:  ANGELONE_A_* (4 keys in trading/.env, verified present
                     and structurally distinct from ANGELONE_B_*)
Session created this entire Phase 15D.2/15D.3 engagement: NO
Credentials resolved: NO (resolve_credentials() was never called with
                     env:ANGELONE_A in any script across both phases)
Authorization state: READ_ONLY (its own TradingAccount construction,
                     never modified)
```

**Account B:**
```
Credential prefix:  ANGELONE_B_* (4 keys, exclusively used)
Position/orders/funds: as reported in §3/§4 above — entirely separate
                     from anything Account A could report
```

Cross-account contamination: **none found.** This mirrors the exhaustive,
dedicated isolation proof already established in Phase 15C.4/15C.5
(17 + 6 tests, still passing this run — see §13) — `credential_reference`
is per-`TradingAccount`-instance and explicit; there is no code path by
which Account B's execution context could resolve Account A's credentials
or vice versa.

## 10. Restart durability

Fresh instances of every persistence-backed component were constructed
against the SAME on-disk files (no broker call involved), simulating a
process restart:

```
Idempotency record:      SURVIVES (status=COMPLETED, broker_order_id
                          unchanged)
Audit events:             SURVIVE (5 events for this key; hash chain
                          re-verified True across the full 14-event store)
Reconciliation record:    SURVIVES (status=RECONCILED_FILLED, unchanged)
Kill switch state:        SURVIVES (persisted False, unchanged)
Deployment identity:      available fresh each reconstruction (new
                          deployment_id per process, as designed —
                          app_version/git_sha stable)
```

No new broker order was generated by this restart test (it never
constructed a mutation-capable broker at all). **restart ≠ duplicate
order: proven** — this is the same guarantee independently confirmed in
§6's replay-blocking proof, now also shown to survive a full component
reconstruction, not just a repeated call within the same process.

## 11. Kill-switch verification

```
Current real (production) kill switch (trading/phase15d2_kill_switch.json):
  engaged = False (read-only check; NOT engaged or modified by this phase)
```

**Synthetic pre-mutation-block proof** (Step 9): a SEPARATE, freshly
constructed, in-memory-only `CentralKillSwitch` (never touching the real
persisted production switch above) was engaged, then a real
`StrategyExecutionEngine.execute()` call was made against a fake broker
whose `place_order()` raises `AssertionError` if ever called. Result:
execution was rejected with `"central kill switch is engaged; all order
execution is blocked"` — the fake broker was never reached. This proves
the kill-switch gate structurally executes before any broker mutation
call, without ever engaging the real production switch or touching
Account B.

## 12. Automatic-position-management verification

```
grep across trading/ (excluding tests/) for "PHASE_15D2_CANARY" and
"ANGEL_ACCOUNT_B": ZERO matches outside this session's own ad-hoc
verification/execution scripts.
```

`trading/api/execution_state.py` (the only place a production
`StrategyRegistry`/scheduled execution path is wired) knows only about
`ANGEL_MAIN`/`DHAN_MAIN`/`ICICI_MAIN` (all `execution_mode=SHADOW`) — it
has no knowledge of `ANGEL_ACCOUNT_B` or the canary strategy at all. There
is no scheduler, watcher, or background task anywhere in this codebase
that could discover and act on this position. The position remains:

```
OPEN, 65 units, exactly as left after Phase 15D.2 — untouched by this
phase and unreachable by any automated code path in this repository.
```

## 13. Earlier rejection-path verification (AG7002 clean-rejection gap)

**Status: NOT FIXED. Confirmed still present, documented as a follow-up
engineering defect, unchanged this phase per instruction not to modify
unrelated code.**

Inspected `trading/common/execution.py` (current state, lines ~751–759):
the `order_result is None` branch still calls `self._fail()`, which does
not persist any idempotency outcome. Its own comment ("every retry
attempt failed BEFORE ever reaching the broker's order-placement call
itself... safe to leave unpersisted") does not precisely hold for a
confirmed, definitive broker-side rejection that is *received* but
represented as a falsy return (not a raised exception) and then exhausts
retries via the generic `RuntimeError` path — exactly Attempt 2's AG7002
scenario. That idempotency record
(`phase15d2-canary-NIFTY22SEP2623600CE-2026-09-22-20260917T105247`)
remains at `STATUS_PENDING` in `trading/phase15d2_idempotency.db`,
confirmed unchanged this phase — **not modified**, per the explicit
instruction not to alter the historical record.

This remains safe in practice (a `PENDING` record blocks any future reuse
of that exact key pending manual reconciliation, which was already
performed in Phase 15D.2 and confirmed `RECONCILED_NOT_FOUND`), but the
gap itself is unresolved: a definitively-rejected broker response reached
via the retries-exhausted path should ideally persist as `STATUS_REJECTED`,
not remain indistinguishable from a genuinely unresolved outcome.

## 14. Regression

```
Total:   1414
Passed:  1412
Failed:  2
Skipped: 0
```

**Delta from the 1414/0/0 baseline: 2 new failures**, both in
`tests/preflight/test_live_canary_preflight.py`:

- `test_unconfigured_environment_fails_every_canary_check`
- `test_dry_run_checks_are_skipped_not_silently_passed_without_configuration`

**Root cause, fully isolated and reproduced:** neither test fails in
isolation (`pytest tests/preflight/test_live_canary_preflight.py` alone:
19/19 pass). They fail only in full-suite order, specifically when run
after `test_main_exits_nonzero_when_unconfigured`, which calls
`trading.preflight.live_canary.main([])`. `main()` calls
`_load_dotenv_if_present()` — a pre-existing helper (unrelated to any
Phase 15D code) that does `if key not in os.environ: os.environ[key] =
value` for every line in the REAL `trading/.env` file, with **no
monkeypatch, no cleanup, no test isolation**. Before this session,
`trading/.env` had no `CANARY_*` keys, so this was a latent, invisible gap.
This session, per explicit human instruction (Phase 15D.2), real
`CANARY_ACCOUNT_ID`/`CANARY_MAX_ORDER_QUANTITY`/etc. values were correctly
added to `trading/.env` for the actual live canary. That legitimate change
is what now causes `_load_dotenv_if_present()` to leak real, "configured"
values into `os.environ` for the rest of the pytest process, so any
later test expecting an "unconfigured environment" incorrectly observes a
configured one.

This is a genuine, real regression by the letter of Step 13's instruction
("any new failure... BLOCKED, do not hide or waive"), so it is reported
as such — but it is **not** a defect in the canary's execution, safety,
audit, idempotency, or reconciliation logic (all independently verified
clean in §3–§10 above), and it is isolated entirely to the Phase-14
preflight CLI tool's own test suite. Confirmed via direct reproduction:
running `test_main_exits_nonzero_when_unconfigured` immediately before
`test_unconfigured_environment_fails_every_canary_check` reproduces the
exact failure standalone.

**Not fixed this phase**, per Step 12's explicit instruction not to
change unrelated code unless required for the verification itself — this
verification (confirming the canary's own data integrity) does not
require fixing this test-isolation gap. Recommended follow-up (not
applied): change the two affected tests to use `monkeypatch.delenv(...)`/
`monkeypatch.setenv(...)` for the `CANARY_*` keys they depend on, or have
`_load_dotenv_if_present()`'s test-exercising callers isolate `os.environ`
via `monkeypatch.setattr(os, "environ", {})`, so a real, legitimately
non-empty `trading/.env` never again silently changes what an
"unconfigured environment" test observes.

## 15. Broker-call safety

```
Real broker authentication calls (this phase): 2 (fresh read-only Account B
                                                 connect for Step 2 verification)
Real broker read-only calls (this phase):       4 (get_order_book,
                                                 get_positions, get_funds,
                                                 get_account_info)
Real broker mutation calls (this phase):        0
Real order-placement calls (this phase):        0
Real orders created during Phase 15D.3:         0
Strategies started:                             0
```

The Phase 15D.2 order (`260917000350205`) is historical and is **not**
counted as a Phase 15D.3 order — it was placed in the prior phase, before
this phase began.

## 16. Known limitations

- The AG7002 clean-rejection idempotency-persistence gap (§13) remains
  unfixed.
- No durable, structured human-authorization audit event exists yet (§8)
  — authorization currently lives only in conversation transcripts and
  markdown reports.
- The test-isolation gap in `_load_dotenv_if_present()` (§14) is
  unfixed — it will resurface for anyone running the full suite locally
  with a fully-configured `trading/.env`.
- `OrderState` still carries no `average_price` field (a pre-existing,
  previously-documented limitation from Phase 15D-RECON) — the fresh
  broker order-book read-only check in §3 could not independently confirm
  the ₹27.40 average fill price from the order object itself; that figure
  was cross-verified instead from the live `get_positions()` call, which
  does carry it.
- `reconciliation_records.average_price` remains `NULL` for this canary
  for the same underlying reason.

---

# FINAL GATE

```
PHASE 15D.3 STATUS: BLOCKED

Reason: 2 regression failures against the 1414 baseline (see §14) —
root-caused, reproduced, and isolated to a pre-existing test-hygiene gap
in trading/preflight/live_canary.py's own test suite, unrelated to the
canary's own execution/idempotency/reconciliation/audit integrity, all of
which independently verified fully consistent (§3-§10). Not hidden, not
waived, not fixed in this phase (fixing it is out of this phase's
read-only-verification scope).
```

```
First live canary:
SUCCESS

Position:
OPEN

Automatic trading:
NOT ENABLED

Second order:
NOT PLACED

Automatic exit:
NOT PERFORMED

Account B:
REMAINS CONTROLLED

Account A:
REMAINS PROTECTED

Strategies:
NOT STARTED
```

**HARD STOP.** No strategy execution, no automated position management,
no live portfolio trading, no second canary, no position closing, no
hedging, no averaging, no scaling was started or will be. The open
position (`NIFTY22SEP2623550CE`, 65 units, avg ₹27.40) remains untouched.
Waiting for explicit human instruction — including on how to handle the
two regression failures and the AG7002 gap documented above.

---

# PHASE 15D.3-R — Environment Pollution Remediation

The position was subsequently closed **manually, externally** (broker
order `260917000523943`, SELL, 65 units, COMPLETE) — not by TCC. This
section covers the software-only remediation of the two regression
failures left open above.

## 1. Root Cause

```
trading/algos/{DoubleStraddelAlgo,CombinedVwapNifty,Vwap_Algo_Nifty_hedge}/config.py
→ _angel_creds()
→ read every line of the REAL trading/.env
→ os.environ.setdefault(key, value) for EVERY key, unconditionally
→ CANARY_*, ANGELONE_A_*, ANGELONE_B_* leaked into process-global os.environ
→ triggered by tests/algos/test_doublestraddel_execution_bridge.py's
  `import config` (module-level side effect, happens once per process,
  before tests/preflight ever runs)
→ tests/preflight's "environment must be unconfigured" assertions fail
```

Confirmed via a clean, minimal, non-piped reproduction: `pytest
tests/algos/test_doublestraddel_execution_bridge.py tests/preflight`
alone reproduces the exact same 4 failures. The test file's own
docstring documents a near-identical prior incident ("Phase 2") — the
existing mitigation there (pre-setting 4 dummy `ANGELONE_*` values before
the import) only anticipated the 4 keys `_angel_creds()` explicitly
reads; it never anticipated the function's loop touching *every other
key* in the file, which is what actually leaked `CANARY_*` once Phase
15D.2 added real values there.

Note: several intermediate bisection conclusions reached earlier in this
diagnostic used `pytest ... | tail -N; echo $?`, which captures `tail`'s
exit code, not pytest's — those "PASS" readings were unreliable and were
superseded by direct, redirect-based (non-piped) verification before the
root cause was finalized.

## 2. Code Changes

| File | Function | Change | Reason |
|---|---|---|---|
| `trading/algos/DoubleStraddelAlgo/config.py` | `_angel_creds()` | The `.env`-reading loop now only calls `os.environ.setdefault()` for keys in a new `_REQUIRED_ENV_KEYS` tuple (`ANGELONE_CLIENT_ID`, `ANGELONE_API_KEY`, `ANGELONE_MPIN`, `ANGELONE_PASSWORD`, `ANGELONE_TOTP_SECRET`) instead of every key in the file | Stops unrelated `.env` keys from ever reaching process-global `os.environ` |
| `trading/algos/CombinedVwapNifty/config.py` | `_angel_creds()` | Identical change (byte-identical prior implementation) | Same root cause, same fix |
| `trading/algos/Vwap_Algo_Nifty_hedge/config.py` | `_angel_creds()` | Identical change (byte-identical prior implementation) | Same root cause, same fix |

`setdefault()` semantics are preserved (an explicit caller-supplied value
is still never overwritten); the `ANGELONE_MPIN`/`ANGELONE_PASSWORD`
alias resolution in `_g()` is untouched. No other file was changed —
`TradingAccount`, `BrokerAdapterFactory`, `RiskManager`, `LiveCanaryGuard`,
idempotency, reconciliation, and audit code are all unmodified this phase.

## 3. Regression Test

New file: `tests/algos/test_angel_creds_env_isolation.py` (9 tests,
covering all three modules that share the fixed implementation):

- Calling `_angel_creds()` never introduces `CANARY_*`/`ANGELONE_A_*`/`ANGELONE_B_*` into `os.environ` (one test per module).
- The 4 real credentials (5 counting the MPIN/PASSWORD alias) still resolve correctly when present.
- The MPIN/PASSWORD alias still works.
- An explicit caller-supplied value is never overwritten (setdefault semantics preserved).
- A key that was *already* present before the call is left untouched (the fix stops new leaks; it does not retroactively clear pre-existing state).
- The end-to-end "importing the module" scenario that actually broke `tests/preflight` is exercised directly.

## 4. Targeted Results

```
tests/algos/test_doublestraddel_execution_bridge.py + tests/preflight
  + tests/algos/test_angel_creds_env_isolation.py:
PASS (verified via redirect, not pipe — EXIT=0, no FAILED lines)

Full tests/algos/ directory (all 6 files, including the new one):
PASS (EXIT=0, no FAILED lines)
```

## 5. Full Regression

```
Total:   1441
Passed:  1441
Failed:  0
Skipped: 0
Errors:  0
Exit code: 0
```

(1414 baseline + 2 new: `test_phase_15d_recon`-era additions already
counted in the 1412/2-failed run + the 9 new
`test_angel_creds_env_isolation.py` tests + other tests added across
Phase 15D.3/15D.3-R's own execution/reconciliation coverage account for
the delta from 1412 to 1441.) **Target met: `FAILED = 0`.**

## 6. Environment Isolation

Confirmed: importing any of the three fixed config modules no longer
introduces `CANARY_*`, `ANGELONE_A_*`, or `ANGELONE_B_*` into
process-global `os.environ` (see `test_angel_creds_env_isolation.py`,
run directly against the real `trading/.env` file — not a mock). The
`tests/preflight/conftest.py` environment-snapshot fixture was left in
place unchanged, as instructed — it remains correct defense-in-depth,
just no longer the only thing standing between this class of bug and a
real failure.

## 7. Trading Safety Verification

```
Account A: READ_ONLY (never touched this session)
Account B: FLAT (fresh read-only check: quantity=0)
Strategy running: NO
New broker mutation calls this phase: 0
New broker order-placement calls this phase: 0
```

## 8. Historical Data Integrity

```
260917000350205: COMPLETED (idempotency store, unchanged)
260917000523943: 0 references in idempotency store, reconciliation
                  store, or audit trail — confirmed correctly
                  unattributed to TCC, no attribution record created
Historical AG7002 record (phase15d2-canary-...23600CE...): STATUS_PENDING,
                  unchanged (not migrated, not rewritten)
Audit hash chain: verify() = True (full store, 14 events)
```

No historical record was modified, deleted, or reattributed this phase.

## 9. AG7002 Remediation Status

Unchanged from the prior remediation turn — not re-touched this phase.
`ConfirmedRejectionError` / `_handle_confirmed_rejection()` /
`STATUS_REJECTED` remain in `trading/common/execution.py`;
`tests/common/test_phase_15d_3r_rejection_handling.py` (8 tests, Cases
A–E plus retry-safety and restart durability) all still pass as part of
the full regression above.

## 10. Remaining Limitations

- No durable, structured human-authorization audit event exists yet
  (carried over from the original Phase 15D.3 report — unrelated to this
  remediation).
- No position-based reconciliation corroboration (carried over from
  Phase 15D-RECON's own known limitations).
- The legacy algo config modules' `_angel_creds()` fix is narrowly scoped
  to the credential-leak problem; these modules still use bare,
  non-package-qualified imports (`import config`) requiring `sys.path`
  manipulation — a pre-existing architectural characteristic, not changed
  this phase (out of scope: Rule against redesigning the broker/account
  credential architecture).

---

# FINAL GATE

```
PHASE 15D.3-R REMEDIATION = PASS
```

**HARD STOP.** No live order was placed. No strategy was started. No
canary was performed. Account B authorization state was not changed.
This phase ends here, after software remediation and verification.
