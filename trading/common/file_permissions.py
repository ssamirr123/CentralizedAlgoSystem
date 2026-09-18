"""
Phase 15D.8 -- best-effort file-permission hardening (owner-read-write
only, 0600) for the safety-critical persistence files this project
writes: kill-switch JSON, and the idempotency/audit/live-authorization/
reconciliation SQLite databases.

POSIX only (this project's production target is Linux/EC2 -- see
trading/infrastructure/backend/systemd/*). A silent no-op on Windows,
where NTFS ACLs don't map to POSIX mode bits and this is dev-only
territory anyway. Never raises: a permission-hardening failure must
never block startup or any store's own read/write operation -- it only
reduces this one defense-in-depth layer, silently, exactly like an
audit-write failure never blocks the action it records.
"""
from __future__ import annotations

import os
import stat

_OWNER_READ_WRITE = stat.S_IRUSR | stat.S_IWUSR  # 0o600


def harden_file_permissions(path: str) -> bool:
    """Best-effort chmod 0600 on `path`. Returns True only when the file
    exists and its mode is now exactly 0600; False for every other case
    (file doesn't exist yet, non-POSIX platform, or any OS error) --
    never raises."""
    if os.name != "posix":
        return False
    if not path:
        return False
    try:
        if not os.path.exists(path):
            return False
        os.chmod(path, _OWNER_READ_WRITE)
        return (os.stat(path).st_mode & 0o777) == 0o600
    except OSError:
        return False
