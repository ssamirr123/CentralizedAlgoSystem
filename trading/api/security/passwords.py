"""Password hashing — bcrypt only, no passlib.

bcrypt has a hard 72-byte input limit; longer inputs are silently
truncated by the C library, which would make a long passphrase weaker
than it looks and cause "wrong password" surprises after a change. We
pre-hash with SHA-256 and base64 the digest (44 bytes, < 72) so the full
password always contributes.
"""
from __future__ import annotations

import base64
import hashlib

import bcrypt

_MIN_LEN = 10
_MAX_LEN = 256


class WeakPasswordError(ValueError):
    pass


def _prehash(password: str) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def validate_password_strength(password: str) -> None:
    if not isinstance(password, str) or len(password) < _MIN_LEN:
        raise WeakPasswordError(f"Password must be at least {_MIN_LEN} characters.")
    if len(password) > _MAX_LEN:
        raise WeakPasswordError(f"Password must be at most {_MAX_LEN} characters.")
    classes = [
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ]
    if sum(classes) < 3:
        raise WeakPasswordError(
            "Password must include at least three of: lowercase, uppercase, digit, symbol."
        )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False
