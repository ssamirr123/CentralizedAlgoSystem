"""
login.py
Authenticate with the configured broker and return a thin BrokerAdapter
exposing the methods market_data / option_chain need, so the rest of the
algo stays broker-agnostic.

Pick the broker with `BROKER` (or `EMA20_PULLBACK_BROKER`):
    ANGELONE  (default) → SmartAPI + TOTP
    DHAN                → dhanhq SDK, long-lived access token
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger

log = get_logger()


@dataclass
class BrokerAdapter:
    kind: str
    client: Any
    jwt_token: Optional[str] = None
    feed_token: Optional[Any] = None


def _login_angelone() -> BrokerAdapter:
    import pyotp
    try:
        from SmartApi import SmartConnect
    except ImportError:  # pragma: no cover
        from smartapi import SmartConnect

    if not (config.clientid and config.apikey and config.mpin and config.token):
        raise RuntimeError(
            "AngelOne credentials incomplete. Need ANGELONE_CLIENT_ID, "
            "ANGELONE_API_KEY, ANGELONE_MPIN, ANGELONE_TOTP_SECRET."
        )

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
            log.info("AngelOne connected (feed token acquired: %s)", bool(feed_token))
            return BrokerAdapter(kind="ANGELONE", client=obj, jwt_token=jwt_token, feed_token=feed_token)
        except Exception as exc:  # noqa: BLE001
            log.warning("AngelOne login attempt %d failed: %s", attempt + 1, exc)
            time.sleep(1)
    raise RuntimeError("AngelOne login failed after 3 attempts")


def _login_dhan() -> BrokerAdapter:
    try:
        from dhanhq import DhanContext, dhanhq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "dhanhq SDK not installed. Add `dhanhq` to the venv "
            "(pip install dhanhq)."
        ) from exc

    if not (config.dhan_client_id and config.dhan_access_token):
        raise RuntimeError(
            "Dhan credentials incomplete. Need DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN."
        )

    for attempt in range(0, 3):
        try:
            ctx = DhanContext(
                client_id=config.dhan_client_id,
                access_token=config.dhan_access_token,
            )
            client = dhanhq(ctx)
            probe = client.get_fund_limits()
            status = (probe or {}).get("status", "")
            if status and status.lower() == "failure":
                raise RuntimeError(f"Dhan fund-limit probe failed: {probe}")
            log.info("Dhan connected (client_id tail: ...%s)",
                     config.dhan_client_id[-4:] if config.dhan_client_id else "")
            return BrokerAdapter(kind="DHAN", client=client)
        except Exception as exc:  # noqa: BLE001
            log.warning("Dhan login attempt %d failed: %s", attempt + 1, exc)
            time.sleep(1)
    raise RuntimeError("Dhan login failed after 3 attempts")


def login() -> BrokerAdapter:
    broker = (config.BROKER or "ANGELONE").strip().upper()
    log.info("Logging in via broker=%s", broker)
    if broker == "ANGELONE":
        return _login_angelone()
    if broker == "DHAN":
        return _login_dhan()
    raise ValueError(f"Unsupported BROKER={broker!r}. Use ANGELONE or DHAN.")
