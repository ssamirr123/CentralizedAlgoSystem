"""
Ships trading-significant log events to POST /api/logs (Milestone 5's
logs table), so the dashboard can show them without RDP/SSM access to
the instance's local log files.

Only ships a curated set of events -- not every line trading/common/
logger.py already emits. Milestone 1's operational boilerplate
(ALGO_STARTED, BROKER_CONNECTED, HEARTBEAT_RUNNING, ...) stays local-file-
only; shipping every log line to the DB would be noisy and mostly
useless for a trading audit trail. SHIPPABLE_EVENTS matches the project's
own spec exactly; anything WARNING+ ships regardless of event name, since
operational problems are worth centralizing even if the event name isn't
in the curated trading-event list.

Threaded queue + background worker, same shape as the heartbeat agents
-- non-blocking from the caller's perspective (ship() just enqueues).
The queue is bounded and DROPS the oldest entry on overflow rather than
blocking the strategy loop or growing unbounded if the API is down.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

SHIPPABLE_EVENTS = {
    "START", "STOP", "ENTRY", "EXIT", "SL", "RE-ENTRY",
    "ERROR", "WARNING", "BROKER_ERROR", "NETWORK_ERROR", "SQUARE_OFF",
}


def should_ship(event: str, level: int) -> bool:
    return event in SHIPPABLE_EVENTS or level >= logging.WARNING


class LogShipper:
    def __init__(
        self,
        algo_name: str,
        server_name: str,
        api_base_url: str,
        api_key: str,
        max_queue_size: int = 200,
        request_timeout_seconds: int = 5,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.algo_name = algo_name
        self.server_name = server_name
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._dropped_count = 0

        self._logger = logging.getLogger(f"log_shipper.{algo_name}.{server_name}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=f"log-shipper-{self.algo_name}", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_seconds)

    def ship(self, level: str, event: str, details: dict[str, Any] | None = None) -> None:
        """Non-blocking enqueue. Drops the OLDEST queued item (not this
        new one) on overflow, so a burst of events doesn't permanently
        wedge the queue on stale entries while the newest, most relevant
        ones get discarded instead."""
        item = {
            "algo_id": self.algo_name,
            "server_id": self.server_name,
            "level": level,
            "event": event,
            "details": details or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            self._dropped_count += 1
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass  # racing with another producer -- fine, this is best-effort

    def _loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._send_with_retry(item)

    def _send_with_retry(self, item: dict[str, Any]) -> bool:
        endpoint = f"{self.api_base_url}/api/logs"
        headers = {"X-API-Key": self.api_key}
        delay = self.retry_backoff_seconds

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(endpoint, json=item, headers=headers, timeout=self.request_timeout_seconds)
                if response.ok:
                    return True
                self._logger.error(
                    "Log ship rejected (attempt %d/%d): status=%s body=%s",
                    attempt, self.max_retries, response.status_code, response.text,
                )
                if 400 <= response.status_code < 500:
                    return False
            except requests.RequestException as exc:
                self._logger.error("Log ship failed (attempt %d/%d): %s", attempt, self.max_retries, exc)
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2

        return False
