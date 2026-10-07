"""
Folder-local logging for VWAP_Reclaim.

Writes to a daily rotating file under this algo's own `logs/` directory
(path is overridable via `VWAP_RECLAIM_LOG_DIR`) and mirrors the same
line format to the console. Matches the `supertrendPivotAlgo` pattern so
the dashboard log tailer can scrape either algo with the same regex.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from trading.algos.VWAP_Reclaim import config

_LOGGER: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Return the singleton configured logger for the algo."""
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = os.path.join(config.LOG_DIR, f"vwap_reclaim_{datetime.now():%Y-%m-%d}.log")

    logger = logging.getLogger("trading.VWAP_Reclaim")
    logger.setLevel(getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    _LOGGER = logger
    return logger
