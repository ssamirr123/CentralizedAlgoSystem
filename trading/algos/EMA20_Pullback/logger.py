"""
Folder-local logging for EMA20_Pullback.

Writes to a daily rotating file under this algo's own `logs/` directory
(path is overridable via `EMA20_PULLBACK_LOG_DIR`) and mirrors the same
line format to the console. Matches the `supertrendPivotAlgo` pattern.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from trading.algos.EMA20_Pullback import config

_LOGGER: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = os.path.join(config.LOG_DIR, f"ema20_pullback_{datetime.now():%Y-%m-%d}.log")

    logger = logging.getLogger("trading.EMA20_Pullback")
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
