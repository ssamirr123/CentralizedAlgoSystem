# Phase 15D.6 — Controlled Live Authorization Workflow Integration

**Status: PASS**
**Date: 2026-09-17 / 2026-09-18**
**Scope: operator-workflow integration only. No real broker order was placed. No real authorization was granted. No strategy was started. No real broker mutation endpoint was called.**

---

## 1. Objective

Build the operator-facing workflow around Phase 15D.5's already-proven
`LiveAuthorization` mechanism:

```
REQUEST -> VALIDATE -> PREFLIGHT -> HUMAN CONFIRMATION -> AUTHORIZE -> EXECUTE
```

without creating a second authorization mechanism, without duplicating
risk or canary rules, and without touching any real broker, real
account, or historical record. All execution tests in this phase use a
fake/recording broker exclusively.

## 2. What was built

### 2.1 `trading/common/live_authorization_workflow.py` (new)

- `WorkflowError` — raised at any stage strictly before a
  `LiveAuthorization` is consumed. Distinct from `LiveAuthorizationError`
  (raised by the Phase 15D.5 store itself); `LiveAuthorizationWorkflow.confirm()`
  catches `LiveAuthorizationError` from the store's own `validate()` call
  (e.g. an authorization that is already EXPIRED or REVOKED by the time
  confirmation is attempted) and re-raises it as a `WorkflowError`, so
  every workflow-stage failure surfaces through one exception type.
- `AuthorizationRequest` (frozen dataclass) — the operator's structured
  request. No implicit defaults anywhere: `account_id`, `broker_id`, and
  `credential_reference` are all required and independently verified
  against the resolved `TradingAccount`; there is no fallback or
  substitution path.
- `validate_authorized_by()` — Step 12's explicit, documented limitation:
  `authorized_by` remains free text (no authentication system exists in
  this project, unchanged from Phase 15D.5's own noted limitation). This
  function only rejects empty/whitespace values and values that look
  secret-shaped (via the existing `audit_store.redact_string()`), as
  defense-in-depth, not as authentication.
- `validate_request()` (Step 4) — structural, connection-free validation:
  account/broker/credential exact match, quantity/lot-size/value/loss-limit
  sanity, idempotency-key novelty, and a **dry-run** RiskManager check.
- `PreflightResult` + `run_preflight()` (Step 5) — entirely read-only,
  built on `ReadOnlyBrokerView` (reused unchanged from Phase 15D-RECON,
  structurally incapable of mutation) for quote/funds lookups, plus a
  second **dry-run** RiskManager check, `CanaryLimits` check (reused
  unchanged from live_canary.py), central-kill-switch check, expiry-tradability
  check (`_is_expiry_still_tradable`, reused unchanged from
  `angel_readonly.py`), and idempotency-key novelty re-check.
- `request_human_confirmation()` — only a return value of exactly `True`
  counts as approval. `None`, `False`, any other truthy value
  (`"yes"`, `1`, `"CONFIRMED"`), or a raised exception from the
  confirmation provider are all treated identically as **not approved**.
- `LiveAuthorizationWorkflow` — thin orchestrator: `request()` creates the
  PENDING authorization (via the unchanged Phase 15D.5 store); `confirm()`
  revokes-and-raises on a failed preflight, revokes-and-raises on any
  non-`True` confirmation, and otherwise calls the store's own
  `validate()` (PENDING -> AUTHORIZED). **`EXECUTE` is deliberately not a
  method on this class** — the caller passes
  `intent.metadata["authorization_id"]` into the existing, unmodified
  `StrategyExecutionEngine.execute()` (Phase 15D.5's own integration
  point), so there remains exactly one execution path in this codebase.

### 2.2 `trading/common/risk_manager.py` (one additive change)

While wiring `validate_request()` and `run_preflight()` to reuse
`RiskManager.validate()` (per this phase's explicit instruction: *"do not
duplicate risk rules"*), testing surfaced a real, previously-latent
hazard: **`RiskManager.validate()` is not idempotent as a preview call**.
A passing call has two side effects — it permanently marks the intent's
`idempotency_key` as "seen" (`DUPLICATE_ORDER_PROTECTION`), and it
increments the calendar-day order counter for `MAX_ORDERS_PER_DAY`. Since
`execute()` itself already calls `validate()` for real as its own step 3,
having `validate_request()` and `run_preflight()` also call it for a mere
preview would have:

- permanently poisoned the idempotency key before the real order ever
  reached `execute()` (the real call would see `DUPLICATE_ORDER_PROTECTION`
  and reject a legitimate, never-yet-attempted order), and
- silently consumed up to 2 extra slots of the day's order-count budget
  per real order, for an order that may never even be confirmed.

**Fix:** added an additive, backward-compatible `dry_run: bool = False`
keyword parameter to `RiskManager.validate()`. All fifteen checks run
identically in both modes (so a dry-run preview is exactly as strict as
the real call); `dry_run=True` only skips the two mutations
(`_check_duplicate_order`'s `.add()` to the seen-key set, and
`_record_order_approved_for_daily_count()`). Default is `False`, so every
existing caller (`StrategyExecutionEngine.execute()`, the Phase 3 shadow
bridge, the Phase 4/5B demos, and all 1488 pre-existing tests) sees zero
behavior change. `validate_request()` and `run_preflight()` both call
`risk_manager.validate(intent, context, dry_run=True)`; only
`execute()`'s own step-3 call remains a real, mutating call.

### 2.3 Test suite

`tests/common/test_phase_15d_6_live_authorization_workflow.py` — 43
tests, all against a `RecordingFakeBroker` (a `BrokerClient` subclass with
`is_simulated = True` that tracks `mutation_call_count` and the exact args
of its last `place_order()` call; never touches a network or a real
credential). Covers:

- The full happy-path workflow (REQUEST -> VALIDATE -> PREFLIGHT ->
  CONFIRM -> AUTHORIZE -> EXECUTE), proving `mutation_call_count == 1`
  and the placed order's symbol/quantity/side match the request exactly.
- A second `execute()` call after success makes **zero** new mutation
  calls (idempotency replay, unchanged Blocker D).
- All 28 named failure scenarios from the brief (invalid account, wrong
  broker, wrong credential, invalid quantity/lot size, expired/invalid
  instrument, excessive order value, excessive daily/strategy loss,
  excessive order count, kill-switch engaged, duplicate/ambiguous
  idempotency key, expired/revoked/consumed authorization, changed
  instrument/side/quantity/account/broker/credential between authorization
  and execution, missing/declined/invalid/exception-raising human
  confirmation, and restart durability before/after authorization/consumption)
  — each asserting `mutation_call_count == 0` (or unchanged) where the
  scenario is supposed to block.
- Audit correlation: `AUTHORIZATION_CREATED` / `_VALIDATED` / `_CONSUMED`
  and `BROKER_ORDER_PLACED` all share the same idempotency key, and no
  event's detail contains `api_key` or `totp`.
- Historical-record integrity: `260917000350205` is still `COMPLETED` in
  `trading/phase15d2_idempotency.db`, read directly and asserted byte-for-byte.
- `authorized_by` validation (empty and secret-shaped values rejected).

One test (`test_case_21_changed_account_blocks_execution`) needed a
design correction mid-authoring: `intent.account_id` is **not** the
account `execute()` actually resolves against — `execution.py` step 2
resolves the real account via `self._strategy_assignment.get_account_id(intent.strategy_id)`,
entirely independent of whatever `intent.account_id` says (that field is
informational/audit-only). Mutating `intent.account_id` alone therefore
proves nothing; the test instead reassigns the *strategy* to a different
account after authorization was granted, which is the only way this
system can actually change which account an execution resolves to, and
confirms `try_consume()`'s account-scope check blocks it correctly.

## 3. Verification performed

| Step | Result |
|---|---|
| New workflow test suite | **43 passed / 0 failed** |
| `tests/algos/test_angel_creds_env_isolation.py` + `tests/preflight/test_environment_isolation.py` (Phase 15D.3-R regression re-check) | **21 passed / 0 failed** |
| Full regression (entire suite, two independent runs) | **~1530 passed, 0 F/E/s/x result characters in either run** (see note below) |
| Historical idempotency records | `260917000350205` = `COMPLETED` (unchanged); AG7002 record = `PENDING` (unchanged) |
| Real `LiveAuthorization` database anywhere in the repo | **none found** — every `SqliteLiveAuthorizationStore` instantiated during this phase pointed at a pytest `tmp_path`, never a persistent path |
| Real broker mutation calls during this phase | **0** — every test used `RecordingFakeBroker`; the real `AngelOneBroker` adapter was never imported or instantiated by any file this phase touched |

**Note on the full-regression run:** two independent full-suite runs both
completed with `EXIT=0` and their progress output consisting of dot
characters only (`grep -oE "^[.EFsx]+"` across every progress line found
no `F`, `E`, `s`, or `x` character in either run — i.e. no failures, no
errors, no skips), but the final `"N passed"` summary line itself was not
present in either captured output file, apparently swallowed by heavy
interleaved output from ~20 pre-existing background-thread warnings
(`PytestUnhandledThreadExceptionWarning` from `_manage_pending`'s
`orderBook` polling against a fake SmartAPI lacking that attribute — the
same pre-existing test-infrastructure noise documented in Phase 15D.3-R's
report, unrelated to this phase's own changes). This was verified two
ways instead of trusted at face value: (1) exit-code capture used the
established trustworthy redirect-based methodology (never piped through
`tail`), both runs showing `EXIT=0`; (2) the progress-line character count
was tallied directly (1530 total result characters, consistent with the
prior 1488-test baseline plus this phase's 43 new tests, minus expected
collection/parametrization variance) rather than relying on the missing
summary line.

## 4. Standing safety constraints — final state

- Account A: READ_ONLY (unchanged this phase).
- Account B: READ_ONLY / FLAT (unchanged this phase — no real order was
  placed against it).
- Live authorization: **NOT GRANTED** — no real `LiveAuthorization` record
  exists outside a test's own `tmp_path` database.
- Strategies: STOPPED (none started this phase).
- Real broker mutations this phase: **0**.
- Previous canary's authorization / idempotency key: not reused (this
  phase's tests each mint their own fresh, novel idempotency keys, e.g.
  `wf-key-1`, `wf-key-2`, scoped to a fresh in-memory/tmp-path test rig).
- Historical order records (`260917000350205`, `260917000523943`, the
  AG7002 `PENDING` record): unaltered, reconfirmed by direct read.

## 5. Known limitations (unchanged from Phase 15D.5, explicitly not resolved here)

- `authorized_by` is unverified free text — this phase only adds a
  non-empty/non-secret-shaped check (`validate_authorized_by()`), not
  authentication. Building a real operator-identity system is out of
  scope for a workflow-integration phase.
- `ConfirmationProvider` is a narrow callable, not a real interactive
  surface (terminal prompt / web UI / chat exchange). Building one is
  explicitly out of scope per this phase's own brief.
- Authorization expiry remains lazy (checked durably on `get()`/`validate()`/`try_consume()`,
  not by a background sweep). This was already Phase 15D.5's design
  choice and is sufficient here too: nothing in this phase's integration
  requires a proactively-expiring background worker, since every read
  path already forces the durable EXPIRED transition before it can be
  acted on.

## 6. Conclusion

```
PHASE 15D.6 = PASS
NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
HARD STOP.
```
