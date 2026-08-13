"""
AngelOne SmartAPI adapter (STUB).

Same safety posture as zerodha_kite.py: place_order()/cancel_order() refuse
to run unless TRADING_MODE=live, checked before any SDK call, independent
of whether the SDK integration below has actually been implemented yet.

Install when ready:  pip install smartapi-python pyotp
Docs: https://smartapi.angelbroking.com/docs
"""
from __future__ import annotations

from trading.common.broker import (
    BrokerClient,
    BrokerConfigError,
    LiveTradingDisabledError,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    Quote,
)
from trading.common.config import TradingConfig


class AngelOneBroker(BrokerClient):
    def __init__(self, config: TradingConfig) -> None:
        self._config = config
        self._connected = False
        # TODO: from SmartApi import SmartConnect
        # self._smart_api = SmartConnect(api_key=config.credentials.angelone_api_key)

    def connect(self) -> None:
        creds = self._config.credentials
        if not (creds.angelone_client_id and creds.angelone_password and creds.angelone_totp_secret):
            raise BrokerConfigError(
                "ANGELONE_CLIENT_ID / ANGELONE_PASSWORD / ANGELONE_TOTP_SECRET not fully set."
            )
        # TODO: totp = pyotp.TOTP(creds.angelone_totp_secret).now()
        # TODO: session = self._smart_api.generateSession(creds.angelone_client_id, creds.angelone_password, totp)
        raise NotImplementedError(
            "AngelOneBroker.connect() is a stub. Implement the SmartAPI session "
            "setup above, then remove this line."
        )

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        # TODO: data = self._smart_api.ltpData(exchange, tradingsymbol, symboltoken)
        raise NotImplementedError("AngelOneBroker.get_quote() is a stub.")

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> OrderResult:
        if not self._config.is_live:
            raise LiveTradingDisabledError(
                "Refusing to place a real AngelOne order: TRADING_MODE is not 'live'. "
                "Use BROKER=paper for development/testing."
            )
        # TODO: implement self._smart_api.placeOrder(...) here, after the guard above.
        raise NotImplementedError("AngelOneBroker.place_order() is a stub.")

    def cancel_order(self, order_id: str) -> bool:
        if not self._config.is_live:
            raise LiveTradingDisabledError(
                "Refusing to cancel a real AngelOne order: TRADING_MODE is not 'live'."
            )
        raise NotImplementedError("AngelOneBroker.cancel_order() is a stub.")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("AngelOneBroker.get_positions() is a stub.")
