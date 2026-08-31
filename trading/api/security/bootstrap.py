"""One-time admin bootstrap.

If AUTH_BOOTSTRAP_ADMIN_USERNAME and AUTH_BOOTSTRAP_ADMIN_PASSWORD are
both set AND the users table is empty, create a single admin user with
must_change_password=True. Idempotent: does nothing once any user exists.

This lets a fresh deploy come up with exactly one way in, without ever
putting a password hash in source or a plaintext secret anywhere but the
process environment for the first boot. Ongoing user management is via
POST /api/admin/users or the CLI (python -m trading.api.admin_cli).
"""
from __future__ import annotations

import logging

from trading.api.security.passwords import WeakPasswordError, hash_password, validate_password_strength
from trading.core.config import load_settings
from trading.database import models
from trading.database.connection import SessionLocal

logger = logging.getLogger("trading.api.auth")


def bootstrap_admin() -> None:
    settings = load_settings()
    username = settings.auth_bootstrap_admin_username
    password = settings.auth_bootstrap_admin_password
    if not username or not password:
        return

    db = SessionLocal()
    try:
        if db.query(models.User.id).first() is not None:
            return  # users already exist -- never touch them
        try:
            validate_password_strength(password)
        except WeakPasswordError as exc:
            logger.error("AUTH_BOOTSTRAP_ADMIN_PASSWORD rejected: %s -- no admin created", exc)
            return
        db.add(
            models.User(
                username=username,
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
                must_change_password=True,
            )
        )
        db.commit()
        logger.warning(
            "Bootstrap admin '%s' created (must change password on first login). "
            "Unset AUTH_BOOTSTRAP_ADMIN_* now.",
            username,
        )
    finally:
        db.close()
