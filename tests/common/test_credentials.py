"""Phase 15B section 14/17: per-account credential resolution and isolation.

Uses only synthetic, fake env vars -- never real secrets, never asks for
real credentials, per this phase's explicit instructions.
"""
from __future__ import annotations

import pytest

from trading.common.broker_types import BrokerType, UnsupportedBrokerError
from trading.common.credentials import is_fully_configured, resolve_credentials


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(("ANGELONE", "DHAN", "ICICI_BREEZE", "ZERODHA")):
            monkeypatch.delenv(name, raising=False)
    yield


def test_default_reference_reads_the_legacy_env_vars(monkeypatch):
    """credential_reference='' must resolve to EXACTLY the same env vars
    trading.common.config.BrokerCredentials already reads today -- zero
    behavior change for any existing single-account deployment."""
    monkeypatch.setenv("ANGELONE_API_KEY", "legacy-key")
    monkeypatch.setenv("ANGELONE_CLIENT_ID", "legacy-client")
    monkeypatch.setenv("ANGELONE_MPIN", "1234")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "legacy-totp")

    creds = resolve_credentials(BrokerType.ANGEL_ONE, "")
    assert creds.api_key == "legacy-key"
    assert creds.client_id == "legacy-client"
    assert creds.password == "1234"
    assert creds.totp_secret == "legacy-totp"


def test_two_angel_one_accounts_resolve_to_disjoint_credentials(monkeypatch):
    """The core Phase 15B guarantee: ANGEL_SAMIR and ANGEL_WIFE (same
    broker) must never resolve to the same credential values."""
    monkeypatch.setenv("ANGELONE_SAMIR_API_KEY", "samir-key")
    monkeypatch.setenv("ANGELONE_SAMIR_CLIENT_ID", "samir-client")
    monkeypatch.setenv("ANGELONE_SAMIR_MPIN", "1111")
    monkeypatch.setenv("ANGELONE_SAMIR_TOTP_SECRET", "samir-totp")

    monkeypatch.setenv("ANGELONE_WIFE_API_KEY", "wife-key")
    monkeypatch.setenv("ANGELONE_WIFE_CLIENT_ID", "wife-client")
    monkeypatch.setenv("ANGELONE_WIFE_MPIN", "2222")
    monkeypatch.setenv("ANGELONE_WIFE_TOTP_SECRET", "wife-totp")

    samir = resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_SAMIR")
    wife = resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_WIFE")

    assert samir.client_id == "samir-client"
    assert wife.client_id == "wife-client"
    assert samir.client_id != wife.client_id
    assert samir.api_key != wife.api_key
    assert samir.password != wife.password
    assert samir.totp_secret != wife.totp_secret

    # Never accidentally cross-read: wife's reference must never surface
    # samir's values under any field.
    wife_values = {wife.api_key, wife.client_id, wife.password, wife.totp_secret}
    samir_values = {samir.api_key, samir.client_id, samir.password, samir.totp_secret}
    assert wife_values.isdisjoint(samir_values)


def test_unconfigured_second_account_resolves_to_empty_not_a_fallback(monkeypatch):
    """A second account's env vars simply not being set must resolve to
    empty strings -- never silently falling back to the default/legacy
    account's credentials."""
    monkeypatch.setenv("ANGELONE_API_KEY", "legacy-key")
    monkeypatch.setenv("ANGELONE_CLIENT_ID", "legacy-client")
    monkeypatch.setenv("ANGELONE_MPIN", "1234")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "legacy-totp")

    wife = resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_WIFE")
    assert wife.client_id == ""
    assert wife.api_key == ""
    assert is_fully_configured(wife) is False


def test_malformed_reference_scheme_is_rejected():
    with pytest.raises(ValueError):
        resolve_credentials(BrokerType.ANGEL_ONE, "secretsmanager:angel/samir")


def test_empty_prefix_is_rejected():
    with pytest.raises(ValueError):
        resolve_credentials(BrokerType.ANGEL_ONE, "env:")


def test_dhan_and_icici_use_their_own_field_shapes(monkeypatch):
    monkeypatch.setenv("DHAN_SAMIR_CLIENT_ID", "dhan-client")
    monkeypatch.setenv("DHAN_SAMIR_ACCESS_TOKEN", "dhan-token")
    dhan = resolve_credentials(BrokerType.DHAN, "env:DHAN_SAMIR")
    assert dhan.client_id == "dhan-client"
    assert dhan.access_token == "dhan-token"
    assert is_fully_configured(dhan) is True

    monkeypatch.setenv("ICICI_SAMIR_API_KEY", "icici-key")
    monkeypatch.setenv("ICICI_SAMIR_API_SECRET", "icici-secret")
    monkeypatch.setenv("ICICI_SAMIR_SESSION_TOKEN", "icici-session")
    icici = resolve_credentials(BrokerType.ICICI_BREEZE, "env:ICICI_SAMIR")
    assert icici.api_secret == "icici-secret"
    assert is_fully_configured(icici) is True


def test_paper_broker_needs_no_credentials():
    creds = resolve_credentials(BrokerType.PAPER, "")
    assert is_fully_configured(creds) is True


def test_unsupported_broker_type_fails_closed():
    class _FakeBrokerType:
        pass

    with pytest.raises(UnsupportedBrokerError):
        resolve_credentials(_FakeBrokerType(), "")  # type: ignore[arg-type]
