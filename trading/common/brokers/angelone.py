"""
AngelOne SmartAPI adapter -- a real implementation of BrokerClient.

This is the ONLY file in the project allowed to know that "AngelOne" means
SmartConnect/SmartWebSocketV2, TOTP+MPIN login, `variety`/`producttype`/
`symboltoken` request fields, or Angel's own order-status vocabulary.
Everything above BrokerClient (RiskManager, StrategyExecutionEngine,
OrderIntent, ...) only ever sees the generic Quote/OrderResult/Position
types and the generic OrderSide/OrderType enums declared in
trading.common.broker.

IMPORTANT -- this adapter is NOT wired into any live algo. DoubleStraddelAlgo,
CombinedVwapNifty and Vwap_Algo_Nifty_hedge each still hold their own direct
`from SmartApi import SmartConnect` dependency and keep running exactly as
before; this file exists purely behind trading.common.broker.create_broker()
for the paper/execution-engine test path today, and as the target for a
future migration PR.

Testability: the real SmartAPI SDK is NOT a dependency of this shared
module (and isn't reliably importable in every dev/CI environment -- the
installed smartapi-python package on this machine, for instance, pulls in
`logzero`, which isn't part of this project's own requirements). So the
SmartConnect client is never imported at module scope, and construction is
injectable via `smart_api_factory` / `instrument_resolver` specifically so
tests can supply a fake and this module never has to import SmartApi to be
tested. Production callers simply omit both and get the real lazy import.

Known limitations (see also the Phase 2 final report):
  - Instrument resolution (tradingsymbol -> exchange + symboltoken) is
    Angel-specific and NOT solved generically -- see _resolve_instrument().
  - Streaming/WebSocket data is NOT part of BrokerClient today, so it is
    deliberately not implemented here. DoubleStraddelAlgo's own
    websocket_feed.py (SmartWebSocketV2) is untouched and remains the only
    live tick source for that algo.
  - Stop-loss/trigger orders aren't representable through today's
    BrokerClient.place_order() signature (no OrderType.STOP, no
    trigger_price parameter), so this adapter only implements MARKET/LIMIT.
  - get_account_info()/get_funds() are best-effort: no existing code in
    this repo exercises SmartAPI's getProfile()/rmsLimit() calls, so their
    exact response-field mapping is unverified against a live account.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pyotp

from trading.common.broker import (
    BrokerAuthenticationError,
    BrokerClient,
    BrokerConfigError,
    BrokerConnectionError,
    BrokerRateLimitError,
    LiveTradingDisabledError,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    Quote,
)
from trading.common.config import TradingConfig
from trading.common.execution import OrderState

_log = logging.getLogger(__name__)

# Angel's own order-status vocabulary (orderBook()'s "status" field, lowercased)
# mapped onto the generic vocabulary trading.common.execution understands
# (TERMINAL_STATUSES = {"FILLED", "COMPLETE", "REJECTED", "CANCELLED"}).
# Never let an Angel-specific string past this map.
_ANGEL_STATUS_MAP = {
    "complete": "COMPLETE",
    "open": "OPEN",
    "pending": "OPEN",
    "trigger pending": "OPEN",
    "modified": "OPEN",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
}

# Substrings observed in Angel's own exception/error text (see
# trading/algos/DoubleStraddelAlgo/broker/orders.py's _is_rate_limit_error)
# used to classify a raw SDK exception without leaking it upward as-is.
_RATE_LIMIT_MARKERS = ("exceeding access rate", "access denied")
_AUTH_ERROR_MARKERS = (
    "invalid totp",
    "invalid password",
    "invalid credential",
    "invalid api key",
    "session expired",
    "auth",
)

_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


@dataclass(frozen=True)
class AccountInfo:
    """Not part of the BrokerClient ABC -- an additional, optional
    capability on this adapter, in the same spirit as get_order/modify_order."""

    client_id: str
    name: str
    email: str


@dataclass(frozen=True)
class FundsSnapshot:
    """See AccountInfo -- optional, adapter-specific, not part of BrokerClient."""

    available_cash: float
    used_margin: float


class AngelOneBroker(BrokerClient):
    def __init__(
        self,
        config: TradingConfig,
        *,
        smart_api_factory: Callable[[str], Any] | None = None,
        instrument_resolver: Callable[[str], tuple[str, str]] | None = None,
    ) -> None:
        self._config = config
        # Both injectable purely for testability (see module docstring) --
        # production callers pass neither and get the real implementations.
        self._smart_api_factory = smart_api_factory or self._default_smart_api_factory
        self._instrument_resolver = instrument_resolver or self._default_instrument_resolver
        self._smart_api: Any = None
        self._connected = False
        self._feed_token: str = ""
        self._refresh_token: str = ""
        self._scrip_master_cache = None  # lazily loaded by _default_instrument_resolver

    def __repr__(self) -> str:
        # Never include self._config.credentials or any session token here.
        return f"AngelOneBroker(connected={self._connected})"

    # -- construction helpers (overridable for tests) ----------------------- #
    @staticmethod
    def _default_smart_api_factory(api_key: str) -> Any:
        try:
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise BrokerConfigError(
                "smartapi-python is not installed. Run: pip install smartapi-python"
            ) from exc
        return SmartConnect(api_key=api_key)

    def _default_instrument_resolver(self, symbol: str) -> tuple[str, str]:
        """Resolve an Angel tradingsymbol (e.g. 'NIFTY30JUL2625000CE', the
        same format trading/algos/DoubleStraddelAlgo/token_file.py produces)
        to (exchange, symboltoken) via Angel's public NFO/OPTIDX scrip
        master. Deliberately narrow -- see the module docstring's "Known
        limitations": this does not cover the NSE cash/index segment or any
        other exchange. A caller needing that must inject its own
        instrument_resolver.
        """
        if self._scrip_master_cache is None:
            import pandas as pd

            df = pd.read_json(_SCRIP_MASTER_URL)
            self._scrip_master_cache = df.loc[(df.exch_seg == "NFO") & (df.instrumenttype == "OPTIDX")]

        matches = self._scrip_master_cache.loc[self._scrip_master_cache["symbol"] == symbol]
        if matches.empty:
            raise ValueError(f"AngelOneBroker: symbol '{symbol}' not found in the NFO scrip master")
        return "NFO", str(matches.iloc[0]["token"])

    # -- error classification ------------------------------------------------ #
    def _classify_error(self, exc: Exception, context: str) -> Exception:
        """Convert a raw SmartAPI exception into the project's generic
        broker-error vocabulary. Never re-raise `exc` itself past this point."""
        text = str(exc).lower()
        if any(marker in text for marker in _RATE_LIMIT_MARKERS):
            return BrokerRateLimitError(f"AngelOne rate limit during {context}")
        if any(marker in text for marker in _AUTH_ERROR_MARKERS):
            return BrokerAuthenticationError(f"AngelOne authentication error during {context}")
        return BrokerConnectionError(f"AngelOne {context} failed")

    def _extract_data(self, response: Any, context: str, allow_empty: bool = False) -> Any:
        """Angel's REST endpoints (ltpData/orderBook/position/...) share a
        `{status, data, message}` envelope. placeOrder/cancelOrder do NOT --
        see place_order()/cancel_order(), which handle those separately."""
        if not isinstance(response, dict):
            raise BrokerConnectionError(f"AngelOne {context}: no response")
        if response.get("status") is False:
            raise BrokerConnectionError(f"AngelOne {context} failed: {response.get('message', 'unknown error')}")
        data = response.get("data")
        if data is None:
            if allow_empty:
                return []
            raise BrokerConnectionError(f"AngelOne {context}: empty data in response")
        return data

    def _require_connected(self) -> None:
        if not self._connected or self._smart_api is None:
            raise BrokerConnectionError("AngelOneBroker is not connected. Call connect() first.")

    def _fetch_order_row(self, order_id: str) -> dict | None:
        try:
            response = self._smart_api.orderBook()
        except Exception as exc:
            raise self._classify_error(exc, context="orderBook") from exc
        rows = self._extract_data(response, context="orderBook", allow_empty=True)
        for row in rows:
            if str(row.get("orderid")) == str(order_id):
                return row
        return None

    # -- BrokerClient: connection lifecycle ----------------------------------- #
    def connect(self) -> None:
        creds = self._config.credentials
        if not (
            creds.angelone_api_key
            and creds.angelone_client_id
            and creds.angelone_password
            and creds.angelone_totp_secret
        ):
            raise BrokerConfigError(
                "ANGELONE_API_KEY / ANGELONE_CLIENT_ID / ANGELONE_PASSWORD / "
                "ANGELONE_TOTP_SECRET not fully set."
            )

        smart_api = self._smart_api_factory(creds.angelone_api_key)
        totp = pyotp.TOTP(creds.angelone_totp_secret).now()

        try:
            session = smart_api.generateSession(creds.angelone_client_id, creds.angelone_password, totp)
        except Exception as exc:
            raise self._classify_error(exc, context="generateSession") from exc

        if not isinstance(session, dict) or not session.get("status"):
            message = session.get("message", "unknown error") if isinstance(session, dict) else "no response"
            raise BrokerAuthenticationError(f"AngelOne session creation failed: {message}")

        try:
            feed_token = smart_api.getfeedToken()
        except Exception as exc:
            raise self._classify_error(exc, context="getfeedToken") from exc

        self._smart_api = smart_api
        self._feed_token = feed_token or ""
        self._refresh_token = (session.get("data") or {}).get("refreshToken", "")
        self._connected = True
        _log.info("AngelOneBroker: session established")

    def disconnect(self) -> None:
        self._connected = False
        self._smart_api = None
        self._feed_token = ""
        self._refresh_token = ""

    def is_connected(self) -> bool:
        return self._connected

    # -- BrokerClient: market data --------------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        exchange, symboltoken = self._instrument_resolver(symbol)
        try:
            response = self._smart_api.ltpData(exchange, symbol, symboltoken)
        except Exception as exc:
            raise self._classify_error(exc, context="get_quote") from exc
        data = self._extract_data(response, context="get_quote")
        try:
            last_price = float(data["ltp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerConnectionError(f"AngelOne get_quote: malformed response for '{symbol}'") from exc
        return Quote(symbol=symbol, last_price=last_price, timestamp=datetime.now(timezone.utc).isoformat())

    # -- BrokerClient: orders --------------------------------------------------- #
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
        self._require_connected()

        if order_type == OrderType.LIMIT and limit_price is None:
            raise ValueError("AngelOneBroker.place_order: LIMIT order requires limit_price")

        exchange, symboltoken = self._instrument_resolver(symbol)
        params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": symboltoken,
            "transactiontype": side.value,
            "exchange": exchange,
            "ordertype": order_type.value,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(limit_price) if order_type == OrderType.LIMIT else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }
        try:
            order_id = self._smart_api.placeOrder(params)
        except Exception as exc:
            raise self._classify_error(exc, context="place_order") from exc

        if not order_id:
            return OrderResult(
                order_id="", symbol=symbol, side=side, quantity=quantity,
                status="REJECTED", message="AngelOne returned no order id",
            )
        # Angel's placeOrder only confirms acceptance, not a fill -- "OPEN"
        # (non-terminal) is the honest status; StrategyExecutionEngine polls
        # get_order() below for the real outcome.
        return OrderResult(order_id=str(order_id), symbol=symbol, side=side, quantity=quantity, status="OPEN")

    def cancel_order(self, order_id: str) -> bool:
        if not self._config.is_live:
            raise LiveTradingDisabledError("Refusing to cancel a real AngelOne order: TRADING_MODE is not 'live'.")
        self._require_connected()
        try:
            response = self._smart_api.cancelOrder(str(order_id), "NORMAL")
        except Exception as exc:
            raise self._classify_error(exc, context="cancel_order") from exc
        return bool(response)

    def get_positions(self) -> list[Position]:
        self._require_connected()
        try:
            response = self._smart_api.position()
        except Exception as exc:
            raise self._classify_error(exc, context="get_positions") from exc
        rows = self._extract_data(response, context="get_positions", allow_empty=True)

        positions: list[Position] = []
        for row in rows:
            try:
                realised = float(row.get("realised", 0) or 0)
                unrealised = float(row.get("unrealised", 0) or 0)
                pnl = float(row.get("pnl")) if row.get("pnl") not in (None, "") else realised + unrealised
                positions.append(
                    Position(
                        symbol=row["tradingsymbol"],
                        quantity=int(float(row.get("netqty", 0) or 0)),
                        average_price=float(row.get("avgnetprice", 0) or 0),
                        last_price=float(row.get("ltp", 0) or 0),
                        pnl=pnl,
                    )
                )
            except (KeyError, TypeError, ValueError):
                _log.warning("AngelOneBroker.get_positions: skipping malformed position row")
        return positions

    # -- optional hooks StrategyExecutionEngine duck-types for -------------------- #
    def get_order(self, order_id: str) -> OrderState:
        row = self._fetch_order_row(order_id)
        if row is None:
            return OrderState(order_id=str(order_id), status="UNKNOWN", filled_quantity=0, remaining_quantity=0)

        quantity = int(float(row.get("quantity", 0) or 0))
        filled = int(float(row.get("filledshares", 0) or 0))
        status = _ANGEL_STATUS_MAP.get(str(row.get("status", "")).strip().lower(), "OPEN")
        return OrderState(
            order_id=str(order_id), status=status, filled_quantity=filled,
            remaining_quantity=max(quantity - filled, 0),
        )

    def modify_order(self, order_id: str, quantity: int, limit_price: float) -> bool:
        self._require_connected()
        row = self._fetch_order_row(order_id)
        if row is None:
            raise BrokerConnectionError(f"AngelOne modify_order: order '{order_id}' not found in order book")

        params = {
            "variety": "NORMAL",
            "orderid": str(order_id),
            "tradingsymbol": row.get("tradingsymbol"),
            "symboltoken": row.get("symboltoken"),
            "exchange": row.get("exchange"),
            "ordertype": "LIMIT",
            "producttype": row.get("producttype", "INTRADAY"),
            "duration": "DAY",
            "price": str(limit_price),
            "quantity": str(quantity),
        }
        try:
            response = self._smart_api.modifyOrder(params)
        except Exception as exc:
            raise self._classify_error(exc, context="modify_order") from exc
        return bool(response)

    # -- optional, adapter-specific capabilities (not part of BrokerClient) ------ #
    def get_account_info(self) -> AccountInfo:
        self._require_connected()
        try:
            response = self._smart_api.getProfile(self._refresh_token)
        except Exception as exc:
            raise self._classify_error(exc, context="get_account_info") from exc
        data = self._extract_data(response, context="get_account_info")
        return AccountInfo(
            client_id=str(data.get("clientcode", "")),
            name=str(data.get("name", "")),
            email=str(data.get("email", "")),
        )

    def get_funds(self) -> FundsSnapshot:
        self._require_connected()
        try:
            response = self._smart_api.rmsLimit()
        except Exception as exc:
            raise self._classify_error(exc, context="get_funds") from exc
        data = self._extract_data(response, context="get_funds")
        return FundsSnapshot(
            available_cash=float(data.get("availablecash", 0) or 0),
            used_margin=float(data.get("utiliseddebits", 0) or 0),
        )
