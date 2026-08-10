"""
Central Strategy Monitoring – Telegram Alert Service
=====================================================
Production-ready alert module for the CentralizedAlgoSystem.

Features
--------
- 6 named alert types: strategy_started, strategy_stopped, strategy_crashed,
  day_loss_exceeded, heartbeat_missing, strategy_recovered
- Retry with exponential back-off (configurable)
- Deduplication window: identical alerts suppressed within N seconds
- Structured logging to both stdout and file
- Configuration via environment variables only (no hard-coded secrets)
- Thread-safe: safe to call from FastAPI worker threads and cron jobs

Environment Variables
---------------------
TELEGRAM_BOT_TOKEN   Your Telegram bot token  (required)
TELEGRAM_CHAT_ID     Target chat/group/channel ID  (required)
ALERT_DEDUP_SECONDS  Duplicate suppression window in seconds  (default: 120)
ALERT_MAX_RETRIES    Maximum send attempts  (default: 3)
ALERT_RETRY_DELAY    Initial retry delay in seconds  (default: 2.0)
DAY_LOSS_LIMIT       Absolute day-loss threshold in ₹  (default: 10000.0)

Usage – singleton (recommended for FastAPI + strategy agents)
-------------------------------------------------------------
    from alerts.telegram import alert_service

    alert_service.strategy_started("mean_reversion_v1", "ec2-ap-south-1a")
    alert_service.strategy_crashed("arb_v2", "ec2-ap-south-1b", reason="KeyError")
    alert_service.day_loss_exceeded("momentum_v3", "ec2-us-east-1",
                                    loss=-15000.0, limit=10000.0)

Usage – custom instance
-----------------------
    from alerts.telegram import AlertService

    svc = AlertService(bot_token="123:ABC", chat_id="-1001234567")
    svc.heartbeat_missing("arb_v2", "ec2-ap-south-1b", minutes=3.5)

Async FastAPI usage
-------------------
    from alerts.telegram import alert_service
    import asyncio

    async def my_route():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, alert_service.strategy_stopped, "arb_v2", "ec2-1"
        )
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
logger = logging.getLogger("alerts.telegram")

_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_file = os.path.join(_log_dir, "telegram_alerts.log")
try:
    _file_handler = logging.FileHandler(_log_file)
    _file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(_file_handler)
except (OSError, PermissionError):
    # Read-only filesystem (e.g. Vercel serverless) — skip file logging
    logger.warning("Could not attach file log handler (read-only filesystem); logging to stdout only.")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EMOJI = {
    "started":   "🟢",
    "stopped":   "🟡",
    "crashed":   "🔴",
    "loss":      "🚨",
    "missing":   "⚠️",
    "recovered": "✅",
}

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# AlertService
# ---------------------------------------------------------------------------
class AlertService:
    """
    Thread-safe Telegram alert service for algo trading monitoring.

    Parameters
    ----------
    bot_token:      Telegram bot token. Falls back to TELEGRAM_BOT_TOKEN env var.
    chat_id:        Target chat/group ID. Falls back to TELEGRAM_CHAT_ID env var.
    dedup_seconds:  Suppress identical alerts within this window (default 120 s).
    max_retries:    Maximum HTTP attempts per alert (default 3).
    retry_delay:    Initial back-off delay; doubles each retry (default 2.0 s).
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        dedup_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_delay: Optional[float] = None,
    ) -> None:
        self._bot_token: str = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id: str = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._dedup_seconds: int = dedup_seconds or int(
            os.environ.get("ALERT_DEDUP_SECONDS", "120")
        )
        self._max_retries: int = max_retries or int(
            os.environ.get("ALERT_MAX_RETRIES", "3")
        )
        self._retry_delay: float = retry_delay or float(
            os.environ.get("ALERT_RETRY_DELAY", "2.0")
        )

        self._dedup_cache: dict[str, float] = {}
        self._lock = threading.Lock()

        if not self._bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set – alerts logged only, NOT sent.")
        if not self._chat_id:
            logger.warning("TELEGRAM_CHAT_ID not set – alerts logged only, NOT sent.")

    # ------------------------------------------------------------------
    # Public alert methods
    # ------------------------------------------------------------------

    def strategy_started(self, strategy_name: str, server_name: str) -> bool:
        """Fire when a strategy transitions to RUNNING for the first time."""
        msg = (
            f"{_EMOJI['started']} *Strategy Started*\n"
            f"• Strategy: `{strategy_name}`\n"
            f"• Server:   `{server_name}`\n"
            f"• Time:     {_now_str()}"
        )
        return self._send(msg, dedup_key=f"started:{strategy_name}:{server_name}")

    def strategy_stopped(self, strategy_name: str, server_name: str) -> bool:
        """Fire when a strategy transitions to STOPPED."""
        msg = (
            f"{_EMOJI['stopped']} *Strategy Stopped*\n"
            f"• Strategy: `{strategy_name}`\n"
            f"• Server:   `{server_name}`\n"
            f"• Time:     {_now_str()}"
        )
        return self._send(msg, dedup_key=f"stopped:{strategy_name}:{server_name}")

    def strategy_crashed(
        self,
        strategy_name: str,
        server_name: str,
        reason: str = "Unknown error",
    ) -> bool:
        """Fire when a strategy status is ERROR."""
        msg = (
            f"{_EMOJI['crashed']} *Strategy Crashed*\n"
            f"• Strategy: `{strategy_name}`\n"
            f"• Server:   `{server_name}`\n"
            f"• Reason:   {reason}\n"
            f"• Time:     {_now_str()}"
        )
        return self._send(msg, dedup_key=f"crashed:{strategy_name}:{server_name}")

    def day_loss_exceeded(
        self,
        strategy_name: str,
        server_name: str,
        loss: float,
        limit: float,
    ) -> bool:
        """Fire when cumulative day P&L crosses the configured loss limit (negative P&L)."""
        msg = (
            f"{_EMOJI['loss']} *Day Loss Limit Exceeded*\n"
            f"• Strategy: `{strategy_name}`\n"
            f"• Server:   `{server_name}`\n"
            f"• Day P&L:  ₹{loss:,.2f}\n"
            f"• Limit:    ₹{limit:,.2f}\n"
            f"• Time:     {_now_str()}"
        )
        return self._send(msg, dedup_key=f"loss:{strategy_name}:{server_name}")

    def heartbeat_missing(
        self,
        strategy_name: str,
        server_name: str,
        minutes: float = 2.0,
    ) -> bool:
        """Fire when no heartbeat has been received for `minutes` minutes."""
        msg = (
            f"{_EMOJI['missing']} *Heartbeat Missing*\n"
            f"• Strategy: `{strategy_name}`\n"
            f"• Server:   `{server_name}`\n"
            f"• Silent for: {minutes:.1f} min\n"
            f"• Time:     {_now_str()}"
        )
        return self._send(msg, dedup_key=f"missing:{strategy_name}:{server_name}")

    def strategy_recovered(
        self,
        strategy_name: str,
        server_name: str,
        downtime_minutes: float = 0.0,
    ) -> bool:
        """Fire when a strategy returns to RUNNING after STOPPED/ERROR."""
        # Clear stale dedup keys so next crash/missing fires fresh
        self._clear_dedup(f"missing:{strategy_name}:{server_name}")
        self._clear_dedup(f"crashed:{strategy_name}:{server_name}")
        self._clear_dedup(f"stopped:{strategy_name}:{server_name}")
        msg = (
            f"{_EMOJI['recovered']} *Strategy Recovered*\n"
            f"• Strategy: `{strategy_name}`\n"
            f"• Server:   `{server_name}`\n"
            f"• Downtime: {downtime_minutes:.1f} min\n"
            f"• Time:     {_now_str()}"
        )
        return self._send(msg, dedup_key=f"recovered:{strategy_name}:{server_name}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_duplicate(self, dedup_key: str) -> bool:
        with self._lock:
            last = self._dedup_cache.get(dedup_key)
            if last is None:
                return False
            return (time.monotonic() - last) < self._dedup_seconds

    def _mark_sent(self, dedup_key: str) -> None:
        with self._lock:
            self._dedup_cache[dedup_key] = time.monotonic()

    def _clear_dedup(self, dedup_key: str) -> None:
        with self._lock:
            self._dedup_cache.pop(dedup_key, None)

    def _send(self, text: str, dedup_key: str) -> bool:
        """
        Core send method: dedup check → retry loop with exponential back-off.
        Never raises; returns True on success, False on failure.
        """
        if self._is_duplicate(dedup_key):
            logger.debug(
                "Suppressed duplicate alert '%s' (within %ds window)",
                dedup_key,
                self._dedup_seconds,
            )
            return True

        logger.info("Sending alert | key=%s", dedup_key)

        if not self._bot_token or not self._chat_id:
            logger.warning(
                "Alert NOT sent (missing credentials) | key=%s | preview=%.80s",
                dedup_key,
                text,
            )
            return False

        url = _TELEGRAM_API.format(token=self._bot_token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        delay = self._retry_delay
        for attempt in range(1, self._max_retries + 1):
            try:
                with httpx.Client(timeout=10.0) as client:
                    response = client.post(url, json=payload)
                data = response.json()

                if response.status_code == 200 and data.get("ok"):
                    self._mark_sent(dedup_key)
                    logger.info(
                        "Alert delivered | key=%s | attempt=%d", dedup_key, attempt
                    )
                    return True

                logger.error(
                    "Telegram API error | key=%s | status=%d | body=%s",
                    dedup_key,
                    response.status_code,
                    data,
                )
                # 4xx = client-side credential/config issue, retrying won't help
                if 400 <= response.status_code < 500:
                    return False

            except httpx.TimeoutException:
                logger.warning(
                    "Telegram request timed out | key=%s | attempt=%d/%d",
                    dedup_key, attempt, self._max_retries,
                )
            except httpx.RequestError as exc:
                logger.warning(
                    "Telegram network error | key=%s | attempt=%d/%d | error=%s",
                    dedup_key, attempt, self._max_retries, exc,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Unexpected error during send | key=%s | error=%s",
                    dedup_key, exc,
                )
                return False

            if attempt < self._max_retries:
                logger.info("Retrying alert in %.1fs …", delay)
                time.sleep(delay)
                delay *= 2  # exponential back-off

        logger.error(
            "Alert delivery failed after %d attempts | key=%s",
            self._max_retries,
            dedup_key,
        )
        return False


# ---------------------------------------------------------------------------
# Module-level singleton – import this everywhere
# ---------------------------------------------------------------------------
alert_service = AlertService()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
