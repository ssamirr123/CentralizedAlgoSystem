"""
Command-line user administration -- for the first admin, or out-of-band
recovery when no admin can log in.

    python -m trading.api.admin_cli create-user  --username alice --role admin
    python -m trading.api.admin_cli list-users
    python -m trading.api.admin_cli set-role     --username alice --role operator
    python -m trading.api.admin_cli reset-password --username alice
    python -m trading.api.admin_cli deactivate   --username alice

Passwords are read interactively (getpass), never from argv.
Runs against DATABASE_URL, same as the app.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from trading.api.security.passwords import WeakPasswordError, hash_password, validate_password_strength
from trading.api.security.permissions import VALID_ROLES
from trading.database import models
from trading.database.connection import SessionLocal, init_db


def _prompt_password() -> str:
    while True:
        p1 = getpass.getpass("New password: ")
        try:
            validate_password_strength(p1)
        except WeakPasswordError as exc:
            print(f"  {exc}", file=sys.stderr)
            continue
        if p1 != getpass.getpass("Confirm password: "):
            print("  passwords did not match", file=sys.stderr)
            continue
        return p1


def cmd_create_user(args: argparse.Namespace) -> int:
    if args.role not in VALID_ROLES:
        print(f"role must be one of {list(VALID_ROLES)}", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        if db.query(models.User).filter(models.User.username == args.username).first():
            print(f"user already exists: {args.username}", file=sys.stderr)
            return 1
        password = _prompt_password()
        db.add(
            models.User(
                username=args.username,
                email=args.email,
                password_hash=hash_password(password),
                role=args.role,
                is_active=True,
                must_change_password=not args.no_force_change,
            )
        )
        db.commit()
        print(f"created {args.username} (role={args.role})")
        return 0
    finally:
        db.close()


def cmd_list_users(_: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        rows = db.query(models.User).order_by(models.User.username).all()
        if not rows:
            print("(no users)")
            return 0
        for u in rows:
            flags = []
            if not u.is_active:
                flags.append("inactive")
            if u.must_change_password:
                flags.append("must-change-pw")
            print(f"{u.id:>3}  {u.username:<24} {u.role:<10} {' '.join(flags)}")
        return 0
    finally:
        db.close()


def cmd_set_role(args: argparse.Namespace) -> int:
    if args.role not in VALID_ROLES:
        print(f"role must be one of {list(VALID_ROLES)}", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        u = db.query(models.User).filter(models.User.username == args.username).one_or_none()
        if u is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1
        u.role = args.role
        db.commit()
        print(f"{args.username} -> role {args.role}")
        return 0
    finally:
        db.close()


def cmd_reset_password(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        u = db.query(models.User).filter(models.User.username == args.username).one_or_none()
        if u is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1
        from datetime import datetime, timezone

        u.password_hash = hash_password(_prompt_password())
        u.must_change_password = not args.no_force_change
        db.query(models.AuthSession).filter(
            models.AuthSession.user_id == u.id, models.AuthSession.revoked_at.is_(None)
        ).update({models.AuthSession.revoked_at: datetime.now(timezone.utc)})
        db.commit()
        print(f"password reset for {args.username}; sessions revoked")
        return 0
    finally:
        db.close()


def cmd_deactivate(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        u = db.query(models.User).filter(models.User.username == args.username).one_or_none()
        if u is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1
        active_admins = (
            db.query(models.User)
            .filter(models.User.role == "admin", models.User.is_active.is_(True), models.User.id != u.id)
            .count()
        )
        if u.role == "admin" and active_admins == 0:
            print("refusing: this is the last active admin", file=sys.stderr)
            return 1
        u.is_active = False
        db.commit()
        print(f"deactivated {args.username}")
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="admin_cli", description="Dashboard user administration")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-user")
    c.add_argument("--username", required=True)
    c.add_argument("--role", default="viewer")
    c.add_argument("--email", default=None)
    c.add_argument("--no-force-change", action="store_true", help="don't require a password change on first login")
    c.set_defaults(func=cmd_create_user)

    c = sub.add_parser("list-users")
    c.set_defaults(func=cmd_list_users)

    c = sub.add_parser("set-role")
    c.add_argument("--username", required=True)
    c.add_argument("--role", required=True)
    c.set_defaults(func=cmd_set_role)

    c = sub.add_parser("reset-password")
    c.add_argument("--username", required=True)
    c.add_argument("--no-force-change", action="store_true")
    c.set_defaults(func=cmd_reset_password)

    c = sub.add_parser("deactivate")
    c.add_argument("--username", required=True)
    c.set_defaults(func=cmd_deactivate)
    return p


def main(argv: list[str] | None = None) -> int:
    init_db()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
