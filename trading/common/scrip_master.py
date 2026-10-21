"""
Robust loader for Angel One's OpenAPIScripMaster.json (~35 MB).

The CDN regularly truncates the body (IncompleteRead) or stalls past a short
read timeout, which crashed or aborted algos at startup/entry (2026-10-21).
This retries with a browser-like UA and a generous read timeout, and caches
the parsed file on disk per day so restarts and sibling algos reuse it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
_TIMEOUT = (10, 120)
_ATTEMPTS = 4


def _cache_path() -> str:
    return os.path.join(
        tempfile.gettempdir(), f"angel_scrip_master_{datetime.now():%Y%m%d}.json"
    )


def load_scrip_master() -> list[dict]:
    """Return the full scrip master as a list of dicts. Raises the last error
    if every attempt fails and no cache for today exists."""
    path = _cache_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            logger.info("Scrip master loaded from cache: %d rows", len(rows))
            return rows
        except (OSError, ValueError) as exc:
            logger.warning("Scrip master cache unreadable (%s); re-downloading.", exc)

    last_exc: Exception | None = None
    rows = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            logger.info("Downloading scrip master (attempt %d/%d)...", attempt, _ATTEMPTS)
            resp = requests.get(SCRIP_MASTER_URL, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            rows = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 - IncompleteRead, timeouts, bad JSON
            last_exc = exc
            logger.warning("Scrip master download failed: %s", exc)
            time.sleep(2 * attempt)
    if rows is None:
        raise last_exc  # type: ignore[misc]

    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not cache scrip master: %s", exc)
    return rows
