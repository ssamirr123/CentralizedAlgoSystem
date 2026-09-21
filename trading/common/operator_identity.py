"""
Phase 15D.7 -- the operator-identity boundary that replaces trust in a
free-text `authorized_by` string with a real, authenticated identity plus
an explicit permission set.

This module is deliberately framework-agnostic (no FastAPI, no
SQLAlchemy import) -- consistent with every other module in
trading/common, and importable in a plain test process with no database
or web server running.

--------------------------------------------------------------------------
Relationship to the existing trading/api authentication framework
--------------------------------------------------------------------------
This project already has a real, production authentication system:
trading/api/security/tokens.py (JWT access tokens),
trading/database/models.py::User (username/password/role/extra_permissions),
trading/api/security/permissions.py (the `Permission` enum: VIEW, START,
STOP, RESTART, TRADING_CONTROL, ADMIN and its ROLE_PERMISSIONS bundles),
and trading/api/deps.py::Principal (the resolved-caller object).

This module does NOT replace or modify any of that. `LivePermission`
below is a deliberately separate, narrower vocabulary scoped to exactly
the live-order-authorization actions this phase (and Phase 15D.5/15D.6)
cares about -- extending the production `Permission` enum with
trading-specific names would widen a safety-focused phase's blast radius
into unrelated, already-shipped control-center API surface for no safety
benefit. `JwtClaimsAuthenticationProvider` below is the seam that lets a
future `trading/api` route bridge an already-authenticated `Principal`
into an `OperatorIdentity` -- see this phase's own
docs/phase-15d-7-implementation-plan.md section 7 for the exact
integration path.

--------------------------------------------------------------------------
Core principle -- never trust a caller-supplied name as proof of anything
--------------------------------------------------------------------------
An `OperatorIdentity` is only ever produced by an `AuthenticationProvider`
resolving *something the caller does not control the meaning of* (a
session token, decoded JWT claims) -- never accepted as a plain string
argument from a caller claiming to be someone. `LiveAuthorizationWorkflow`
(Phase 15D.6, extended in this phase) requires an `OperatorIdentity`
object, not a name, everywhere it used to accept `authorized_by` as free
text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


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


_VIEWER_PERMISSIONS: frozenset[LivePermission] = frozenset({
    LivePermission.READ_MARKET_DATA, LivePermission.READ_ACCOUNT,
})
_TRADER_PERMISSIONS: frozenset[LivePermission] = _VIEWER_PERMISSIONS | frozenset({
    LivePermission.REQUEST_LIVE_AUTHORIZATION, LivePermission.CONFIRM_LIVE_ACTION,
    LivePermission.EXECUTE_LIVE_ACTION,
})
_ADMIN_PERMISSIONS: frozenset[LivePermission] = frozenset(LivePermission)  # every permission, including KILL_TRADING

ROLE_PERMISSIONS: dict[OperatorRole, frozenset[LivePermission]] = {
    OperatorRole.VIEWER: _VIEWER_PERMISSIONS,
    OperatorRole.TRADER: _TRADER_PERMISSIONS,
    OperatorRole.ADMIN: _ADMIN_PERMISSIONS,
}


def permissions_for_role(role: OperatorRole, extra: frozenset[LivePermission] | None = None) -> frozenset[LivePermission]:
    """Mirrors trading/api/security/permissions.py::permissions_for()'s
    shape (role bundle + optional per-identity extra grants) without
    importing anything from trading/api."""
    return ROLE_PERMISSIONS.get(role, frozenset()) | (extra or frozenset())


@dataclass(frozen=True)
class OperatorIdentity:
    """A resolved, authenticated (or explicitly not-authenticated)
    operator. Never constructed directly from caller-supplied free text --
    always the return value of an AuthenticationProvider."""

    operator_id: str  # stable id, e.g. "user:42" -- never a credential, never a broker account id
    display_name: str
    role: OperatorRole
    auth_state: AuthState
    authentication_source: str  # e.g. "jwt", "fake-test", future "sso:okta"
    permissions: frozenset[LivePermission] = field(default_factory=frozenset)
    is_active: bool = True

    @property
    def is_authenticated(self) -> bool:
        return self.auth_state == AuthState.AUTHENTICATED and self.is_active

    def has(self, permission: LivePermission) -> bool:
        return self.is_authenticated and permission in self.permissions


def unauthenticated(reason: str = "") -> OperatorIdentity:
    """The canonical UNAUTHENTICATED identity -- returned by an
    AuthenticationProvider for a missing/unknown credential. Never raises;
    callers must check `is_authenticated` (this mirrors the brief's
    AuthenticationProvider contract distinguishing AUTHENTICATED /
    UNAUTHENTICATED / INVALID rather than throwing for the common
    not-logged-in case)."""
    return OperatorIdentity(
        operator_id="", display_name=reason or "unauthenticated", role=OperatorRole.VIEWER,
        auth_state=AuthState.UNAUTHENTICATED, authentication_source="", permissions=frozenset(), is_active=False,
    )


def invalid_identity(reason: str = "") -> OperatorIdentity:
    """A malformed/corrupt credential (as opposed to simply absent) --
    kept distinct from UNAUTHENTICATED per the brief's own three-state
    requirement, even though both fail every permission check identically
    (`is_authenticated` is False for both)."""
    return OperatorIdentity(
        operator_id="", display_name=reason or "invalid", role=OperatorRole.VIEWER,
        auth_state=AuthState.INVALID, authentication_source="", permissions=frozenset(), is_active=False,
    )


class AuthenticationProvider(Protocol):
    """Resolves whatever the caller presents (an opaque token, decoded JWT
    claims, ...) into an OperatorIdentity. Must never raise for a missing
    or malformed credential -- return `unauthenticated()`/`invalid_identity()`
    instead, so every caller has one uniform way to fail closed."""

    def resolve_operator(self, credential: Any) -> OperatorIdentity: ...


class FakeAuthenticationProvider:
    """In-memory AuthenticationProvider for tests and local/manual
    exercise of the workflow. Never used in production -- there is no
    persistence, no expiry, and no verification beyond a dict lookup."""

    def __init__(self) -> None:
        self._by_token: dict[str, OperatorIdentity] = {}

    def register(self, token: str, identity: OperatorIdentity) -> None:
        self._by_token[token] = identity

    def disable(self, token: str) -> None:
        """Simulates an admin deactivating an operator -- the token still
        resolves to a known identity, but `is_active=False` (so
        `is_authenticated` becomes False without discarding who they are,
        matching a real deactivated-user row rather than an unknown one)."""
        existing = self._by_token.get(token)
        if existing is not None:
            self._by_token[token] = OperatorIdentity(
                operator_id=existing.operator_id, display_name=existing.display_name, role=existing.role,
                auth_state=existing.auth_state, authentication_source=existing.authentication_source,
                permissions=existing.permissions, is_active=False,
            )

    def resolve_operator(self, credential: Any) -> OperatorIdentity:
        if credential is None:
            return unauthenticated("no credential presented")
        if not isinstance(credential, str) or not credential:
            return invalid_identity(f"malformed credential: {credential!r}")
        identity = self._by_token.get(credential)
        if identity is None:
            return unauthenticated(f"unknown token {credential!r}")
        return identity


_ROLE_STRING_MAP: dict[str, OperatorRole] = {
    "viewer": OperatorRole.VIEWER,
    "trader": OperatorRole.TRADER,
    # The existing trading/api "operator" role already carries
    # TRADING_CONTROL in that system's own RBAC -- the closest existing
    # analog to live-order authority, so it maps to TRADER here.
    "operator": OperatorRole.TRADER,
    "admin": OperatorRole.ADMIN,
}


def operator_role_from_string(role: str) -> OperatorRole:
    return _ROLE_STRING_MAP.get((role or "").lower(), OperatorRole.VIEWER)


class JwtClaimsAuthenticationProvider:
    """Production integration point (Step 4). Deliberately does NOT import
    FastAPI or SQLAlchemy, and does NOT decode a JWT itself -- the caller
    (a future trading/api route) is responsible for resolving the caller
    via the EXISTING trading.api.security.tokens.decode_access_token() +
    trading.database.models.User lookup exactly as
    trading/api/deps.py::_user_principal_from_bearer already does, and
    handing this provider the resulting plain claims dict. This keeps
    trading/common free of a web-framework dependency while still being a
    real, wireable bridge to the JWT system that already exists, not a
    mock of one.

    Expected claims shape (a subset of what decode_access_token() already
    returns, extended with `is_active` since the JWT itself does not carry
    it -- the caller must supply it from its own User-row lookup, exactly
    as trading/api/deps.py already does to reject a deactivated user):
        {"sub": "42", "username": "samir", "role": "admin",
         "perms": [...], "is_active": True}
    """

    def resolve_operator(self, credential: Any) -> OperatorIdentity:
        if not credential or not isinstance(credential, dict):
            return invalid_identity("claims must be a non-empty dict")
        operator_id = credential.get("sub")
        username = credential.get("username")
        if not operator_id or not username:
            return invalid_identity("claims missing sub/username")
        role = operator_role_from_string(str(credential.get("role", "")))
        is_active = bool(credential.get("is_active", True))
        if not is_active:
            return OperatorIdentity(
                operator_id=f"user:{operator_id}", display_name=str(username), role=role,
                auth_state=AuthState.AUTHENTICATED, authentication_source="jwt",
                permissions=frozenset(), is_active=False,
            )
        return OperatorIdentity(
            operator_id=f"user:{operator_id}", display_name=str(username), role=role,
            auth_state=AuthState.AUTHENTICATED, authentication_source="jwt",
            permissions=permissions_for_role(role), is_active=True,
        )
