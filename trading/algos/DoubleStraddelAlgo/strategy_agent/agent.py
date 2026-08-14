"""
StrategyHeartbeatAgent
======================

A tiny, dependency-light heartbeat client for the Central Strategy Monitoring
System.

Design goals (in priority order):
    1. It must **never** affect / block / crash the trading strategy.
       - All network I/O happens on a *daemon* thread.
       - Every failure is swallowed and printed, never raised to the caller.
    2. It pushes the latest metrics to
          POST <api_base_url>/update_strategy
       - on every explicit `update_metrics(...)` call (immediately), and
       - at least once every `heartbeat_interval_seconds` (default 30s) even if
         nothing changed, so the server can detect a dead strategy.

Typical usage:
    agent = StrategyHeartbeatAgent(
        strategy_name="<YOUR_STRATEGY_NAME>",
        server_name="<YOUR_EC2_OR_SERVER_NAME>",
        api_base_url="http://127.0.0.1:8000",
    ).start()

    agent.update_metrics(mtm=1234.5, pnl=987.0, trade_count=3, status="RUNNING")
    ...
    agent.stop(status="STOPPED")   # clean shutdown
"""

import threading
import time
from datetime import datetime, timezone

try:
    import requests
except Exception:  # requests is optional – if missing the agent silently no-ops.
    requests = None


_VALID_STATUSES = ("RUNNING", "STOPPED", "ERROR")


class StrategyHeartbeatAgent:
    def __init__(
        self,
        strategy_name: str,
        server_name: str,
        api_base_url: str,
        heartbeat_interval_seconds: int = 30,  # keep as 30
        request_timeout_seconds: int = 5,
        max_retries: int = 5,
    ):
        self.strategy_name = strategy_name
        self.server_name = server_name
        # Normalise so we always end up with "<base>/update_strategy".
        self.api_base_url = (api_base_url or "").rstrip("/")
        self.endpoint = self.api_base_url + "/update_strategy"
        self.heartbeat_interval_seconds = int(heartbeat_interval_seconds)
        self.request_timeout_seconds = int(request_timeout_seconds)
        self.max_retries = int(max_retries)

        # Latest known metrics (protected by _lock).
        self._lock = threading.Lock()
        self._metrics = {
            "mtm": 0.0,
            "pnl": 0.0,
            "trade_count": 0,
            "status": "RUNNING",
        }

        # Thread control.
        self._thread = None
        self._stop_event = threading.Event()
        # Set whenever update_metrics is called so the heartbeat thread can
        # push an update *immediately* instead of waiting for the next tick.
        self._wake_event = threading.Event()
        self._started = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def start(self):
        """Start the background heartbeat daemon thread. Safe to call twice."""
        if self._started:
            return self
        self._started = True
        self._thread = threading.Thread(
            target=self._run, name="StrategyHeartbeatAgent", daemon=True
        )
        self._thread.start()
        print(
            f"[HEARTBEAT] agent started -> {self.endpoint} "
            f"(interval={self.heartbeat_interval_seconds}s)"
        )
        # Fire an initial heartbeat right away.
        self._wake_event.set()
        return self

    def update_metrics(self, mtm, pnl, trade_count, status):
        """
        Update the cached metrics and trigger an immediate (async) push.

        This call is **non-blocking**: it only updates an in-memory dict and
        wakes the daemon thread. The network request happens on that thread.
        """
        try:
            status = str(status).upper()
            if status not in _VALID_STATUSES:
                status = "RUNNING"
            with self._lock:
                self._metrics["mtm"] = float(mtm)
                self._metrics["pnl"] = float(pnl)
                self._metrics["trade_count"] = int(trade_count)
                self._metrics["status"] = status
            # Ask the heartbeat thread to send now.
            self._wake_event.set()
        except Exception as e:  # never propagate into the trading loop
            print(f"[HEARTBEAT] update_metrics ignored error: {e}")

    def set_status(self, status):
        """Convenience helper to change only the status field."""
        with self._lock:
            m = dict(self._metrics)
        self.update_metrics(m["mtm"], m["pnl"], m["trade_count"], status)

    def stop(self, status: str = "STOPPED"):
        """
        Send one final metric with the given status and stop the thread.

        Sends synchronously (best-effort, bounded) so the final STOPPED/ERROR
        state is delivered even as the process is exiting.
        """
        try:
            status = str(status).upper()
            if status not in _VALID_STATUSES:
                status = "STOPPED"
            with self._lock:
                self._metrics["status"] = status
                payload = self._build_payload_locked()
            self._post(payload)
        except Exception as e:
            print(f"[HEARTBEAT] stop() ignored error: {e}")
        finally:
            self._stop_event.set()
            self._wake_event.set()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _build_payload_locked(self):
        """Build the request body. Caller must hold _lock.

        Field names must match the Central API schema exactly, otherwise the
        server rejects the request with HTTP 422 (validation error).
        """
        return {
            "strategy_name": self.strategy_name,
            "server_name": self.server_name,
            "status": self._metrics["status"],
            "current_mtm": float(self._metrics["mtm"]),
            "day_pnl": float(self._metrics["pnl"]),
            "number_of_trades": int(self._metrics["trade_count"]),
            # UTC ISO-8601 timestamp with a trailing "Z" (e.g. 2026-07-25T12:34:56Z).
            "last_update_time": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }

    def _run(self):
        """Heartbeat loop: push at least every `heartbeat_interval_seconds`."""
        while not self._stop_event.is_set():
            # Wait for either an explicit update or the heartbeat interval.
            self._wake_event.wait(timeout=self.heartbeat_interval_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            with self._lock:
                payload = self._build_payload_locked()
            self._post(payload)

    def _post(self, payload):
        """POST the payload with retries. All errors are swallowed."""
        if requests is None:
            return
        delay = 1
        for attempt in range(1, self.max_retries + 1):
            if self._stop_event.is_set() and attempt > 1:
                # Don't keep retrying while the process is trying to exit.
                return
            try:
                resp = requests.post(
                    self.endpoint,
                    json=payload,
                    timeout=self.request_timeout_seconds,
                )
                if 200 <= resp.status_code < 300:
                    return
                print(
                    f"[HEARTBEAT] server returned {resp.status_code} "
                    f"(attempt {attempt}/{self.max_retries})"
                )
            except Exception as e:
                print(
                    f"[HEARTBEAT] post failed: {e} "
                    f"(attempt {attempt}/{self.max_retries})"
                )
            time.sleep(delay)
            delay = min(delay * 2, 10)
        # All retries exhausted – give up silently until the next heartbeat.

