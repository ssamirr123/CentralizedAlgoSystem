"""
Canonical structured logging for the whole project.

Two entry points:

* ``configure_logging()`` -- call ONCE at process start (the app factory
  does this). Installs exactly one structured stdout handler on the root
  logger, so every ``logging.getLogger(...)`` in the codebase -- including
  ``alerts.telegram`` -- emits consistent JSON without configuring itself.
  stdout only: safe on a read-only filesystem, container/CloudWatch
  friendly. Idempotent; removes any prior ``logging.basicConfig`` handler
  so there are no duplicate lines.

* ``get_logger(component, ...)`` + ``log_event(logger, level, event, **d)``
  -- the per-component structured logger used by the strategy runtime.
  Adds a rotating file under ``trading/logs/`` when the FS is writable and
  falls back to stdout-only when it is not.

Every log line is a single JSON object. The per-component shape:

    {"timestamp","level","component","server","algo","message",
     "event"?, "details"?, "exc_info"?}

The root shape (anything not going through get_logger):

    {"timestamp","level","logger","message", "event"?, "details"?, "exc_info"?}
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

_ROOT_HANDLER_NAME = "cas-root"
_root_configured = False


# --------------------------------------------------------------------------
# Stage 20 -- secret redaction. Applied to every message + traceback that
# passes through this module's formatters, so a credential accidentally
# interpolated into a log line (or an exception string) never lands in
# stdout / CloudWatch / a shipped log.
# --------------------------------------------------------------------------
_REDACTED = "<redacted>"
_KEY = (
    r"api[_-]?key|secret[_-]?key|secret|client[_-]?secret|password|passwd|pwd|mpin|"
    r"totp[_-]?secret|totp|session[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"control[_-]?api[_-]?key|auth[_-]?secret[_-]?key|authorization|x-api-key|breeze[_-]?[a-z_]*"
)
_REDACTORS: list[tuple[re.Pattern[str], str]] = [
    # Authorization: Bearer <jwt>  (before the generic key rule, which would
    # otherwise just redact the word "Bearer" and leave the token)
    (re.compile(r'((?:authorization["\']?\s*[:=]\s*)?bearer\s+)[A-Za-z0-9._\-]{10,}', re.IGNORECASE),
     rf'\1{_REDACTED}'),
    # key = value / "key": "value" / key: value  (keep the key, drop the value)
    (re.compile(rf'((?:{_KEY})["\']?\s*[:=]\s*)(["\']?)([^\s"\',;)}}]{{3,}})(\2)', re.IGNORECASE),
     rf'\1\2{_REDACTED}\4'),
    # DSNs with an inline password:  scheme://user:pass@host
    (re.compile(r'([a-z0-9+]+://[^\s:/@]+:)[^\s@/]+(@)', re.IGNORECASE), rf'\1{_REDACTED}\2'),
    # AWS access key id
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '<redacted-aws-key-id>'),
    # PEM private key blocks
    (re.compile(r'-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----', re.DOTALL),
     '<redacted-private-key>'),
]


def redact_text(text: str) -> str:
    """Best-effort scrub of credential-shaped substrings. Never raises."""
    if not text:
        return text
    try:
        for pattern, repl in _REDACTORS:
            text = pattern.sub(repl, text)
    except Exception:  # noqa: BLE001 - logging must never fail
        return text
    return text


class SecretRedactionFilter(logging.Filter):
    """Attach to any handler to scrub record args/msg before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
            red = redact_text(msg)
            if red != msg:
                record.msg = red
                record.args = ()
        except Exception:  # noqa: BLE001
            pass
        return True


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
            "message": redact_text(record.getMessage()),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        details = getattr(record, "details", None)
        if details:
            payload["details"] = details
        if record.exc_info:
            payload["exc_info"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class RootJsonFormatter(logging.Formatter):
    """JSON formatter for the root handler -- anything not emitted through
    get_logger(). ensure_ascii keeps emoji as \\uXXXX escapes, so a
    cp1252 Windows console can never raise UnicodeEncodeError mid-log."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        details = getattr(record, "details", None)
        if details:
            payload["details"] = details
        if record.exc_info:
            payload["exc_info"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(*, level: str | None = None, force: bool = False) -> logging.Logger:
    """Install one structured stdout handler on the root logger. Idempotent.

    * stdout only -- never touches the filesystem, so it works identically
      whether the FS is writable or read-only.
    * Removes plain logging.basicConfig() StreamHandlers so there are no
      duplicate lines; leaves handlers installed by other frameworks
      (e.g. pytest's log capture) untouched.
    * Safe to call before or after Telegram is (not) configured -- it only
      sets up transport, never sends anything.
    """
    global _root_configured
    root = logging.getLogger()

    if _root_configured and not force:
        return root

    resolved = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    root.setLevel(resolved)

    for handler in list(root.handlers):
        # our own previous handler, or a bare basicConfig StreamHandler
        # (exact type -- NOT FileHandler or framework subclasses)
        if getattr(handler, "name", "") == _ROOT_HANDLER_NAME or type(handler) is logging.StreamHandler:
            root.removeHandler(handler)
            handler.close()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.set_name(_ROOT_HANDLER_NAME)
    stream_handler.setFormatter(RootJsonFormatter())
    stream_handler.addFilter(SecretRedactionFilter())
    root.addHandler(stream_handler)

    _root_configured = True
    return root


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


def attach_shipper(logger: logging.Logger, shipper: Any) -> None:
    """Attach a trading.common.log_shipper.LogShipper to a logger. log_event()
    checks for this and ships qualifying events (see log_shipper.should_ship)
    to the control-center API in addition to the normal local logging."""
    logger._cc_log_shipper = shipper  # noqa: SLF001


def log_event(logger: logging.Logger, level: int, event: str, **details: Any) -> None:
    """Emit a structured event log line, e.g. log_event(logger, logging.INFO, "ENTRY", strike=24750).

    Also ships to the control-center API if a shipper is attached (see
    attach_shipper) and the event qualifies (log_shipper.should_ship) --
    the curated trading-event list, or WARNING+ regardless of event name.
    """
    logger.log(level, event, extra={"event": event, "details": details})

    shipper = getattr(logger, "_cc_log_shipper", None)
    if shipper is not None:
        from trading.common.log_shipper import should_ship

        if should_ship(event, level):
            shipper.ship(level=logging.getLevelName(level), event=event, details=details)
