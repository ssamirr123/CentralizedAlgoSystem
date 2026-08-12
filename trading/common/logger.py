"""
Structured JSON logging for trading processes.

Every log line is a single JSON object with a consistent shape:

    {
      "timestamp": "...",
      "level": "INFO",
      "component": "example_strategy",
      "event": "ENTRY",
      "server": "ec2-1",
      "algo": "example_strategy",
      "details": {...}
    }

Logs go to both stdout (for CloudWatch/journald capture) and a rotating
file under trading/logs/. Use `log_event(logger, level, event, **details)`
for structured events; plain logger.info(...) still works for free-text
messages during development.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str, server: str, algo: str) -> None:
        super().__init__()
        self._component = component
        self._server = server
        self._algo = algo

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": self._component,
            "server": self._server,
            "algo": self._algo,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        details = getattr(record, "details", None)
        if details:
            payload["details"] = details
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(component: str, server: str = "local-dev", algo: str | None = None) -> logging.Logger:
    """Return a configured structured logger. Safe to call repeatedly (idempotent)."""
    logger_name = f"trading.{component}"
    logger = logging.getLogger(logger_name)

    if logger.handlers:
        return logger

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(log_level)
    logger.propagate = False

    formatter = JsonFormatter(component=component, server=server, algo=algo or component)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / f"{component}.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # Read-only filesystem or permissions issue — stdout logging still works.
        logger.warning("Could not attach file log handler; logging to stdout only.")

    return logger


def log_event(logger: logging.Logger, level: int, event: str, **details: Any) -> None:
    """Emit a structured event log line, e.g. log_event(logger, logging.INFO, "ENTRY", strike=24750)."""
    logger.log(level, event, extra={"event": event, "details": details})
