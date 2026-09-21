# Phase 15D.7 — Operator Authentication & Authorization Boundary

**Status: PASS**
**Scope: safety/authentication phase only. No real broker order was placed. No real live authorization was granted. No strategy was started. Account A remains READ_ONLY, Account B remains READ_ONLY/FLAT.**

---

## 1. Objective

Replace trust in a free-text `authorized_by` string with a production-safe
operator-identity boundary that can establish who requested a live
authorization, who confirmed it, whether that identity is permitted, which
account/broker/credential is being used, when it happened, and exactly
what action was authorized — built as a clean abstraction that a future
phase can wire to the repository's existing JWT/RBAC system, without
implementing a second authentication mechanism.

## 2. Initial security gap

Phase 15D.6's `authorized_by` was caller-supplied free text, checked only
for non-emptiness and not looking secret-shaped. Nothing verified the
string named a real, authenticated, permitted person; nothing checked
role or account ownership; nothing distinguished the requesting identity
from the confirming one; and `TradingAccount.owner_id` (added in Phase
15B) was never read or enforced anywhere.

## 3. Architecture before / after

**Before:** `AuthorizationRequest.authorized_by: str` (arbitrary text) →
`validate_authorized_by()` (non-empty, not secret-shaped) → workflow
proceeds with no identity or permission check.

**After:**
```
AuthenticationProvider.resolve_operator(credential) -> OperatorIdentity
        |
AuthorizationRequest.operator: OperatorIdentity   (authorized_by is now DERIVED, never caller-supplied)
        |
validate_request() --calls--> AuthorizationService.evaluate(operator, REQUEST_LIVE_AUTHORIZATION, account)
        |                       (authenticated? active? permission? account ownership? -- all fail-closed)
LiveAuthorizationWorkflow.request()  -- stamps operator_id/display_name/authentication_source/role onto the LiveAuthorization row
        |
LiveAuthorizationWorkflow.confirm() --calls--> AuthorizationService.evaluate(operator, CONFIRM_LIVE_ACTION, account)
        |
execute()'s existing try_consume() call (Phase 15D.5, UNCHANGED in shape) -- now also compares operator_id, one more exact-scope field
```

This is an integration, not a replacement: every existing gate (central
kill switch at step 0, `RiskManager`, `LiveCanaryGuard`, idempotency,
`LiveAuthorization.try_consume()`'s own exact-scope match) runs exactly
where it already did, in the same order, unchanged.

## 4. Files changed

**New:**
- `trading/common/operator_identity.py` — `OperatorIdentity`, `AuthState`,
  `OperatorRole`, `LivePermission`, `ROLE_PERMISSIONS`, `permissions_for_role()`,
  `unauthenticated()`/`invalid_identity()`, `AuthenticationProvider`
  protocol, `FakeAuthenticationProvider` (test-only), `JwtClaimsAuthenticationProvider`
  (production integration seam), `operator_role_from_string()`.
- `trading/common/live_authorization_service.py` — `AuthorizationService.evaluate()`,
  `AuthorizationDecision`, fail-closed on every branch.
- `tests/test_phase_15d_7_operator_authorization.py` — 57 tests.
- `docs/phase-15d-7-implementation-plan.md`, this report.

**Modified:**
- `trading/common/live_authorization.py` — additive `operator_id`,
  `operator_display_name`, `authentication_source`, `role`,
  `permission_used` fields on `LiveAuthorization` (all default `""`);
  matching additive columns on `SqliteLiveAuthorizationStore`'s schema
  (with an `ALTER TABLE ... ADD COLUMN` migration guard for a pre-existing
  database file, defensive-only — none exists in this repository);
  `create()` gains the same fields as optional keyword arguments;
  `try_consume()` gains an optional `operator_id` parameter, compared
  exactly like every other scope field **only when the caller supplies
  one** (`None` skips the check, preserving every pre-15D.7 caller's
  exact behavior).
- `trading/common/execution.py` — the existing (Phase 15D.5) `try_consume()`
  call now additionally passes `operator_id=intent.metadata.get("operator_id")`.
  No new gate, no reordering — one more argument on an existing call.
- `trading/common/risk_manager.py` — **carried over from Phase 15D.6's
  own fix**, unrelated to this phase's identity work but re-verified:
  `validate(..., dry_run=False)` remains additive and untouched.
- `trading/common/live_authorization_workflow.py` — `AuthorizationRequest.operator`
  replaces the free-text `authorized_by` input (`authorized_by` is now a
  derived `@property`); `validate_request()` gains a required
  `authorization_service` parameter and evaluates
  `REQUEST_LIVE_AUTHORIZATION` before any structural/risk check;
  `LiveAuthorizationWorkflow.confirm()` gains `operator`, `account`, and
  `authorization_service` parameters and evaluates `CONFIRM_LIVE_ACTION`
  before human confirmation; new audit-emission helper
  `_audit_operator_event()`; five new event constants.
- `tests/common/test_phase_15d_6_live_authorization_workflow.py` —
  updated (not rewritten) to construct requests with an `OperatorIdentity`
  and pass the new `confirm()`/`validate_request()` parameters; every
  original assertion is unchanged, still 43/43 passing.

## 5. Authentication abstraction

`AuthenticationProvider.resolve_operator(credential) -> OperatorIdentity`
never raises for a missing/malformed credential — it returns
`unauthenticated()` or `invalid_identity()` (both `is_authenticated ==
False`), so every caller has one uniform fail-closed path instead of a
mix of exceptions and `None`s.

- `FakeAuthenticationProvider` — in-memory, test-only, used throughout
  this phase's own suite and Phase 15D.6's updated suite. Never touches a
  real credential or session.
- `JwtClaimsAuthenticationProvider` — the production integration point.
  Deliberately imports neither FastAPI nor SQLAlchemy: it accepts a plain
  claims dict of the exact shape `trading/api/deps.py::_user_principal_from_bearer`
  already produces after calling the **existing**
  `trading.api.security.tokens.decode_access_token()` and loading the
  `User` row (`sub`, `username`, `role`, `is_active`). A future
  `trading/api` route wires this by resolving the caller via the
  existing `get_principal()`/JWT machinery exactly as every other
  authenticated route already does, then handing this provider the
  resulting claims — no second authentication mechanism is created.
  `trading/api/security/permissions.py`'s own `Permission` enum and
  `ROLE_PERMISSIONS` are **not modified**; this phase's `LivePermission`
  is a separate, purpose-scoped vocabulary for exactly the live-order
  actions this phase and Phase 15D.5/15D.6 care about, keeping this
  safety-focused phase's blast radius out of the already-shipped
  control-center API and its own RBAC/tests.

## 6. Authorization model

`AuthorizationService.evaluate(operator, permission, account=...)` is
stateless and fails closed on every branch (an exception during
evaluation is caught and turned into `DENIED`, mirroring `RiskManager`'s
own `INTERNAL_ERROR` pattern):

1. `operator is None` → DENIED.
2. `not operator.is_authenticated` (covers `UNAUTHENTICATED`, `INVALID`,
   and expired/malformed states) → DENIED.
3. `not operator.is_active` → DENIED.
4. `not operator.operator_id` → DENIED.
5. `not operator.has(permission)` → DENIED.
6. `account.owner_id` set and `!= operator.operator_id` → DENIED. An
   account with **no** declared owner (this project's default for every
   account created before Phase 15B's ownership field existed) is
   treated as house/shared — still gated by every check above, just not
   additionally denied by ownership, so no pre-existing account is
   silently locked out.

Roles: `VIEWER` (`READ_MARKET_DATA`, `READ_ACCOUNT`), `TRADER` (+
`REQUEST_LIVE_AUTHORIZATION`, `CONFIRM_LIVE_ACTION`, `EXECUTE_LIVE_ACTION`),
`ADMIN` (every `LivePermission`, including `KILL_TRADING`). The existing
`trading/api` `"operator"` role string maps to `TRADER` here — centralized
in one place (`_ROLE_STRING_MAP` / `operator_role_from_string()`) so the
mapping can change later without touching the authorization pipeline
itself.

**`KILL_TRADING` is defined but deliberately NOT wired into
`CentralKillSwitch.engage()`** in this phase. The kill switch is an
existing, independent, unconditional (execute() step 0) safety primitive;
gating it behind a permission check is one bug away from "no one can
press it in an emergency" — a safety regression, not an improvement.
`test_09_kill_switch_fail_safe_independent_of_permissions` proves the
switch still blocks execution identically for an `ADMIN` identity holding
every permission including `KILL_TRADING`, because the check still runs
unconditionally ahead of anything this phase added.

## 7. Account isolation

`TradingAccount.owner_id` (Phase 15B, previously unused) is now read and
enforced by `AuthorizationService`. Verified: an operator who does not
own an account cannot request or confirm a live authorization scoped to
it (tests 10, 5); an authorization scoped to one account cannot execute
against a different one even after a strategy reassignment (tests 11,
12, mirroring Phase 15D.6's own `test_case_21` technique — `execute()`
resolves the account via `StrategyAssignment`, not `intent.account_id`,
so that reassignment is the only way to actually change which account an
execution resolves to).

## 8. Audit changes

Five new events on the existing `PersistentAuditTrail` (the hash-chained
trail Phase 15D.5 already writes to — **not** the separate SQL-backed
`trading/api/security/audit.py` log, which is an HTTP-request concern):
`OPERATOR_AUTHENTICATED`, `OPERATOR_AUTHORIZATION_REQUESTED`,
`OPERATOR_AUTHORIZATION_DENIED`, `OPERATOR_CONFIRMATION_ACCEPTED`,
`OPERATOR_CONFIRMATION_DECLINED`. `AUTHORIZATION_CREATED` /
`AUTHORIZATION_CONSUMED` (Phase 15D.5) are reused as-is, not duplicated —
they gain the new operator fields as additional detail on the same
event. Every new event carries `correlation_id`/`idempotency_key` for
cross-event tracing; audit-write failures never raise into a gate
(`_audit_operator_event()` swallows exceptions, matching every other
`_audit()` helper in this codebase).

## 9. Test counts

| Suite | Result |
|---|---|
| `tests/test_phase_15d_7_operator_authorization.py` (new) | **57 passed / 0 failed** |
| `tests/common/test_phase_15d_6_live_authorization_workflow.py` (updated) | **43 passed / 0 failed** |
| `tests/common/test_phase_15d_5_live_authorization.py` (unmodified) | **47 passed / 0 failed** |
| `tests/algos/test_angel_creds_env_isolation.py` + `tests/preflight/test_environment_isolation.py` | **21 passed / 0 failed** |
| Combined targeted run (above four suites together) | **168 passed / 0 failed** |

## 10. Full regression result

`python -m pytest -q --tb=line > file.txt 2>&1; echo "EXIT=$?" >> file.txt`
(redirect-based, never piped through `tail`) → `EXIT=0`. As in Phase
15D.6, the final `"N passed"` summary line itself was again swallowed by
the same pre-existing, unrelated background-thread warning noise
(`PytestUnhandledThreadExceptionWarning` from `_manage_pending`'s
`orderBook` polling against a fake SmartAPI, documented since Phase
15D.3-R) — verified instead by counting result characters directly:
**1585 result characters, zero `F`/`E`/`s`/`x` among them** (up from
Phase 15D.6's 1530 by +55, consistent with this phase's 57 new tests
plus minor collection variance).

## 11. Broker mutation count

**0.** Every test in `tests/test_phase_15d_7_operator_authorization.py`
uses `RecordingFakeBroker` (imported from Phase 15D.6's test file, not
redefined); no test in this file's 57 cases ever reaches a successful
`place_order()` call — the two tests that do call `execute()` (kill-switch
fail-safe, account-isolation) both assert the execution failed with
`mutation_call_count == 0`.

## 12. Live account state

- Account A: READ_ONLY (unchanged).
- Account B: READ_ONLY / FLAT (unchanged — no real order was placed).
- Live authorization: **NOT GRANTED** — confirmed no `*live_auth*.db` file
  exists anywhere in the repository outside test `tmp_path` databases.
- Strategies: STOPPED (none started this phase).

## 13. Historical-integrity verification

Directly re-read from `trading/phase15d2_idempotency.db`:

- `260917000350205`: `status=COMPLETED` — **unchanged**.
- AG7002 record (`phase15d2-canary-NIFTY22SEP2623600CE...`):
  `status=PENDING` — **unchanged**.
- `260917000523943` (manual SELL): no idempotency record exists for it —
  **remains unattributed to TCC**, as before.
- `trading/phase15d2_audit.db`'s hash chain: `PersistentAuditTrail.verify()`
  → `True`, 14 records (unchanged count).

## 14. Known limitations

- `JwtClaimsAuthenticationProvider` is a bridge, not a wired route — no
  `trading/api` endpoint calls it yet. Wiring an actual "request live
  authorization" HTTP endpoint is future work, explicitly out of scope
  for this safety/authentication-boundary phase.
- `KILL_TRADING` exists in the `LivePermission` vocabulary but is not
  consulted anywhere yet (by design — see section 6).
- Account ownership enforcement treats an unset `owner_id` as
  house/shared rather than "no one may use it" — this preserves every
  pre-existing account's usability but means ownership is opt-in per
  account, not a repository-wide guarantee, until every account has an
  explicit owner assigned.
- `ConfirmationProvider` remains a narrow callable (Phase 15D.6), not a
  real interactive surface — unchanged and out of scope here.

## 15. Future SSO integration plan

A future phase adds a `trading/api` route that (a) resolves the caller
via the existing `get_principal()` dependency exactly as every other
authenticated route does, (b) builds a plain claims dict
(`sub`/`username`/`role`/`is_active`) from the resulting `Principal`, and
(c) passes it to `JwtClaimsAuthenticationProvider.resolve_operator()` to
obtain the `OperatorIdentity` this phase's `AuthorizationRequest` and
`LiveAuthorizationWorkflow.confirm()` require. Replacing local
username/password auth with real corporate SSO/OIDC later only changes
*how* `trading/api` obtains that `Principal` (an OIDC middleware instead
of `trading/api/auth_routes.py`'s password flow) — nothing in
`trading/common`'s operator-identity or authorization-service layer needs
to change, since neither ever depended on JWTs specifically, only on an
already-resolved identity, role, and permission set.

---

## Conclusion

```
PHASE 15D.7 = PASS
NO LIVE AUTHORIZATION GRANTED.
NO LIVE ORDER SUBMITTED.
HARD STOP.
```
