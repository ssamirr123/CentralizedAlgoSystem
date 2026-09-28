"""
logger.py
Centralized logging setup. Writes to console and a daily rotating file.
"""

import logging
import os
from datetime import datetime

import config

_LOGGER = None


def get_logger():
    """Return a singleton configured logger for the bot."""
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_file = os.path.join(
        config.LOG_DIR, f"bot_{datetime.now():%Y-%m-%d}.log"
    )

    logger = logging.getLogger("option_selling_bot")
    logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not logger.handlers:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        console = logging.StreamHandler()
        console.setFormatter(fmt)
        logger.addHandler(console)

    _LOGGER = logger
    return logger

