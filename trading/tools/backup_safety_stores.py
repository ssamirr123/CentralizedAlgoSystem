"""
Phase 15D.8 -- explicit, on-demand snapshot/restore tooling for the
safety-critical persistence files (kill-switch JSON, and the
idempotency/audit/live-authorization/reconciliation SQLite databases).

Deliberately NOT scheduled or run automatically by this phase -- see
docs/phase-15d-8-production-hardening-report.md's "in scope" list: this
is a script an operator runs (by hand, or later wires to cron/a systemd
timer as a separate, explicit deployment decision), not a background
job this phase enables silently.

Usage:
    python -m trading.tools.backup_safety_stores snapshot <path> [<path> ...] --out-dir <dir>
    python -m trading.tools.backup_safety_stores restore <snapshot_path> <restore_to_path>

`snapshot` uses SQLite's own online backup API (`sqlite3.Connection.backup()`)
for `.db` files -- safe to run against a live, in-use database (unlike a
plain file copy, which can capture a torn write mid-transaction) -- and a
plain, atomic-rename file copy for anything else (e.g., the kill-switch
JSON file). `restore` is a byte-for-byte copy back, plus a read-back
verification pass appropriate to the file's own type.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _is_sqlite_db(path: Path) -> bool:
    return path.suffix == ".db"


def snapshot_one(source_path: str, out_dir: str, *, now: datetime | None = None) -> str:
    """Snapshot one file to `out_dir`, timestamped. Returns the snapshot
    path. Raises FileNotFoundError if `source_path` does not exist --
    callers (e.g. an operator's cron job) should treat that as a real
    error, not silently skip it."""
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(f"snapshot source does not exist: {src}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    dest = out / f"{src.stem}.{stamp}{src.suffix}"

    if _is_sqlite_db(src):
        src_conn = sqlite3.connect(str(src))
        try:
            dest_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    else:
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copy2(src, tmp_dest)
        tmp_dest.replace(dest)  # atomic on the same filesystem

    return str(dest)


def verify_snapshot(snapshot_path: str) -> tuple[bool, str]:
    """Read-back verification: for a SQLite snapshot, run `PRAGMA
    integrity_check`; for anything else, confirm the file is non-empty
    and readable. Never raises -- returns (ok, detail)."""
    path = Path(snapshot_path)
    if not path.is_file():
        return False, "snapshot file does not exist"
    if _is_sqlite_db(path):
        try:
            conn = sqlite3.connect(str(path))
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
            ok = bool(result and result[0] == "ok")
            return ok, (result[0] if result else "no result")
        except sqlite3.Error as exc:
            return False, f"sqlite error: {exc}"
    try:
        return path.stat().st_size > 0, "non-empty file"
    except OSError as exc:
        return False, f"stat failed: {exc}"


def restore_one(snapshot_path: str, restore_to_path: str) -> tuple[bool, str]:
    """Restore `snapshot_path` to `restore_to_path`, verifying the
    snapshot first (refuses to restore a corrupt snapshot) and the
    restored copy afterward. Returns (ok, detail); never raises."""
    ok, detail = verify_snapshot(snapshot_path)
    if not ok:
        return False, f"refusing to restore -- snapshot failed verification: {detail}"
    dest = Path(restore_to_path)
    tmp_dest = dest.with_suffix(dest.suffix + ".restoring")
    try:
        shutil.copy2(snapshot_path, tmp_dest)
        tmp_dest.replace(dest)  # atomic on the same filesystem
    except OSError as exc:
        return False, f"restore copy failed: {exc}"
    ok, detail = verify_snapshot(str(dest))
    return ok, detail


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap_parser = sub.add_parser("snapshot")
    snap_parser.add_argument("paths", nargs="+")
    snap_parser.add_argument("--out-dir", required=True)

    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("snapshot_path")
    restore_parser.add_argument("restore_to_path")

    args = parser.parse_args(argv)

    if args.command == "snapshot":
        exit_code = 0
        for path in args.paths:
            try:
                dest = snapshot_one(path, args.out_dir)
                ok, detail = verify_snapshot(dest)
                print(f"snapshot {path} -> {dest} (verify: {'ok' if ok else 'FAILED: ' + detail})")
                if not ok:
                    exit_code = 1
            except FileNotFoundError as exc:
                print(f"snapshot {path} -> SKIPPED: {exc}")
        return exit_code

    ok, detail = restore_one(args.snapshot_path, args.restore_to_path)
    print(f"restore {args.snapshot_path} -> {args.restore_to_path}: {'ok' if ok else 'FAILED: ' + detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
