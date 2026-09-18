"""
Phase 15D.7 -- the authorization-decision layer between an OperatorIdentity
and the Phase 15D.5/15D.6 LiveAuthorization mechanism.

Evaluates: OperatorIdentity + LivePermission + the exact requested action
(account/broker/credential/instrument/side/quantity) -> AUTHORIZED/DENIED.

This runs IN ADDITION TO, never instead of, every existing gate (central
kill switch, RiskManager, LiveCanaryGuard, idempotency,
LiveAuthorization.try_consume()'s own exact-scope match). Fail-closed on
every branch: an exception, an unknown role, or an unset field is always
a DENIED, never a silent AUTHORIZED.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.common.operator_identity import LivePermission, OperatorIdentity
from trading.common.trading_account import TradingAccount


class AuthorizationOutcome:
    AUTHORIZED = "AUTHORIZED"
    DENIED = "DENIED"


@dataclass(frozen=True)
class AuthorizationDecision:
    outcome: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == AuthorizationOutcome.AUTHORIZED


def _denied(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(outcome=AuthorizationOutcome.DENIED, reason=reason)


_AUTHORIZED = AuthorizationDecision(outcome=AuthorizationOutcome.AUTHORIZED, reason="")


class AuthorizationService:
    """Stateless -- every evaluate() call is a pure function of its
    arguments. No caching, no side effects; callers are responsible for
    auditing the decision (see live_authorization_workflow.py)."""

    def evaluate(
        self, operator: OperatorIdentity, permission: LivePermission, *, account: TradingAccount,
    ) -> AuthorizationDecision:
        try:
            return self._evaluate(operator, permission, account=account)
        except Exception as exc:  # noqa: BLE001 -- fail closed, mirrors RiskManager's own INTERNAL_ERROR pattern
            return _denied(f"internal error during authorization evaluation: {exc}")

    def _evaluate(
        self, operator: OperatorIdentity, permission: LivePermission, *, account: TradingAccount,
    ) -> AuthorizationDecision:
        if operator is None:
            return _denied("no operator identity presented")
        if not operator.is_authenticated:
            return _denied(f"operator is not authenticated (auth_state={operator.auth_state.value})")
        if not operator.is_active:
            return _denied("operator is disabled")
        if not operator.operator_id:
            return _denied("operator has no stable operator_id")
        if not operator.has(permission):
            return _denied(f"operator lacks required permission {permission.value!r}")

        # Account ownership (Phase 15B's owner_id, unused until now). An
        # account with NO declared owner is treated as house/shared --
        # still gated by the permission check above, but not additionally
        # denied by ownership, so every pre-existing account (created
        # before ownership was tracked) isn't silently locked out.
        if account.owner_id and account.owner_id != operator.operator_id:
            return _denied(
                f"operator {operator.operator_id!r} does not own account {account.account_id!r} "
                f"(owner={account.owner_id!r})"
            )

        return _AUTHORIZED
