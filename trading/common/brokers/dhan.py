"""
Dhan broker adapter -- a real implementation of BrokerClient, following
the EXACT same structure and safety posture as
trading.common.brokers.angelone.AngelOneBroker (Phase 2/4/5A): isolated
behind BrokerClient, lazy SDK import, injectable factory/resolver for
testability, read-only guard checked first in every mutating method,
error classification into the shared BrokerConnectionError/
BrokerAuthenticationError/BrokerRateLimitError vocabulary, and the same
optional adapter-specific extras (resolve_instrument, get_account_info,
get_funds, get_order_book, get_open_orders).

Dhan's authentication model differs from Angel's: there is no TOTP/MPIN
login flow to call here. A Dhan access token is generated externally
(Dhan's web console or partner OAuth flow) and supplied as a long-lived
credential (DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN); this adapter's connect()
verifies that token is still valid by calling a lightweight authenticated
read endpoint (fund limits) -- there is nothing to "generate a session"
from, only a token to confirm.

CRITICAL, per Phase 8's explicit instruction ("do NOT enable real Dhan
order placement yet"): unlike AngelOneBroker (whose read_only flag
defaults to False, relying on the separate TRADING_MODE gate),
DhanBroker's read_only flag DEFAULTS TO TRUE. Enabling real Dhan order
placement requires BOTH an explicit read_only=False (or DHAN_READ_ONLY=false
in the environment) AND TRADING_MODE=live -- two independent, both-explicit
opt-ins, matching the elevated caution this brand-new, unvalidated adapter
warrants.

Known limitation -- UNVERIFIED against a real Dhan account (no real Dhan
credentials have been used against this adapter; every test uses a fake
Dhan client double): the exact response-field names for get_fund_limits(),
get_positions(), get_order_list(), and the public scrip-master CSV columns
are modeled on DhanHQ v2's publicly documented REST shape from general
knowledge, not verified live. This mirrors the exact same caveat Phase 2
already carries for AngelOneBroker.get_account_info()/get_funds().
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

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
    ReadOnlyModeError,
)
from trading.common.config import TradingConfig
from trading.common.execution import TERMINAL_STATUSES, OrderState

_log = logging.getLogger(__name__)

# Dhan's own order-status vocabulary (best-effort, unverified -- see module
# docstring) mapped onto the generic vocabulary trading.common.execution
# understands. Never let a Dhan-specific string past this map.
_DHAN_STATUS_MAP = {
    "traded": "COMPLETE",
    "complete": "COMPLETE",
    "pending": "OPEN",
    "transit": "OPEN",
    "open": "OPEN",
    "modified": "OPEN",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "expired": "CANCELLED",
}

_RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "dh-909")
_AUTH_ERROR_MARKERS = ("invalid_authentication", "invalid access token", "dh-901", "unauthorized", "token expired")

_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_DEFAULT_OPTION_EXCHANGE_SEGMENT = "NSE_FNO"


def _read_only_enabled_by_env() -> bool:
    """DHAN_READ_ONLY defaults to TRUE (unlike Angel's ANGEL_READ_ONLY,
    which defaults to False) -- see the module docstring's CRITICAL note.
    Must be set to a falsy value explicitly to disable."""
    val = os.environ.get("DHAN_READ_ONLY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class AccountInfo:
    """Not part of the BrokerClient ABC -- optional, adapter-specific,
    matching AngelOneBroker's own AccountInfo shape."""

    client_id: str
    name: str
    email: str


@dataclass(frozen=True)
class FundsSnapshot:
    available_cash: float
    used_margin: float


class DhanBroker(BrokerClient):
    def __init__(
        self,
        config: TradingConfig,
        *,
        dhan_client_factory: Callable[[str, str], Any] | None = None,
        instrument_resolver: Callable[[str], tuple[str, str]] | None = None,
        read_only: bool | None = None,
    ) -> None:
        self._config = config
        self._dhan_client_factory = dhan_client_factory or self._default_dhan_client_factory
        self._instrument_resolver = instrument_resolver or self._default_instrument_resolver
        self._dhan_client: Any = None
        self._connected = False
        self._scrip_master_cache = None
        self._read_only = read_only if read_only is not None else _read_only_enabled_by_env()

    def __repr__(self) -> str:
        return f"DhanBroker(connected={self._connected}, read_only={self._read_only})"

    @property
    def is_read_only(self) -> bool:
        return self._read_only

    # -- construction helpers (overridable for tests) ----------------------- #
    @staticmethod
    def _default_dhan_client_factory(client_id: str, access_token: str) -> Any:
        try:
            from dhanhq import dhanhq
        except ImportError as exc:
            raise BrokerConfigError("dhanhq is not installed. Run: pip install dhanhq") from exc
        return dhanhq(client_id, access_token)

    def _default_instrument_resolver(self, symbol: str) -> tuple[str, str]:
        """Resolve a trading symbol to (exchange_segment, security_id) via
        Dhan's public scrip-master CSV. UNVERIFIED -- see module docstring.
        Deliberately narrow, matching AngelOneBroker's own documented
        limitation: this does not attempt to cover every segment/exchange,
        only NSE F&O (options), which is all today's live algos need."""
        if self._scrip_master_cache is None:
            import pandas as pd

            df = pd.read_csv(_SCRIP_MASTER_URL)
            self._scrip_master_cache = df

        df = self._scrip_master_cache
        matches = df.loc[df["SEM_TRADING_SYMBOL"] == symbol] if "SEM_TRADING_SYMBOL" in df.columns else df.iloc[0:0]
        if matches.empty:
            raise ValueError(f"DhanBroker: symbol '{symbol}' not found in the scrip master")
        row = matches.iloc[0]
        return _DEFAULT_OPTION_EXCHANGE_SEGMENT, str(row["SEM_SMST_SECURITY_ID"])

    # -- error classification ------------------------------------------------ #
    def _classify_error(self, exc: Exception, context: str) -> Exception:
        text = str(exc).lower()
        if any(marker in text for marker in _RATE_LIMIT_MARKERS):
            return BrokerRateLimitError(f"Dhan rate limit during {context}")
        if any(marker in text for marker in _AUTH_ERROR_MARKERS):
            return BrokerAuthenticationError(f"Dhan authentication error during {context}")
        return BrokerConnectionError(f"Dhan {context} failed")

    def _extract_payload(self, response: Any, context: str, allow_empty: bool = False) -> Any:
        """Dhan's SDK/REST responses are less uniformly enveloped than
        Angel's -- some calls return the payload directly (a list/dict),
        others wrap it as {"status": "success"|"failure", "data": ...,
        "remarks": ...}. Handle both shapes defensively."""
        if response is None:
            if allow_empty:
                return []
            raise BrokerConnectionError(f"Dhan {context}: no response")
        if isinstance(response, dict) and "status" in response:
            if str(response.get("status")).lower() == "failure":
                remarks = response.get("remarks") or response.get("message") or "unknown error"
                raise BrokerConnectionError(f"Dhan {context} failed: {remarks}")
            data = response.get("data")
            if data is None:
                return [] if allow_empty else response
            return data
        return response

    def _require_connected(self) -> None:
        if not self._connected or self._dhan_client is None:
            raise BrokerConnectionError("DhanBroker is not connected. Call connect() first.")

    def _all_order_rows(self) -> list[dict]:
        try:
            response = self._dhan_client.get_order_list()
        except Exception as exc:
            raise self._classify_error(exc, context="get_order_list") from exc
        return self._extract_payload(response, context="get_order_list", allow_empty=True) or []

    def _fetch_order_row(self, order_id: str) -> dict | None:
        for row in self._all_order_rows():
            if str(row.get("orderId")) == str(order_id):
                return row
        return None

    def _require_not_read_only(self, operation: str) -> None:
        if self._read_only:
            raise ReadOnlyModeError(f"DhanBroker is in read-only mode (DHAN_READ_ONLY): {operation}() is blocked.")

    # -- BrokerClient: connection lifecycle ----------------------------------- #
    def connect(self) -> None:
        creds = self._config.credentials
        if not (creds.dhan_client_id and creds.dhan_access_token):
            raise BrokerConfigError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not fully set.")

        client = self._dhan_client_factory(creds.dhan_client_id, creds.dhan_access_token)

        # No login/session-generation call exists for Dhan (see module
        # docstring) -- verify the supplied access token is still valid by
        # calling a lightweight authenticated read endpoint instead.
        try:
            response = client.get_fund_limits()
        except Exception as exc:
            raise self._classify_error(exc, context="get_fund_limits (auth check)") from exc

        if isinstance(response, dict) and str(response.get("status", "")).lower() == "failure":
            remarks = response.get("remarks") or response.get("message") or "unknown error"
            raise BrokerAuthenticationError(f"Dhan token validation failed: {remarks}")

        self._dhan_client = client
        self._connected = True
        _log.info("DhanBroker: access token validated, session established")

    def disconnect(self) -> None:
        self._connected = False
        self._dhan_client = None

    def is_connected(self) -> bool:
        return self._connected

    # -- BrokerClient: market data --------------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        exchange_segment, security_id = self._instrument_resolver(symbol)
        try:
            response = self._dhan_client.ticker_data(
                securities={exchange_segment: [int(security_id)]} if security_id.isdigit() else {exchange_segment: [security_id]}
            )
        except Exception as exc:
            raise self._classify_error(exc, context="get_quote") from exc
        data = self._extract_payload(response, context="get_quote")
        try:
            segment_data = data[exchange_segment] if isinstance(data, dict) and exchange_segment in data else data
            row = segment_data[security_id] if isinstance(segment_data, dict) else segment_data
            last_price = float(row["last_price"]) if isinstance(row, dict) else float(row)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise BrokerConnectionError(f"Dhan get_quote: malformed response for '{symbol}'") from exc
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
        self._require_not_read_only("place_order")
        if not self._config.is_live:
            raise LiveTradingDisabledError(
                "Refusing to place a real Dhan order: TRADING_MODE is not 'live'. Use BROKER=paper for development/testing."
            )
        self._require_connected()

        if order_type == OrderType.LIMIT and limit_price is None:
            raise ValueError("DhanBroker.place_order: LIMIT order requires limit_price")

        exchange_segment, security_id = self._instrument_resolver(symbol)
        params = {
            "security_id": security_id,
            "exchange_segment": exchange_segment,
            "transaction_type": side.value,
            "quantity": quantity,
            "order_type": order_type.value,
            "product_type": "INTRADAY",
            "price": limit_price if order_type == OrderType.LIMIT else 0,
            "validity": "DAY",
        }
        try:
            response = self._dhan_client.place_order(**params)
        except Exception as exc:
            raise self._classify_error(exc, context="place_order") from exc

        data = self._extract_payload(response, context="place_order") if isinstance(response, dict) and "status" in response else response
        order_id = data.get("orderId") if isinstance(data, dict) else None

        if not order_id:
            return OrderResult(order_id="", symbol=symbol, side=side, quantity=quantity, status="REJECTED", message="Dhan returned no order id")
        # Dhan's place_order only confirms acceptance (typically "PENDING"/
        # "TRANSIT"), not a fill -- "OPEN" is the honest status;
        # StrategyExecutionEngine polls get_order() below for the outcome.
        return OrderResult(order_id=str(order_id), symbol=symbol, side=side, quantity=quantity, status="OPEN")

    def cancel_order(self, order_id: str) -> bool:
        self._require_not_read_only("cancel_order")
        if not self._config.is_live:
            raise LiveTradingDisabledError("Refusing to cancel a real Dhan order: TRADING_MODE is not 'live'.")
        self._require_connected()
        try:
            response = self._dhan_client.cancel_order(order_id)
        except Exception as exc:
            raise self._classify_error(exc, context="cancel_order") from exc
        if isinstance(response, dict) and str(response.get("status", "")).lower() == "failure":
            return False
        return True

    def get_positions(self) -> list[Position]:
        self._require_connected()
        try:
            response = self._dhan_client.get_positions()
        except Exception as exc:
            raise self._classify_error(exc, context="get_positions") from exc
        rows = self._extract_payload(response, context="get_positions", allow_empty=True) or []

        positions: list[Position] = []
        for row in rows:
            try:
                realised = float(row.get("realizedProfit", 0) or 0)
                unrealised = float(row.get("unrealizedProfit", 0) or 0)
                positions.append(
                    Position(
                        symbol=row["tradingSymbol"],
                        quantity=int(float(row.get("netQty", 0) or 0)),
                        average_price=float(row.get("costPrice", 0) or 0),
                        last_price=float(row.get("lastTradedPrice", 0) or 0),
                        pnl=realised + unrealised,
                    )
                )
            except (KeyError, TypeError, ValueError):
                _log.warning("DhanBroker.get_positions: skipping malformed position row")
        return positions

    def resolve_instrument(self, symbol: str) -> tuple[str, str]:
        """Public wrapper over this adapter's Dhan-specific resolution --
        not part of BrokerClient. See AngelOneBroker.resolve_instrument()."""
        return self._instrument_resolver(symbol)

    def get_order_book(self) -> list[OrderState]:
        self._require_connected()
        states = []
        for row in self._all_order_rows():
            order_id = str(row.get("orderId", ""))
            quantity = int(float(row.get("quantity", 0) or 0))
            filled = int(float(row.get("filledQty", 0) or 0))
            status = _DHAN_STATUS_MAP.get(str(row.get("orderStatus", "")).strip().lower(), "OPEN")
            states.append(OrderState(order_id=order_id, status=status, filled_quantity=filled, remaining_quantity=max(quantity - filled, 0)))
        return states

    def get_open_orders(self) -> list[OrderState]:
        return [state for state in self.get_order_book() if state.status not in TERMINAL_STATUSES]

    # -- optional hooks StrategyExecutionEngine duck-types for -------------------- #
    def get_order(self, order_id: str) -> OrderState:
        row = self._fetch_order_row(order_id)
        if row is None:
            return OrderState(order_id=str(order_id), status="UNKNOWN", filled_quantity=0, remaining_quantity=0)
        quantity = int(float(row.get("quantity", 0) or 0))
        filled = int(float(row.get("filledQty", 0) or 0))
        status = _DHAN_STATUS_MAP.get(str(row.get("orderStatus", "")).strip().lower(), "OPEN")
        return OrderState(order_id=str(order_id), status=status, filled_quantity=filled, remaining_quantity=max(quantity - filled, 0))

    def modify_order(self, order_id: str, quantity: int, limit_price: float) -> bool:
        self._require_not_read_only("modify_order")
        self._require_connected()
        row = self._fetch_order_row(order_id)
        if row is None:
            raise BrokerConnectionError(f"Dhan modify_order: order '{order_id}' not found in order book")
        try:
            response = self._dhan_client.modify_order(
                order_id=order_id, order_type=row.get("orderType", "LIMIT"),
                quantity=quantity, price=limit_price, validity=row.get("validity", "DAY"),
            )
        except Exception as exc:
            raise self._classify_error(exc, context="modify_order") from exc
        if isinstance(response, dict) and str(response.get("status", "")).lower() == "failure":
            return False
        return True

    # -- optional, adapter-specific capabilities (not part of BrokerClient) ------ #
    def get_account_info(self) -> AccountInfo:
        self._require_connected()
        try:
            response = self._dhan_client.get_fund_limits()
        except Exception as exc:
            raise self._classify_error(exc, context="get_account_info") from exc
        data = self._extract_payload(response, context="get_account_info")
        return AccountInfo(
            client_id=str(data.get("dhanClientId", self._config.credentials.dhan_client_id)),
            name=str(data.get("name", "")),
            email=str(data.get("email", "")),
        )

    def get_funds(self) -> FundsSnapshot:
        self._require_connected()
        try:
            response = self._dhan_client.get_fund_limits()
        except Exception as exc:
            raise self._classify_error(exc, context="get_funds") from exc
        data = self._extract_payload(response, context="get_funds")
        return FundsSnapshot(
            available_cash=float(data.get("availableBalance", 0) or 0),
            used_margin=float(data.get("utilizedAmount", 0) or 0),
        )
