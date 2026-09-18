# Phase 15D.7 — Implementation Plan: Operator Authentication & Authorization Boundary

**Status: PLAN — awaiting go-ahead before implementation.**

This phase does not place any order, grant any live authorization, or
touch any historical record. It replaces the free-text `authorized_by`
trust model with a real operator-identity boundary sitting in front of
the existing, unmodified Phase 15D.5/15D.6 mechanism.

---

## 1. Current authentication model (read from the repository)

An existing, real, production authentication framework already exists
under `trading/api/` — this is **not** a greenfield problem:

- `trading/api/security/tokens.py` — short-lived signed JWT access
  tokens (`HS256`, `create_access_token()`/`decode_access_token()`) plus
  opaque, hashed-at-rest refresh tokens. Backed by `AUTH_SECRET_KEY`.
- `trading/database/models.py::User` — a real `users` table: `username`,
  `password_hash` (bcrypt), `role` (string), `extra_permissions` (JSON
  list), `is_active`, `must_change_password`.
- `trading/api/security/permissions.py` — `Permission` enum (`VIEW`,
  `START`, `STOP`, `RESTART`, `TRADING_CONTROL`, `ADMIN`) and
  `ROLE_PERMISSIONS` (`viewer` / `trader` / `operator` / `admin` fixed
  bundles, `permissions_for(role, extra)` merges per-user grants).
- `trading/api/deps.py::Principal` — the resolved-caller dataclass
  (`kind: "user"|"service"`, `permissions`, `user_id`, `username`,
  `role`, `must_change_password`), produced by `get_principal()` from
  either a bearer JWT or the one fixed machine `X-API-Key`.
- `trading/api/security/audit.py` — a **separate**, SQLAlchemy-backed
  audit log (`models.AuditLog`) used for API/HTTP-level actions
  (login, algo start/stop, user admin, kill-switch-via-API). This is
  distinct from `trading.common.audit_store.PersistentAuditTrail`, the
  hash-chained append-only trail the execution pipeline and
  `LiveAuthorization` already write to (Phases 15D-audit / 15D.5 /
  15D.6).

This satisfies the brief's condition exactly: *"Do NOT implement actual
corporate SSO/OAuth unless the repository already contains an existing
authentication framework."* One does. Phase 15D.7 must **integrate
with it**, not replace or duplicate it.

## 2. Current authorization model (as of Phase 15D.6)

- `TradingAccount.owner_id` (Phase 15B) already exists — "who this
  account belongs to" — but nothing in the live-authorization or
  execution path currently reads or enforces it.
- `LiveAuthorization.authorized_by` is free text, supplied by whatever
  caller invokes `LiveAuthorizationWorkflow.request()`. Phase 15D.6 added
  `validate_authorized_by()`, which only rejects empty or
  secret-shaped strings — it does not verify the string names a real,
  authenticated, permitted person.
- `LiveAuthorizationWorkflow.confirm()`'s `ConfirmationProvider` is an
  arbitrary `Callable[[PreflightResult], bool]` — nothing ties the
  `True` it returns to *who* pressed the button.
- Every exact-scope check that exists today (account/broker/credential/
  instrument/side/quantity/order-type/product-type/idempotency-key, all
  enforced atomically in `try_consume()`) is unaffected by, and will
  remain completely unaffected by, this phase — Step 7 of the brief only
  adds `operator_id` as one more field in that same exact-match set.

## 3. The security gap this phase closes

Today, "who authorized this trade" is answered only by an unverified
string an operator could type as anything (`"Samir"`, `"whoever"`, empty
was already rejected in 15D.6, but `"asdf"` was not). There is no binding
to:

- a real, authenticated identity (a JWT-backed session, or any
  authentication at all),
- a role or permission check (a viewer-only account could type
  `authorized_by="admin"` and nothing would stop it),
- account ownership (nothing stops operator X from typing a request
  scoped to an account they do not own),
- a durable record of *which* authenticated identity did the confirming,
  separate from the requesting.

## 4. Proposed architecture

### 4.1 New module: `trading/common/operator_identity.py`

Framework-agnostic (no FastAPI/SQLAlchemy import — stays consistent with
every other `trading/common` module, importable in a plain test process),
mirroring the *shape* of the existing `trading/api` model but scoped to
the specific live-trading actions this phase cares about:

```
class AuthState(str, Enum):
    AUTHENTICATED = "AUTHENTICATED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID = "INVALID"

class LivePermission(str, Enum):
    READ_MARKET_DATA = "READ_MARKET_DATA"
    READ_ACCOUNT = "READ_ACCOUNT"
    REQUEST_LIVE_AUTHORIZATION = "REQUEST_LIVE_AUTHORIZATION"
    CONFIRM_LIVE_ACTION = "CONFIRM_LIVE_ACTION"
    EXECUTE_LIVE_ACTION = "EXECUTE_LIVE_ACTION"
    KILL_TRADING = "KILL_TRADING"

class OperatorRole(str, Enum):
    VIEWER = "VIEWER"
    TRADER = "TRADER"
    ADMIN = "ADMIN"

ROLE_PERMISSIONS: dict[OperatorRole, frozenset[LivePermission]] = {
    OperatorRole.VIEWER: {READ_MARKET_DATA, READ_ACCOUNT},
    OperatorRole.TRADER: VIEWER's set | {REQUEST_LIVE_AUTHORIZATION, CONFIRM_LIVE_ACTION, EXECUTE_LIVE_ACTION},
    OperatorRole.ADMIN:  every LivePermission (incl. KILL_TRADING),
}

@dataclass(frozen=True)
class OperatorIdentity:
    operator_id: str            # stable id -- e.g. "user:42", never a credential
    display_name: str
    role: OperatorRole
    auth_state: AuthState
    authentication_source: str  # e.g. "jwt", "fake-test", future "sso:okta"
    permissions: frozenset[LivePermission]
    is_active: bool = True

    @property
    def is_authenticated(self) -> bool: ...
    def has(self, permission: LivePermission) -> bool: ...
```

This is a **new, purpose-scoped** enum/role set, not a modification of
`trading/api/security/permissions.py`'s `Permission` enum. Rationale:
that enum is live production RBAC for the whole control-center API
(algo start/stop, user admin, etc.) — six capabilities that have nothing
to do with live-order authorization semantics. Extending it with
trading-specific permissions would widen the blast radius of this
safety-focused phase into unrelated, already-shipped API surface for no
safety benefit, and risks an accidental grant (e.g. if `admin` silently
picked up `KILL_TRADING` in a context where that wasn't reviewed).
Keeping `LivePermission` a separate, additive vocabulary — while still
following the exact same *shape* (`Enum` + role→permission-set dict +
`permissions_for`-style merge) — satisfies "the exact names may follow
existing repository conventions" without touching working production
code outside this phase's own scope.

### 4.2 New module: authentication provider abstraction

```
class AuthenticationProvider(Protocol):
    def resolve_operator(self, credential: Any) -> OperatorIdentity: ...
```

Two implementations ship in this phase:

- `FakeAuthenticationProvider` (`trading/common/operator_identity.py`) —
  an in-memory `dict[str, OperatorIdentity]` keyed by an opaque token
  string, used by every test in this phase and by any local/manual
  exercise of the workflow. Returns an `OperatorIdentity` with
  `auth_state=UNAUTHENTICATED` (not a `None`/exception) for an unknown
  token, and `INVALID` for a malformed one — callers must check
  `is_authenticated` before proceeding; nothing here ever raises to
  signal "not logged in".
- `JwtClaimsAuthenticationProvider` — the **production integration
  point**. Takes already-decoded JWT claims (the same shape
  `trading/api/deps.py::_user_principal_from_bearer` already produces
  after calling `decode_access_token()` and loading the `User` row) and
  maps them to an `OperatorIdentity`: `user.role` (`"viewer"`/`"trader"`/
  `"operator"`/`"admin"`) is mapped to `OperatorRole` (`operator` maps to
  `TRADER` for this purpose — an "operator" in the existing RBAC already
  has `TRADING_CONTROL`, the closest existing analog to live-order
  authority), `authentication_source="jwt"`. **Deliberately does not
  import FastAPI or SQLAlchemy** — the caller (a future `trading/api`
  route) is responsible for decoding the token and loading the `User`
  row using the *existing* `trading.api.security.tokens` +
  `trading.database.models.User` machinery, then handing this provider
  a plain dict of claims. This keeps `trading/common` dependency-light
  (matches every other module in it) while giving a real, wireable path
  to the JWT system that already exists — not a mock of one.
- Future SSO/OIDC only ever needs a third `AuthenticationProvider`
  implementation; nothing else in this design changes.

### 4.3 New module: `trading/common/live_authorization_service.py`

`AuthorizationService.evaluate(operator, live_permission, request:
AuthorizationRequest, *, resolved_account: TradingAccount) ->
AuthorizationDecision` (`AUTHORIZED` / `DENIED` + reason), fail-closed on
every branch:

- `operator.is_authenticated` false -> DENIED (`"not authenticated"`).
- `operator.is_active` false -> DENIED (`"operator disabled"`).
- `not operator.has(live_permission)` -> DENIED (`"missing permission"`).
- `resolved_account.owner_id` set and `!= operator.operator_id` -> DENIED
  (`"operator does not own this account"`). An account with an empty
  `owner_id` (this repo's existing default for every account created
  before Phase 15B's ownership field existed) is treated as
  **house/shared**, not "no restriction" — still requires the permission
  check above, but does not add an extra ownership denial; this avoids
  silently locking every pre-existing account out from day one while
  never *weakening* the check for an account that has explicitly
  declared an owner.
- Any exception during evaluation -> DENIED (mirrors `RiskManager`'s own
  fail-closed `INTERNAL_ERROR` pattern).

This service runs **in addition to**, never instead of, every existing
gate (kill switch, `RiskManager`, `LiveCanaryGuard`, idempotency,
`LiveAuthorization.try_consume()`'s own exact-scope match). It is
consulted at two points, mirroring Steps 5 and 8 of the brief:

1. Inside `validate_request()` (Step 4/5 of the 15D.6 workflow) with
   `LivePermission.REQUEST_LIVE_AUTHORIZATION` — before a `PENDING`
   `LiveAuthorization` row is even created.
2. Inside `LiveAuthorizationWorkflow.confirm()` with
   `LivePermission.CONFIRM_LIVE_ACTION` — before the store's own
   `validate()` (`PENDING -> AUTHORIZED`) is called.

`LivePermission.EXECUTE_LIVE_ACTION` is checked once more, at the moment
`intent.metadata` carries both `authorization_id` and the operator
context, immediately alongside the existing `try_consume()` call in
`execution.py` — see 4.5.

`LivePermission.KILL_TRADING` is **not** wired into
`CentralKillSwitch.engage()` in this phase. The kill switch is an
existing, independent, unconditional (step-0) safety primitive that
must never gain a new failure mode that could make it *harder* to
engage in an emergency — gating it behind a permission check one line of
buggy code away from "no one can press it" is a net safety regression,
not an improvement. `KILL_TRADING` is defined now (so the `LivePermission`
vocabulary is complete per the brief) and reserved for a future,
separate, carefully-scoped phase that explicitly reviews the kill
switch's own call sites; this phase's test suite instead proves the
*opposite* property the brief actually asks for ("kill-switch permission
behavior remains fail-safe"): that engaging the switch blocks execution
identically regardless of the acting operator's permissions, because the
switch check still runs unconditionally at step 0, ahead of anything
this phase adds.

### 4.4 Binding identity into `LiveAuthorization` (Step 7)

Additive columns/fields on `LiveAuthorization` (all with defaults, so
`_row_to_record`'s generic `**row` construction keeps working and no
historical row — none currently exist outside test `tmp_path` databases,
confirmed in Phase 15D.6's own report — needs a migration):

```
operator_id: str = ""
operator_display_name: str = ""
authentication_source: str = ""
role: str = ""
permission_used: str = ""
```

`authorized_by` is **kept, unchanged, still required** — for a
newly-created authorization it is populated from
`operator.display_name` (never accepted as free text from the caller
once an `OperatorIdentity` is available), preserving every existing
15D.5/15D.6 test and code path that reads `authorized_by` while removing
the ability for a caller to just type an arbitrary name once this
phase's `AuthorizationRequest` requires an `OperatorIdentity`.

`try_consume()`'s exact-scope match gains one more required-equal field:
`operator_id` (the operator who *requested* must be the same one whose
authorization is being consumed — this is naturally already true since
`operator_id` is stamped into the row at `create()` time and the
consuming intent's metadata must carry the same value, but making it an
explicit compared field, like every other scope field, means a future
code path can never accidentally bypass it).

### 4.5 `AuthorizationRequest` / workflow changes (`live_authorization_workflow.py`)

`AuthorizationRequest` gains a required `operator: OperatorIdentity`
field (replacing the free-text `authorized_by: str` **as an input** —
the dataclass still carries an `authorized_by` derived field for
backward-compatible reads, but it is computed from
`operator.display_name`, never accepted as a separate caller-supplied
argument). `validate_authorized_by()` (15D.6) is retained internally as
a defense-in-depth check on the derived value, but a caller can no
longer influence `authorized_by` independently of who they authenticated
as.

`validate_request()` gains an `authentication_provider` +
`authorization_service` parameter pair, calls
`authorization_service.evaluate(..., LivePermission.REQUEST_LIVE_AUTHORIZATION, ...)`
immediately after resolving the account, before any of the existing
structural/risk checks — fails closed with a `WorkflowError` on `DENIED`.

`LiveAuthorizationWorkflow.confirm()` gains an `operator: OperatorIdentity`
parameter (the *confirming* identity — may differ from the *requesting*
one; both are recorded) and evaluates
`LivePermission.CONFIRM_LIVE_ACTION` before calling
`request_human_confirmation()`. A denial revokes the pending
authorization and raises `WorkflowError`, exactly like every other
`confirm()`-stage failure already does.

`execution.py`'s existing Phase 15D.5 gate (the `try_consume()` call) is
**not modified in shape** — it already fails closed on any
`LiveAuthorizationError`, and `try_consume()`'s own exact-scope match
(4.4) now includes `operator_id`, so a mismatched or missing operator on
the intent is rejected by the exact same, already-proven, atomic
compare-and-swap path. No new gate is inserted into `execute()` itself;
the identity/permission boundary lives entirely in the workflow layer
that runs *before* `execute()` is ever called, matching this phase's own
Hard Safety Rule 7 ("do not bypass `LiveAuthorizationWorkflow`") and
Hard Safety Rule 6 ("do not weaken or reorder existing safety gates").

### 4.6 Audit

New events, appended to the same `trading.common.audit_store.PersistentAuditTrail`
`LiveAuthorization` already writes to (not the separate API-level SQL
audit log — this is a trading-pipeline concern, not an HTTP-request
concern):

```
OPERATOR_AUTHENTICATED
OPERATOR_AUTHORIZATION_REQUESTED
OPERATOR_AUTHORIZATION_DENIED
OPERATOR_CONFIRMATION_ACCEPTED
OPERATOR_CONFIRMATION_DECLINED
```

`LIVE_AUTHORIZATION_CREATED`/`LIVE_AUTHORIZATION_CONSUMED` already exist
as `EVENT_AUTHORIZATION_CREATED`/`EVENT_AUTHORIZATION_CONSUMED` in
`live_authorization.py` (Phase 15D.5) — per the brief's own instruction
("do not duplicate existing audit events unnecessarily"), these are
**reused as-is**, only gaining the new operator fields as additional
`**extra` keys in the existing `_audit()` call, not renamed or
duplicated. Every new event carries `correlation_id` for cross-event
tracing, exactly like every existing event in this trail.

## 5. Test strategy

New file `tests/test_phase_15d_7_operator_authorization.py`, all 36
brief-specified cases plus the account-ownership and kill-switch
fail-safe cases, all against `RecordingFakeBroker` (reused from Phase
15D.6's own test file — imported, not redefined) with
`mutation_call_count == 0` asserted for every single test in this file
(no test in this phase ever reaches a successful broker call — Phase
15D.7 adds a gate in front of the workflow, it does not exercise the
happy path through to a fake fill, since doing so isn't necessary to
prove any of the 36 required properties and keeps this phase's own
blast radius strictly at "gate," never "execute").

## 6. Compatibility strategy

- Every Phase 15D.5 and Phase 15D.6 test continues to pass unmodified:
  `LiveAuthorization`'s new fields all default to `""`, and
  `SqliteLiveAuthorizationStore.create()` gains new optional keyword
  arguments (`operator_id=""`, `operator_display_name=""`,
  `authentication_source=""`, `role=""`, `permission_used=""`) rather
  than replacing `authorized_by`, which remains required exactly as
  before.
- `trading/api/security/permissions.py` and `trading/api/deps.py` are
  **not modified** by this phase — zero risk to the live control-center
  API's existing RBAC and its own test suite.
- No historical record (`260917000350205`, `260917000523943`, the AG7002
  `PENDING` row) is read/written by anything in this phase.

## 7. Future SSO integration point

`JwtClaimsAuthenticationProvider` is the seam: a future phase wires a
new `trading/api` route (e.g. an internal "request live authorization"
endpoint) that (a) resolves the caller via the *existing*
`get_principal()`/`decode_access_token()` machinery exactly as every
other authenticated route already does, (b) builds a plain claims dict
from the resulting `Principal`, and (c) passes it to
`JwtClaimsAuthenticationProvider.resolve_operator()`. Swapping in a real
corporate SSO/OIDC provider later only means replacing *how*
`trading/api` obtains that `Principal` (e.g. via an OIDC middleware
instead of local password auth) — nothing in `trading/common`'s
operator-identity/authorization-service layer needs to change, since it
never depended on JWTs specifically, only on an already-decoded identity
+ role + permission set.

---

## Next step

Awaiting go-ahead to implement exactly this design (Steps 3–13 of the
brief). No code has been written yet beyond this plan document.
