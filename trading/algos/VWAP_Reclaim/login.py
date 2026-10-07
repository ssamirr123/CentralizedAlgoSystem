"""
login.py
Authentication with Angel One SmartAPI (same shape as supertrendPivotAlgo).
Returns a connected SmartConnect object with jwt_token / feed_token attached.
"""
from __future__ import annotations

import time

import pyotp

try:
    from SmartApi import SmartConnect
except ImportError:  # pragma: no cover
    from smartapi import SmartConnect

from trading.algos.VWAP_Reclaim import config
from trading.algos.VWAP_Reclaim.logger import get_logger

log = get_logger()


def login():
    for attempt in range(0, 3):
        try:
            tk = pyotp.TOTP(config.token).now()
            obj = SmartConnect(api_key=config.apikey)
            data = obj.generateSession(config.clientid, config.mpin, tk)

            jwt_token = (data or {}).get("data", {}).get("jwtToken")
            try:
                feed_token = obj.getfeedToken()
            except Exception:  # noqa: BLE001
                feed_token = getattr(obj, "feed_token", None)

            obj.jwt_token = jwt_token
            obj.feed_token = feed_token

            log.info("Connection established (feed token acquired: %s)", bool(feed_token))
            return obj
        except Exception as exc:  # noqa: BLE001
            log.warning("Login attempt %d failed: %s", attempt + 1, exc)
            time.sleep(1)

    raise RuntimeError("Login failed after 3 attempts")
