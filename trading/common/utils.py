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
    Installs SIGINT/SIGTERM handlers that set a threading.Event instead of
    killing the process immediately, so the main loop can finish its current
    iteration, flush state, and disconnect the broker cleanly.

    Usage:
        shutdown = GracefulShutdown()
        while not shutdown.is_set():
            ...
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        # Windows has no real SIGTERM delivery (os.kill(pid, SIGTERM) hard-
        # terminates instead) — CTRL_BREAK_EVENT + SIGBREAK is the closest
        # equivalent for a clean local-dev shutdown path on that platform.
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._handle_signal)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        self._stop_event.set()

    def is_set(self) -> bool:
        return self._stop_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._stop_event.wait(timeout)
