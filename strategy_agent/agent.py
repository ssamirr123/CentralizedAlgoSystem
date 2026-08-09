"""
Lightweight Strategy Heartbeat Agent
====================================
Threaded heartbeat client for strategy processes.

- Sends heartbeat every 30 seconds (configurable)
- Uses `requests` with retry + exponential backoff
- Keeps strategy loop non-blocking via daemon thread
- Continues running through transient network failures
- Exposes `update_metrics(mtm, pnl, trade_count, status)`

Expected central endpoint: POST /update_strategy
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


@dataclass
class StrategyMetrics:
    mtm: float = 0.0
    day_pnl: float = 0.0
    trade_count: int = 0
    status: str = "RUNNING"


class StrategyHeartbeatAgent:
    """Background heartbeat sender that does not block the trading loop."""

    def __init__(
        self,
        strategy_name: str,
        server_name: str,
        api_base_url: str,
        heartbeat_interval_seconds: int = 30,
        request_timeout_seconds: int = 5,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
    ) -> None:
        self.strategy_name = strategy_name
        self.server_name = server_name
        self.api_base_url = api_base_url.rstrip("/")
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

        self._metrics = StrategyMetrics()
        self._metrics_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._logger = self._build_logger()

    def start(self) -> None:
        """Start the heartbeat loop in a daemon thread."""
        if self._thread and self._thread.is_alive():
            self._logger.debug("Heartbeat thread already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"heartbeat-{self.strategy_name}",
            daemon=True,
        )
        self._thread.start()
        self._logger.info("Heartbeat agent started for %s on %s", self.strategy_name, self.server_name)

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Signal the thread to stop and wait briefly for clean exit."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_seconds)
        self._logger.info("Heartbeat agent stopped for %s on %s", self.strategy_name, self.server_name)

    def update_metrics(self, mtm: float, pnl: float, trade_count: int, status: str) -> None:
        """Update shared metrics snapshot used by the heartbeat sender."""
        with self._metrics_lock:
            self._metrics.mtm = float(mtm)
            self._metrics.day_pnl = float(pnl)
            self._metrics.trade_count = int(trade_count)
            self._metrics.status = str(status)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._build_payload()
                success = self._send_with_retry(payload)
                if success:
                    self._logger.debug("Heartbeat sent successfully for %s", self.strategy_name)
            except Exception as exc:  # noqa: BLE001
                # Keep the thread alive even for unexpected failures.
                self._logger.exception("Unexpected heartbeat loop error: %s", exc)

            self._stop_event.wait(self.heartbeat_interval_seconds)

    def _build_payload(self) -> dict[str, Any]:
        with self._metrics_lock:
            metrics = StrategyMetrics(
                mtm=self._metrics.mtm,
                day_pnl=self._metrics.day_pnl,
                trade_count=self._metrics.trade_count,
                status=self._metrics.status,
            )

        return {
            "strategy_name": self.strategy_name,
            "server_name": self.server_name,
            "status": metrics.status,
            "current_mtm": metrics.mtm,
            "day_pnl": metrics.day_pnl,
            "number_of_trades": metrics.trade_count,
            "last_update_time": datetime.now(timezone.utc).isoformat(),
        }

    def _send_with_retry(self, payload: dict[str, Any]) -> bool:
        endpoint = f"{self.api_base_url}/update_strategy"
        delay = self.retry_backoff_seconds

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    endpoint,
                    json=payload,
                    timeout=self.request_timeout_seconds,
                )
                if response.ok:
                    return True

                self._logger.error(
                    "Heartbeat rejected (attempt %d/%d). status=%s body=%s",
                    attempt,
                    self.max_retries,
                    response.status_code,
                    response.text,
                )
            except requests.RequestException as exc:
                self._logger.error(
                    "Heartbeat send failed (attempt %d/%d): %s",
                    attempt,
                    self.max_retries,
                    exc,
                )

            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2

        self._logger.error("All heartbeat retries failed for %s", self.strategy_name)
        return False

    def _build_logger(self) -> logging.Logger:
        logger_name = f"strategy_heartbeat.{self.strategy_name}.{self.server_name}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)

        if logger.handlers:
            return logger

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        )

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        log_file = Path(__file__).resolve().parent / "heartbeat_agent.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        logger.propagate = False
        return logger
