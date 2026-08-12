"""
ICICI Direct Breeze API adapter (STUB).

Same safety posture as the other adapters: place_order()/cancel_order()
refuse to run unless TRADING_MODE=live, checked before any SDK call.

Install when ready:  pip install breeze-connect
Docs: https://api.icicidirect.com/apidocs/
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


class ICICIBreezeBroker(BrokerClient):
    def __init__(self, config: TradingConfig) -> None:
        self._config = config
        self._connected = False
        # TODO: from breeze_connect import BreezeConnect
        # self._breeze = BreezeConnect(api_key=config.credentials.icici_breeze_api_key)

    def connect(self) -> None:
        creds = self._config.credentials
        if not (creds.icici_breeze_api_secret and creds.icici_breeze_session_token):
            raise BrokerConfigError(
                "ICICI_BREEZE_API_SECRET / ICICI_BREEZE_SESSION_TOKEN not set. "
                "Generate a session token via the Breeze login flow first."
            )
        # TODO: self._breeze.generate_session(
        #     api_secret=creds.icici_breeze_api_secret,
        #     session_token=creds.icici_breeze_session_token,
        # )
        raise NotImplementedError(
            "ICICIBreezeBroker.connect() is a stub. Implement the Breeze session "
            "setup above, then remove this line."
        )

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        # TODO: data = self._breeze.get_quotes(stock_code=symbol, exchange_code="NSE")
        raise NotImplementedError("ICICIBreezeBroker.get_quote() is a stub.")

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
                "Refusing to place a real ICICI Breeze order: TRADING_MODE is not 'live'. "
                "Use BROKER=paper for development/testing."
            )
        # TODO: implement self._breeze.place_order(...) here, after the guard above.
        raise NotImplementedError("ICICIBreezeBroker.place_order() is a stub.")

    def cancel_order(self, order_id: str) -> bool:
        if not self._config.is_live:
            raise LiveTradingDisabledError(
                "Refusing to cancel a real ICICI Breeze order: TRADING_MODE is not 'live'."
            )
        raise NotImplementedError("ICICIBreezeBroker.cancel_order() is a stub.")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("ICICIBreezeBroker.get_positions() is a stub.")
