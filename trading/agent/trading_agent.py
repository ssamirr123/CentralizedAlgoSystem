"""
Trading Agent — controls algo processes on this EC2 instance.

Each invocation is a short-lived CLI command (this is what Lambda will
eventually invoke via SSM SendCommand in Milestone 3), not a long-running
daemon. Consecutive invocations agree on state via:

  - trading/data/<algo>.pid           written/removed by the algo process
                                       itself (see Milestone 1's main.py) —
                                       authoritative "is it alive right now"
  - trading/data/<algo>.state.json    written by this agent — started_at /
                                       stopped_at / last_command metadata
                                       that survives after the process exits

STATUS always re-checks actual OS process liveness (os.kill(pid, 0)) rather
than trusting stored state — a stored "RUNNING" flag means nothing if the
process crashed since the last check. This is the same principle as
"never report RUNNING just because a command was accepted" (Milestone 4+
applies it to SSM/Lambda; here it applies to the state file).

NOT implemented here (explicit scope boundary for Milestone 2):
  - SAFE_STOP position-checking before stopping a strategy — that needs
    strategy/broker-specific position queries. stop_algo() sends a signal
    and verifies the OS process exits; it does not know or check whether
    the strategy had an open position. Wire this in when a real strategy
    exists to check against.
  - last_heartbeat in STATUS output — the existing backend/dashboard
    already tracks heartbeats independently; wiring STATUS to query it is
    deferred to keep this milestone to process control only.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from trading.common.utils import (
    clear_stop_flag,
    is_process_running,
    read_pid_file,
    remove_pid_file,
    request_stop,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALGOS_DIR = PROJECT_ROOT / "trading" / "algos"
DATA_DIR = PROJECT_ROOT / "trading" / "data"
LOGS_DIR = PROJECT_ROOT / "trading" / "logs"

STOP_GRACE_TIMEOUT_SECONDS = 10.0
START_CRASH_CHECK_SECONDS = 2.0

IS_WINDOWS = os.name == "nt"


class AlgoNotFoundError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _algo_main_path(algo_name: str) -> Path:
    path = ALGOS_DIR / algo_name / "main.py"
    if not path.is_file():
        raise AlgoNotFoundError(
            f"No algo named '{algo_name}' (expected {path} to exist)."
        )
    return path


def _state_file_path(algo_name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{algo_name}.state.json"


def _read_state(algo_name: str) -> dict:
    path = _state_file_path(algo_name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}


def _write_state(algo_name: str, **fields) -> dict:
    state = _read_state(algo_name)
    state.update(fields)
    _state_file_path(algo_name).write_text(json.dumps(state))
    return state


def _launch_log_path(algo_name: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{algo_name}.agent-launch.log"


def _algo_log_path(algo_name: str) -> Path:
    return LOGS_DIR / f"{algo_name}.log"


def _current_pid_if_alive(algo_name: str) -> int | None:
    pid = read_pid_file(algo_name)
    if pid is not None and is_process_running(pid):
        return pid
    return None


@contextmanager
def _start_lock(algo_name: str):
    """Cross-process exclusive lock around the 'check-then-launch' section
    of start_algo, so two concurrent START_ALGO callers (e.g. an explicit
    RESTART racing the box's crash-recovery watchdog) can never both get
    past the 'already running?' check and spawn a duplicate process.
    POSIX only -- on Windows this is a no-op (the original behavior)."""
    if os.name == "nt":
        yield
        return
    import fcntl

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_DIR / f"{algo_name}.start.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def start_algo(algo_name: str) -> dict:
    _algo_main_path(algo_name)  # raises AlgoNotFoundError if missing

    with _start_lock(algo_name):
        return _start_algo_locked(algo_name)


def _start_algo_locked(algo_name: str) -> dict:
    existing_pid = _current_pid_if_alive(algo_name)
    if existing_pid is not None:
        state = _read_state(algo_name)
        return {
            "algo": algo_name,
            "status": "RUNNING",
            "pid": existing_pid,
            "started_at": state.get("started_at"),
            "message": "already running — did not start a duplicate process",
        }

    cmd = [sys.executable, str(_algo_main_path(algo_name))]
    launch_log = _launch_log_path(algo_name)

    popen_kwargs: dict = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    with open(launch_log, "a") as f:
        f.write(f"\n--- launch attempt at {_now_iso()} ---\n")
        f.flush()
        proc = subprocess.Popen(cmd, stdout=f, **popen_kwargs)

    # Wait for the algo to write its own PID file (main.py does this via
    # write_pid_file() right after startup) rather than trusting proc.pid.
    # On Windows, python.exe launched from a venv is a launcher stub that
    # spawns the real interpreter as a child with a DIFFERENT pid — proc.pid
    # is the stub's pid, not the actual algo process's. The self-reported
    # PID is authoritative on every platform; this also naturally doubles
    # as the crash-on-startup check (import errors, bad config, etc.) since
    # a crashed process never gets far enough to write its PID file.
    deadline = time.monotonic() + START_CRASH_CHECK_SECONDS
    child_pid: int | None = None
    while time.monotonic() < deadline:
        child_pid = read_pid_file(algo_name)
        if child_pid is not None and is_process_running(child_pid):
            break
        child_pid = None
        if proc.poll() is not None:
            break  # launcher/process exited without ever writing a PID file
        time.sleep(0.2)

    if child_pid is None:
        tail = _tail_file(launch_log, 20)
        _write_state(
            algo_name,
            status="ERROR",
            pid=None,
            started_at=None,
            last_command="START_ALGO",
            last_error="process exited immediately after launch",
        )
        return {
            "algo": algo_name,
            "status": "ERROR",
            "pid": None,
            "message": "process exited immediately after launch",
            "log_tail": tail,
        }

    started_at = _now_iso()
    _write_state(
        algo_name,
        status="RUNNING",
        pid=child_pid,
        started_at=started_at,
        last_command="START_ALGO",
        last_error=None,
    )
    return {
        "algo": algo_name,
        "status": "RUNNING",
        "pid": child_pid,
        "started_at": started_at,
    }


def stop_algo(algo_name: str, timeout: float = STOP_GRACE_TIMEOUT_SECONDS) -> dict:
    pid = read_pid_file(algo_name)
    if pid is None or not is_process_running(pid):
        remove_pid_file(algo_name)
        _write_state(algo_name, status="STOPPED", pid=None, last_command="STOP_ALGO", stopped_at=_now_iso())
        return {"algo": algo_name, "status": "STOPPED", "pid": None, "message": "was not running"}

    _write_state(algo_name, status="STOPPING", last_command="STOP_ALGO")

    # Cross-platform, cross-process graceful stop: write a flag file that
    # GracefulShutdown polls for (see trading/common/utils.py). Deliberately
    # NOT an OS signal (os.kill(pid, SIGTERM/CTRL_BREAK_EVENT)) — verified
    # unreliable on Windows when sent from an unrelated process (this CLI
    # invocation isn't the algo's parent and doesn't share its console),
    # which silently degraded every stop to a slow force-kill.
    request_stop(algo_name)

    deadline = time.monotonic() + timeout
    forced = False
    while is_process_running(pid) and time.monotonic() < deadline:
        time.sleep(0.5)

    if is_process_running(pid):
        # Still alive after the grace period — either it's genuinely stuck
        # (blocked in a long synchronous call, broker hang, etc.) or never
        # got a chance to check the flag. Force-kill as the safety net so
        # STOP_ALGO always actually stops something, even if not gracefully.
        forced = True
        try:
            if IS_WINDOWS:
                os.kill(pid, signal.SIGTERM)  # hard-terminates on Windows
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        deadline = time.monotonic() + 5.0
        while is_process_running(pid) and time.monotonic() < deadline:
            time.sleep(0.2)

    remove_pid_file(algo_name)
    clear_stop_flag(algo_name)
    stopped_at = _now_iso()
    _write_state(algo_name, status="STOPPED", pid=None, last_command="STOP_ALGO", stopped_at=stopped_at)

    return {
        "algo": algo_name,
        "status": "STOPPED",
        "pid": None,
        "graceful": not forced,
        "forced": forced,
        "message": "force-killed after grace timeout" if forced else "stopped gracefully",
    }


def restart_algo(algo_name: str) -> dict:
    stop_result = stop_algo(algo_name)
    start_result = start_algo(algo_name)
    return {
        "algo": algo_name,
        "stop": stop_result,
        "start": start_result,
        "status": start_result["status"],
    }


def status(algo_name: str) -> dict:
    _algo_main_path(algo_name)  # raises AlgoNotFoundError if the algo itself is unknown

    state = _read_state(algo_name)
    pid = read_pid_file(algo_name)

    if pid is not None and is_process_running(pid):
        if state.get("status") != "RUNNING" or state.get("pid") != pid:
            state = _write_state(algo_name, status="RUNNING", pid=pid)
        return {
            "algo": algo_name,
            "status": "RUNNING",
            "pid": pid,
            "started_at": state.get("started_at"),
            "last_heartbeat": None,  # tracked by the monitoring backend, not this agent
        }

    if pid is not None:
        # PID file exists but the process is dead — crashed without its own
        # cleanup running (e.g. killed -9, OOM). Treat as ERROR, not STOPPED,
        # and clear the stale PID file so future checks aren't fooled by it.
        remove_pid_file(algo_name)
        state = _write_state(algo_name, status="ERROR", pid=None, last_error="process not found (crashed?)")
        return {
            "algo": algo_name,
            "status": "ERROR",
            "pid": None,
            "started_at": state.get("started_at"),
            "message": "PID file existed but process is not running",
        }

    # No PID file at all — either never started, or cleanly stopped. If the
    # state file still claims RUNNING (stale — e.g. edited/left over from a
    # crash before this function's own self-healing above ever ran), that's
    # wrong by definition without a live PID, so report STOPPED instead.
    last_status = state.get("status", "STOPPED")
    if last_status == "RUNNING":
        last_status = "STOPPED"

    return {
        "algo": algo_name,
        "status": last_status,
        "pid": None,
        "started_at": state.get("started_at"),
        "last_heartbeat": None,
    }


def update_algo(algo_name: str) -> dict:
    _algo_main_path(algo_name)

    pid = read_pid_file(algo_name)
    if pid is not None and is_process_running(pid):
        return {
            "algo": algo_name,
            "updated": False,
            "message": "refusing to update while RUNNING — stop the algo first",
        }

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    try:
        previous_version = _git("rev-parse", "HEAD")
        _git("pull", "--ff-only")
        new_version = _git("rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:
        return {
            "algo": algo_name,
            "updated": False,
            "message": f"git update failed: {exc.stderr.strip() if exc.stderr else exc}",
        }

    _write_state(algo_name, last_command="UPDATE", last_update_at=_now_iso(), version=new_version)

    return {
        "algo": algo_name,
        "updated": previous_version != new_version,
        "previous_version": previous_version,
        "new_version": new_version,
    }


def _tail_file(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with open(path, "r", errors="replace") as f:
        return [line.rstrip("\n") for line in f.readlines()[-lines:]]


def get_logs(algo_name: str, lines: int = 100) -> dict:
    _algo_main_path(algo_name)
    return {
        "algo": algo_name,
        "log_file": str(_algo_log_path(algo_name)),
        "lines": _tail_file(_algo_log_path(algo_name), lines),
    }


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Trading Agent — control algo processes on this host.")
    parser.add_argument(
        "command",
        choices=["START_ALGO", "STOP_ALGO", "RESTART_ALGO", "STATUS", "UPDATE", "LOGS"],
    )
    parser.add_argument("algo_name")
    parser.add_argument("--lines", type=int, default=100, help="LOGS: number of trailing lines to return")
    args = parser.parse_args()

    try:
        if args.command == "START_ALGO":
            result = start_algo(args.algo_name)
        elif args.command == "STOP_ALGO":
            result = stop_algo(args.algo_name)
        elif args.command == "RESTART_ALGO":
            result = restart_algo(args.algo_name)
        elif args.command == "STATUS":
            result = status(args.algo_name)
        elif args.command == "UPDATE":
            result = update_algo(args.algo_name)
        elif args.command == "LOGS":
            result = get_logs(args.algo_name, args.lines)
        else:  # unreachable — argparse choices already constrains this
            raise ValueError(f"Unknown command: {args.command}")
    except AlgoNotFoundError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    print(json.dumps(result, default=str))
    return 0 if result.get("status") != "ERROR" else 1


if __name__ == "__main__":
    sys.exit(_cli())
