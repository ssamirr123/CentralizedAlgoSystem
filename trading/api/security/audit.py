"""Audit-log writer. One helper, used from routes and deps.

Never raises into the request path: an audit write failing must not take
down the action it is recording (it is logged instead).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from trading.database import models

logger = logging.getLogger("trading.api.audit")

# Actions we record. Not an enum (routes pass literals) but centralised
# here for grep-ability.
AUTH_LOGIN = "AUTH_LOGIN"
AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
AUTH_LOGOUT = "AUTH_LOGOUT"
AUTH_REFRESH = "AUTH_REFRESH"
AUTH_REFRESH_DENIED = "AUTH_REFRESH_DENIED"
AUTH_PASSWORD_CHANGED = "AUTH_PASSWORD_CHANGED"
PERMISSION_DENIED = "PERMISSION_DENIED"
ALGO_START = "ALGO_START"
ALGO_STOP = "ALGO_STOP"
ALGO_RESTART = "ALGO_RESTART"
ALGO_UPDATE = "ALGO_UPDATE"
SERVER_REGISTERED = "SERVER_REGISTERED"
SERVER_UPDATED = "SERVER_UPDATED"
SERVER_DELETED = "SERVER_DELETED"
SERVER_START = "SERVER_START"
SERVER_STOP = "SERVER_STOP"
SERVER_RESTART = "SERVER_RESTART"
ALGO_REGISTERED = "ALGO_REGISTERED"
ALGO_PATCHED = "ALGO_PATCHED"
ALGO_DELETED = "ALGO_DELETED"
USER_CREATED = "USER_CREATED"
USER_UPDATED = "USER_UPDATED"
USER_PASSWORD_RESET = "USER_PASSWORD_RESET"
USER_DEACTIVATED = "USER_DEACTIVATED"


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    actor_label: str | None = None,
    target: str | None = None,
    outcome: str = "success",
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    try:
        db.add(
            models.AuditLog(
                actor=actor,
                actor_label=actor_label,
                action=action,
                target=target,
                outcome=outcome,
                ip=ip,
                user_agent=(user_agent or "")[:255] or None,
                detail=detail,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 -- auditing must never break the request
        logger.exception("audit write failed: action=%s actor=%s", action, actor)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
