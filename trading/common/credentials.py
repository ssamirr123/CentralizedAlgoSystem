"""
Per-account credential resolution (Phase 15B section 6/14).

The gap this closes: trading.common.config.BrokerCredentials reads exactly
one fixed set of env vars per broker (ANGELONE_API_KEY, ANGELONE_CLIENT_ID,
...), populated once at TradingConfig() construction. That is fine for a
single account per broker, but cannot represent "ANGEL_SAMIR" and
"ANGEL_WIFE" existing at once -- both would resolve to the same real login.

This module resolves a TradingAccount's own, non-secret
`credential_reference` string into a broker-specific credentials dataclass,
reading a DIFFERENT set of env vars per reference. The default reference
("" or the broker's bare name) reads the exact same env vars
trading.common.config already does today, so every existing account/test
that never sets credential_reference is completely unaffected.

Reference format: "env:<PREFIX>", e.g. "env:ANGELONE" (the legacy/default
Angel One account) or "env:ANGELONE_WIFE" (a second, independent Angel One
account, reading ANGELONE_WIFE_API_KEY / ANGELONE_WIFE_CLIENT_ID / ...).
Only the "env:" scheme is implemented today -- a future secrets-manager
scheme (e.g. "secretsmanager:...") is out of scope for this phase and is
intentionally not stubbed out here (see docs/phase-15b-architecture-plan.md
Section 11: build only what is needed now).

Security: never logs, prints, or returns anything derived from the
credential VALUES themselves except inside the returned dataclass, which
callers must handle with the same care as trading.common.config.BrokerCredentials
(never logged, never included in any repr encountered elsewhere in this
codebase).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from trading.common.broker_types import BrokerType, UnsupportedBrokerError

_DEFAULT_PREFIX: dict[BrokerType, str] = {
    BrokerType.ANGEL_ONE: "ANGELONE",
    BrokerType.DHAN: "DHAN",
    BrokerType.ICICI_BREEZE: "ICICI_BREEZE",
    BrokerType.ZERODHA: "ZERODHA",
}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_alias(*names: str) -> str:
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


@dataclass(frozen=True)
class AccountCredentials:
    """Broker-generic container: every field defaults to empty, and each
    broker adapter reads only the subset it needs (see each broker's own
    `to_*` accessor below). Frozen and never carries a `__repr__` override
    beyond dataclass's default field-by-field repr -- callers must not log
    an instance of this class."""

    broker_type: BrokerType
    prefix: str  # the resolved env-var prefix actually used -- non-secret, safe to log for audit
    api_key: str = ""
    client_id: str = ""
    password: str = ""  # Angel's MPIN/password
    totp_secret: str = ""
    api_secret: str = ""
    access_token: str = ""
    session_token: str = ""


def _prefix_for_reference(broker_type: BrokerType, credential_reference: str) -> str:
    """"" or "env:<PREFIX>" -> <PREFIX>. Anything else is rejected rather
    than silently falling back, so a typo'd reference fails closed instead
    of quietly resolving to the wrong (or default) credentials."""
    if broker_type == BrokerType.PAPER:
        return "PAPER"  # PaperBroker needs no real credentials at all
    if not credential_reference:
        try:
            return _DEFAULT_PREFIX[broker_type]
        except KeyError:
            raise UnsupportedBrokerError(
                f"No default credential prefix for {broker_type!r}."
            ) from None
    if not credential_reference.startswith("env:"):
        raise ValueError(
            f"Unsupported credential_reference scheme {credential_reference!r}. "
            "Only 'env:<PREFIX>' is supported in this phase."
        )
    prefix = credential_reference[len("env:"):].strip()
    if not prefix:
        raise ValueError(f"credential_reference {credential_reference!r} has an empty prefix.")
    return prefix


def resolve_credentials(broker_type: BrokerType, credential_reference: str = "") -> AccountCredentials:
    """The one function every per-account broker construction path should
    call. Deterministic and side-effect-free (only reads os.environ)."""
    prefix = _prefix_for_reference(broker_type, credential_reference)

    if broker_type == BrokerType.ANGEL_ONE:
        return AccountCredentials(
            broker_type=broker_type,
            prefix=prefix,
            api_key=_env(f"{prefix}_API_KEY"),
            client_id=_env(f"{prefix}_CLIENT_ID"),
            password=_env_alias(f"{prefix}_PASSWORD", f"{prefix}_MPIN"),
            totp_secret=_env(f"{prefix}_TOTP_SECRET"),
        )
    if broker_type == BrokerType.DHAN:
        return AccountCredentials(
            broker_type=broker_type,
            prefix=prefix,
            client_id=_env(f"{prefix}_CLIENT_ID"),
            access_token=_env(f"{prefix}_ACCESS_TOKEN"),
        )
    if broker_type == BrokerType.ICICI_BREEZE:
        return AccountCredentials(
            broker_type=broker_type,
            prefix=prefix,
            api_key=_env(f"{prefix}_API_KEY"),
            api_secret=_env(f"{prefix}_API_SECRET"),
            session_token=_env(f"{prefix}_SESSION_TOKEN"),
        )
    if broker_type == BrokerType.ZERODHA:
        return AccountCredentials(
            broker_type=broker_type,
            prefix=prefix,
            api_key=_env(f"{prefix}_API_KEY"),
            api_secret=_env(f"{prefix}_API_SECRET"),
            access_token=_env(f"{prefix}_ACCESS_TOKEN"),
        )
    if broker_type == BrokerType.PAPER:
        return AccountCredentials(broker_type=broker_type, prefix=prefix)

    raise UnsupportedBrokerError(f"resolve_credentials: no handler for {broker_type!r}.")


def is_fully_configured(credentials: AccountCredentials) -> bool:
    """Mirrors each adapter's own existing "all required fields present"
    check (see e.g. AngelOneBroker.connect()), exposed generically so a
    caller can pre-validate an account's credential completeness without
    duplicating per-broker field lists."""
    if credentials.broker_type == BrokerType.ANGEL_ONE:
        return bool(credentials.api_key and credentials.client_id and credentials.password and credentials.totp_secret)
    if credentials.broker_type == BrokerType.DHAN:
        return bool(credentials.client_id and credentials.access_token)
    if credentials.broker_type == BrokerType.ICICI_BREEZE:
        return bool(credentials.api_key and credentials.api_secret and credentials.session_token)
    if credentials.broker_type == BrokerType.ZERODHA:
        return bool(credentials.api_key and credentials.api_secret and credentials.access_token)
    if credentials.broker_type == BrokerType.PAPER:
        return True
    return False
