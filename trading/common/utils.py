"""
Process management helpers: PID files, graceful shutdown, identification.

Milestone 2's trading-agent will use write_pid_file/read_pid_file/
is_process_running to check whether an algo it started is still alive.
Keeping these here (not agent-specific) so both the algo process itself
and the future controller agree on the same PID file format/location.
"""
from __future__ import annotations

import os
import signal
import socket
import threading
import time
from pathlib import Path
from types import FrameType

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def get_hostname() -> str:
    return socket.gethostname()


def get_pid() -> int:
    return os.getpid()


def pid_file_path(algo_name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{algo_name}.pid"


def write_pid_file(algo_name: str) -> Path:
    path = pid_file_path(algo_name)
    path.write_text(str(get_pid()))
    return path


def read_pid_file(algo_name: str) -> int | None:
    path = pid_file_path(algo_name)
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid_file(algo_name: str) -> None:
    path = pid_file_path(algo_name)
    path.unlink(missing_ok=True)


def stop_flag_path(algo_name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{algo_name}.stop_requested"


def request_stop(algo_name: str) -> None:
    """Signal an algo to shut down gracefully. Cross-platform, no OS signal
    involved — GracefulShutdown polls for this file's existence. Preferred
    over os.kill(pid, SIGTERM/CTRL_BREAK_EVENT) sent from an unrelated
    process, which is unreliable on Windows (console-signal delivery
    requires session/console relationships that don't hold across
    independent process invocations)."""
    stop_flag_path(algo_name).write_text(_now_iso())


def clear_stop_flag(algo_name: str) -> None:
    stop_flag_path(algo_name).unlink(missing_ok=True)


def is_stop_requested(algo_name: str) -> bool:
    return stop_flag_path(algo_name).exists()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def is_process_running(pid: int) -> bool:
    """Check whether a PID is alive. Does not confirm it's the expected algo."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_process_running_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_process_running_windows(pid: int) -> bool:
    # os.kill(pid, 0) is NOT a safe existence check on Windows: signal value
    # 0 is signal.CTRL_C_EVENT, so it actually attempts to deliver Ctrl+C
    # rather than querying existence — and that delivery fails with OSError
    # for processes started in their own process group (as trading_agent.py
    # does), producing false "not running" results for processes that are
    # very much alive. Query the Win32 API directly instead, with no signal
    # sent to the target at all.
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class GracefulShutdown:
    """
    Signals the main loop to exit cleanly (finish current iteration, flush
    state, disconnect the broker) instead of being killed mid-operation.

    Two independent trigger paths, both feeding the same stop condition:
      1. SIGINT/SIGTERM (and SIGBREAK on Windows) — real OS signals, mainly
         useful for interactive Ctrl+C during manual/local runs.
      2. A stop-flag file (trading/data/<algo_name>.stop_requested) —
         cross-process, platform-independent. This is what
         trading_agent.py's STOP_ALGO uses: sending a real OS signal from
         an unrelated process is unreliable on Windows (console-signal
         delivery needs a session/console relationship that separate CLI
         invocations don't share), so the agent writes this file instead
         and the loop polls for it. No such fragility on POSIX either way.

    Pass algo_name to enable the flag-file path; omit it to fall back to
    signal-only behavior (e.g. a script with no agent-managed lifecycle).

    Usage:
        shutdown = GracefulShutdown(algo_name="example_strategy")
        while not shutdown.is_set():
            ...
            shutdown.wait(loop_interval_seconds)
    """

    def __init__(self, algo_name: str | None = None, poll_interval: float = 0.5) -> None:
        self._stop_event = threading.Event()
        self._algo_name = algo_name
        self._poll_interval = poll_interval
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._handle_signal)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        self._stop_event.set()

    def _flag_requested(self) -> bool:
        return self._algo_name is not None and is_stop_requested(self._algo_name)

    def is_set(self) -> bool:
        return self._stop_event.is_set() or self._flag_requested()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until stop is signaled (event or flag file) or timeout
        elapses. Polls in small increments so the flag file is checked
        promptly even when timeout is large — a real signal still wakes it
        immediately via the underlying threading.Event."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._stop_event.is_set():
                return True
            if self._flag_requested():
                self._stop_event.set()
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                step = min(self._poll_interval, remaining)
            else:
                step = self._poll_interval
            if self._stop_event.wait(step):
                return True
