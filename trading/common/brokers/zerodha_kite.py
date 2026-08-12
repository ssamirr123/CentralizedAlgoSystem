"""
Zerodha Kite Connect adapter (STUB).

Real implementation TODOs are marked below. This file intentionally does
NOT place real orders yet — place_order() raises LiveTradingDisabledError
unconditionally until you fill in the real kiteconnect.KiteConnect calls
AND set TRADING_MODE=live. The check happens before any SDK call, so a
half-finished implementation still can't fire an order by accident.

Install when ready:  pip install kiteconnect
Docs: https://kite.trade/docs/connect/v3/
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


class ZerodhaKiteBroker(BrokerClient):
    def __init__(self, config: TradingConfig) -> None:
        self._config = config
        self._connected = False
        # TODO: from kiteconnect import KiteConnect
        # self._kite = KiteConnect(api_key=config.credentials.zerodha_api_key)

    def connect(self) -> None:
        if not self._config.credentials.zerodha_access_token:
            raise BrokerConfigError(
                "ZERODHA_ACCESS_TOKEN not set. Generate one via the Kite Connect "
                "login flow before starting this algo."
            )
        # TODO: self._kite.set_access_token(self._config.credentials.zerodha_access_token)
        # TODO: verify session, e.g. self._kite.profile()
        raise NotImplementedError(
            "ZerodhaKiteBroker.connect() is a stub. Implement the kiteconnect "
            "session setup above, then remove this line."
        )

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        # TODO: data = self._kite.quote(symbol); return Quote(symbol, data[symbol]["last_price"], ...)
        raise NotImplementedError("ZerodhaKiteBroker.get_quote() is a stub.")

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
                "Refusing to place a real Zerodha order: TRADING_MODE is not 'live'. "
                "Use BROKER=paper for development/testing."
            )
        # TODO: implement self._kite.place_order(...) here, after the guard above.
        raise NotImplementedError("ZerodhaKiteBroker.place_order() is a stub.")

    def cancel_order(self, order_id: str) -> bool:
        if not self._config.is_live:
            raise LiveTradingDisabledError(
                "Refusing to cancel a real Zerodha order: TRADING_MODE is not 'live'."
            )
        raise NotImplementedError("ZerodhaKiteBroker.cancel_order() is a stub.")

    def get_positions(self) -> list[Position]:
        # TODO: read-only — safe to implement before place_order.
        raise NotImplementedError("ZerodhaKiteBroker.get_positions() is a stub.")
