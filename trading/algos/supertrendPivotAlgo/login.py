"""
login.py
Handles authentication with Angel One SmartAPI.
"""

import time

import pyotp

try:
    # Newer package name
    from SmartApi import SmartConnect
except ImportError:  # pragma: no cover
    # Older package name
    from smartapi import SmartConnect

import config
from logger import get_logger

log = get_logger()


def login():
    """
    Authenticate with SmartAPI and return a connected SmartConnect object.

    The returned object also carries the tokens needed for the live WebSocket
    feed, exposed as convenience attributes:
        obj.jwt_token   -> JWT access token (auth_token for SmartWebSocketV2)
        obj.feed_token  -> feed token for SmartWebSocketV2

    Returns
    -------
    SmartConnect
        An authenticated SmartConnect session.
    """
    for attempt in range(0, 3):
        try:
            apikey = config.apikey
            clientid = config.clientid
            mpin = config.mpin
            token = config.token
            tk = pyotp.TOTP(token).now()
            obj = SmartConnect(api_key=apikey)
            data = obj.generateSession(clientid, mpin, tk)

            # Capture tokens required by the live WebSocket feed.
            jwt_token = (data or {}).get("data", {}).get("jwtToken")
            try:
                feed_token = obj.getfeedToken()
            except Exception:  # noqa: BLE001
                feed_token = getattr(obj, "feed_token", None)

            # Attach for later use (e.g. tick_stream.LiveCandleStream).
            obj.jwt_token = jwt_token
            obj.feed_token = feed_token

            log.info("Connection established (feed token acquired: %s)",
                     bool(feed_token))
            print('Connection Established')
            return obj
        except Exception as e:  # noqa: BLE001
            log.warning("Login attempt %d failed: %s", attempt + 1, e)
            print('_Error Resolved')
            time.sleep(1)

    raise RuntimeError("Login failed after 3 attempts")

