"""Stage 18 security primitives: password hashing, JWT access tokens,
opaque refresh tokens, the permission model, and the audit-log writer.

Kept deliberately small and dependency-light (bcrypt + PyJWT only) so it
is easy to audit.
"""
