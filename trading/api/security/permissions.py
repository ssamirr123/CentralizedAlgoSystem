"""The permission model.

Six capabilities, exactly as the Stage 18 brief names them. Trading-
process control is split so a role can be granted START without STOP, etc.
TRADING_CONTROL covers the heavier operations (code deploy / UPDATE, EC2
power, registering or deleting servers and algos). ADMIN covers user
administration and reading the audit log.
"""
from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    VIEW = "VIEW"
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    TRADING_CONTROL = "TRADING_CONTROL"
    ADMIN = "ADMIN"


# Roles are fixed bundles. A brand-new user is a viewer.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset({Permission.VIEW}),
    "trader": frozenset({Permission.VIEW, Permission.START, Permission.STOP, Permission.RESTART}),
    "operator": frozenset(
        {
            Permission.VIEW,
            Permission.START,
            Permission.STOP,
            Permission.RESTART,
            Permission.TRADING_CONTROL,
        }
    ),
    "admin": frozenset(Permission),  # every permission
}

VALID_ROLES = tuple(ROLE_PERMISSIONS)
DEFAULT_ROLE = "viewer"

# The one machine identity (CONTROL_API_KEY). It may ingest telemetry and
# read, nothing else -- it can never start/stop/restart a process or
# touch administration, no matter how the key leaks.
SERVICE_PERMISSIONS: frozenset[Permission] = frozenset({Permission.VIEW})


def permissions_for(role: str, extra: list[str] | None = None) -> frozenset[Permission]:
    perms = set(ROLE_PERMISSIONS.get(role, frozenset()))
    for name in extra or []:
        try:
            perms.add(Permission(name))
        except ValueError:
            continue
    return frozenset(perms)


def normalize_extra_permissions(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        try:
            out.append(Permission(v).value)
        except ValueError as exc:
            raise ValueError(f"Unknown permission: {v!r}") from exc
    return sorted(set(out))
