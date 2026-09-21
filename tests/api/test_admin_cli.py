"""TCC administrative access recovery: trading/api/admin_cli.py's
reset-password command. Covers the offline recovery mechanism used when
no operator can authenticate through the normal /api/auth/login path --
must reuse the app's own hashing/policy, touch only the password
credential of an EXISTING user, revoke outstanding sessions, and leave an
auditable trail without ever persisting or printing a secret."""
from __future__ import annotations

import argparse

import pytest

from trading.api import admin_cli
from trading.api.security.passwords import verify_password
from trading.database import models

OLD_PW = "Old-Password-123!"
NEW_PW = "New-Password-456!"


@pytest.fixture
def existing_admin(db_session):
    from trading.api.security.passwords import hash_password

    u = models.User(username="admin", password_hash=hash_password(OLD_PW), role="admin")
    db_session.add(u)
    db_session.commit()
    return u


def _args(username: str, no_force_change: bool = False) -> argparse.Namespace:
    return argparse.Namespace(username=username, no_force_change=no_force_change)


def _run(monkeypatch, username: str, *, confirm: str = "RESET", new_pw: str = NEW_PW, no_force_change: bool = False) -> int:
    inputs = iter([confirm])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(inputs))
    pw_inputs = iter([new_pw, new_pw])
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: next(pw_inputs))
    return admin_cli.cmd_reset_password(_args(username, no_force_change=no_force_change))


def test_existing_user_reset_succeeds(monkeypatch, db_session, existing_admin):
    rc = _run(monkeypatch, "admin")
    assert rc == 0
    db_session.refresh(existing_admin)
    assert verify_password(NEW_PW, existing_admin.password_hash)


def test_unknown_user_rejected(monkeypatch, db_session):
    rc = _run(monkeypatch, "nobody")
    assert rc == 1
    assert db_session.query(models.User).filter(models.User.username == "nobody").first() is None


def test_password_policy_enforced(monkeypatch, db_session, existing_admin):
    # A weak new password loops in _prompt_password() until a strong one is
    # given -- feed one weak attempt then a strong one.
    inputs = iter(["RESET"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(inputs))
    pw_inputs = iter(["weak", "weak", NEW_PW, NEW_PW])
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: next(pw_inputs))
    rc = admin_cli.cmd_reset_password(_args("admin"))
    assert rc == 0
    db_session.refresh(existing_admin)
    assert verify_password(NEW_PW, existing_admin.password_hash)


def test_password_confirmation_mismatch_reprompts(monkeypatch, db_session, existing_admin):
    inputs = iter(["RESET"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(inputs))
    # First confirmation attempt mismatches; loop re-prompts until it matches.
    pw_inputs = iter([NEW_PW, "different-confirm", NEW_PW, NEW_PW])
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: next(pw_inputs))
    rc = admin_cli.cmd_reset_password(_args("admin"))
    assert rc == 0
    db_session.refresh(existing_admin)
    assert verify_password(NEW_PW, existing_admin.password_hash)


def test_confirmation_abort_makes_no_change(monkeypatch, db_session, existing_admin):
    rc = _run(monkeypatch, "admin", confirm="yes please")
    assert rc == 1
    db_session.refresh(existing_admin)
    assert verify_password(OLD_PW, existing_admin.password_hash)


def test_role_permissions_username_id_unchanged(monkeypatch, db_session, existing_admin):
    original_id, original_username, original_role = existing_admin.id, existing_admin.username, existing_admin.role
    rc = _run(monkeypatch, "admin")
    assert rc == 0
    db_session.refresh(existing_admin)
    assert existing_admin.id == original_id
    assert existing_admin.username == original_username
    assert existing_admin.role == original_role


def test_password_hash_changed_old_rejected_new_accepted(monkeypatch, db_session, existing_admin):
    rc = _run(monkeypatch, "admin")
    assert rc == 0
    db_session.refresh(existing_admin)
    assert not verify_password(OLD_PW, existing_admin.password_hash)
    assert verify_password(NEW_PW, existing_admin.password_hash)


def test_sessions_invalidated(monkeypatch, db_session, existing_admin):
    from datetime import datetime, timedelta, timezone

    session = models.AuthSession(
        user_id=existing_admin.id, token_hash="deadbeef" * 8, csrf_token="csrf-token-value",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    rc = _run(monkeypatch, "admin")
    assert rc == 0
    db_session.refresh(session)
    assert session.revoked_at is not None


def test_audit_event_generated(monkeypatch, db_session, existing_admin):
    from trading.api.security import audit

    rc = _run(monkeypatch, "admin")
    assert rc == 0
    row = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == audit.USER_PASSWORD_RESET)
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.actor == admin_cli.CLI_ACTOR
    assert row.target == f"user:{existing_admin.id}"
    assert row.detail["target_username"] == "admin"


def test_password_and_hash_never_logged(monkeypatch, db_session, existing_admin, caplog):
    rc = _run(monkeypatch, "admin")
    assert rc == 0
    db_session.refresh(existing_admin)
    log_text = caplog.text
    assert OLD_PW not in log_text
    assert NEW_PW not in log_text
    assert existing_admin.password_hash not in log_text


def test_reset_password_is_transactional_on_failure(monkeypatch, db_session, existing_admin):
    """If the commit itself fails, no partial state (new hash without
    session revocation, or vice versa) may be left behind."""
    inputs = iter(["RESET"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(inputs))
    pw_inputs = iter([NEW_PW, NEW_PW])
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: next(pw_inputs))

    real_session = admin_cli.SessionLocal()

    class _BoomSession:
        def __getattr__(self, name):
            return getattr(real_session, name)

        def commit(self):
            raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(admin_cli, "SessionLocal", lambda: _BoomSession())
    with pytest.raises(RuntimeError):
        admin_cli.cmd_reset_password(_args("admin"))

    db_session.refresh(existing_admin)
    assert verify_password(OLD_PW, existing_admin.password_hash)
