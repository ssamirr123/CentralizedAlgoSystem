"""
Heartbeat sender for the control-center schema (Milestone 5's heartbeats
table via POST /api/heartbeat) -- separate from strategy_agent/agent.py,
which still feeds the old strategy_heartbeats table/dashboard unchanged.
Runs alongside it, not instead of it.

Recommended interval per the project's own spec: 10 seconds (vs. the old
agent's 30s default) -- kept as a separate config value
(CONTROL_HEARTBEAT_INTERVAL_SECONDS) rather than changing the existing
one, since that would also affect the old dashboard's staleness math.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import requests


@dataclass
class ControlCenterMetrics:
    status: str = "RUNNING"
    pnl: float = 0.0
    position: str | None = None


class ControlCenterHeartbeatAgent:
    """Background heartbeat sender for POST /api/heartbeat. Non-blocking
    (daemon thread), same shape as strategy_agent.agent.StrategyHeartbeatAgent
    but reports cpu/memory (via psutil, this process) and position, and
    authenticates with the API's X-API-Key header."""

    def __init__(
        self,
        algo_name: str,
        server_name: str,
        api_base_url: str,
        api_key: str,
        interval_seconds: int = 10,
        request_timeout_seconds: int = 5,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
    ) -> None:
        self.algo_name = algo_name
        self.server_name = server_name
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.interval_seconds = interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

        self._metrics = ControlCenterMetrics()
        self._metrics_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._process = psutil.Process()
        self._process.cpu_percent()  # first call always returns 0.0 -- primes the internal baseline

        self._logger = self._build_logger()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"cc-heartbeat-{self.algo_name}", daemon=True,
        )
        self._thread.start()
        self._logger.info("Control-center heartbeat agent started for %s on %s", self.algo_name, self.server_name)

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_seconds)
        self._logger.info("Control-center heartbeat agent stopped for %s on %s", self.algo_name, self.server_name)

    def update_metrics(self, status: str, pnl: float, position: str | None = None) -> None:
        with self._metrics_lock:
            self._metrics.status = status
            self._metrics.pnl = pnl
            self._metrics.position = position

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._build_payload()
                self._send_with_retry(payload)
            except Exception as exc:  # noqa: BLE001
                self._logger.exception("Unexpected control-center heartbeat error: %s", exc)
            self._stop_event.wait(self.interval_seconds)

    def _build_payload(self) -> dict[str, Any]:
        with self._metrics_lock:
            metrics = ControlCenterMetrics(self._metrics.status, self._metrics.pnl, self._metrics.position)

        try:
            cpu = self._process.cpu_percent()
            memory = self._process.memory_percent()
        except psutil.Error:
            cpu = None
            memory = None

        return {
            "algo_id": self.algo_name,
            "server_id": self.server_name,
            "status": metrics.status,
            "cpu": cpu,
            "memory": memory,
            "pnl": metrics.pnl,
            "position": metrics.position,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _send_with_retry(self, payload: dict[str, Any]) -> bool:
        endpoint = f"{self.api_base_url}/api/heartbeat"
        headers = {"X-API-Key": self.api_key}
        delay = self.retry_backoff_seconds

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(endpoint, json=payload, headers=headers, timeout=self.request_timeout_seconds)
                if response.ok:
                    return True
                self._logger.error(
                    "Control-center heartbeat rejected (attempt %d/%d): status=%s body=%s",
                    attempt, self.max_retries, response.status_code, response.text,
                )
                if 400 <= response.status_code < 500:
                    return False  # client/config issue -- retrying won't help
            except requests.RequestException as exc:
                self._logger.error(
                    "Control-center heartbeat send failed (attempt %d/%d): %s", attempt, self.max_retries, exc,
                )
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2

        self._logger.error("All control-center heartbeat retries failed for %s", self.algo_name)
        return False

    def _build_logger(self) -> logging.Logger:
        logger_name = f"control_center_heartbeat.{self.algo_name}.{self.server_name}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if logger.handlers:
            return logger

        formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        log_file = Path(__file__).resolve().parent.parent / "logs" / "control_center_heartbeat.log"
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            logger.warning("Could not attach file log handler; logging to stdout only.")

        logger.propagate = False
        return logger
