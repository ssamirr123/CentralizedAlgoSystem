"""
ICICI Direct Breeze API adapter -- a real implementation of BrokerClient,
following the EXACT same structure and safety posture as
trading.common.brokers.angelone.AngelOneBroker (Phase 2/4/5A) and
trading.common.brokers.dhan.DhanBroker (Phase 8): isolated behind
BrokerClient, lazy SDK import, injectable factory/resolver for
testability, read-only guard checked first in every mutating method,
error classification into the shared BrokerConnectionError/
BrokerAuthenticationError/BrokerRateLimitError vocabulary, and the same
optional adapter-specific extras (resolve_instrument, get_account_info,
get_funds, get_order_book, get_open_orders, get_order, modify_order).

This is the ONLY BrokerClient-facing file in the project allowed to know
that "ICICI Breeze" means BreezeConnect/generate_session, stock_code/
exchange_code/right/strike_price request fields, or Breeze's own
{"Success": ..., "Status": ..., "Error": ...} response envelope.
Everything above BrokerClient only ever sees the generic Quote/OrderResult/
Position types.

There is a SEPARATE, already-real, READ-ONLY market-data integration at
trading/market_data/providers/icici_breeze.py (a MarketDataProvider, not a
BrokerClient) that this file deliberately does NOT import from or depend
on -- different layer, different env-var convention (BREEZE_* there vs
ICICI_BREEZE_* here, matching this repo's existing BrokerCredentials
fields), different concerns (streaming/candles/option-chain vs order
placement). It was consulted only as a reference for how Breeze's real
auth/response shape behaves (BreezeConnect(api_key) -> generate_session
(api_secret, session_token), and the {"Success"/"Status"/"Error"} envelope)
-- duplicating that tiny amount of protocol knowledge here keeps the two
layers independently maintainable, exactly as Angel/Dhan already are kept
independent of each other.

Symbol resolution: unlike Angel/Dhan (whose real order APIs address an
option by an opaque numeric token/security-id that must be looked up
against a downloaded scrip/security master), Breeze's own API addresses an
option contract by giving (stock_code, expiry_date, right, strike_price)
directly as separate parameters -- there is no separate token to resolve.
The default instrument_resolver therefore just PARSES the same Angel-style
tradingsymbol string OrderIntent.symbol already carries everywhere in this
repo (e.g. "NIFTY15SEP2623400CE", built by
trading/algos/DoubleStraddelAlgo/token_file.py's token_nifty()) directly
into those fields -- no network call, no security-master download needed
for this. Deliberately narrow, matching Angel/Dhan's own documented
limitation: NSE F&O index options only.

CRITICAL, per Phase 9's explicit instruction ("no real orders"): matching
DhanBroker's elevated-caution posture for a brand-new, unvalidated
adapter (rather than AngelOneBroker's, which defaults open), this
adapter's read_only flag DEFAULTS TO TRUE. Enabling real ICICI Breeze
order placement requires BOTH an explicit read_only=False (or
ICICI_BREEZE_READ_ONLY=false in the environment) AND TRADING_MODE=live --
two independent, both-explicit opt-ins.

Known limitation -- UNVERIFIED against a real ICICI Breeze account (no
real Breeze credentials have been used against this adapter; every test
uses a fake Breeze client double): the exact response-field names for
get_portfolio_positions(), get_order_list(), get_funds(), and
get_customer_details() are modeled on Breeze's publicly documented REST
shape from general knowledge, not verified live. This mirrors the exact
same caveat Phase 2/Phase 8 already carry for AngelOneBroker/DhanBroker's
get_account_info()/get_funds().
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
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

# Breeze's own order-status vocabulary (best-effort, unverified -- see
# module docstring) mapped onto the generic vocabulary
# trading.common.execution understands. Never let a Breeze-specific string
# past this map.
_BREEZE_STATUS_MAP = {
    "executed": "COMPLETE",
    "fully executed": "COMPLETE",
    "complete": "COMPLETE",
    "ordered": "OPEN",
    "open": "OPEN",
    "pending": "OPEN",
    "requested": "OPEN",
    "modified": "OPEN",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "expired": "CANCELLED",
}

_RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "throttl")
_AUTH_ERROR_MARKERS = (
    "invalid session",
    "session expired",
    "invalid api",
    "authentication fail",
    "unauthorized",
    "public key",
)

# Matches the Angel-style tradingsymbol format this repo uses everywhere
# for NIFTY options, e.g. "NIFTY15SEP2623400CE" -- underlying (letters) +
# DDMMMYY expiry code + strike (digits, optionally decimal) + CE/PE. See
# the module docstring's "Symbol resolution" section.
_SYMBOL_RE = re.compile(r"^([A-Z]+?)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$")


def _read_only_enabled_by_env() -> bool:
    """ICICI_BREEZE_READ_ONLY defaults to TRUE -- the same elevated-caution
    posture as DhanBroker's DHAN_READ_ONLY (a brand-new, unvalidated
    adapter), unlike AngelOneBroker's ANGEL_READ_ONLY which defaults to
    False. Must be set to a falsy value explicitly to disable."""
    val = os.environ.get("ICICI_BREEZE_READ_ONLY", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def _breeze_expiry(d: date) -> str:
    """Breeze expects an ISO datetime string for expiry_date/from_date/to_date."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _strike_str(strike: float) -> str:
    """Breeze builds its contract key by string concatenation, so a strike
    must be "23400" not "23400.0" (matches the existing market-data
    provider's own _strike_str, duplicated here to keep the two layers
    independent -- see module docstring)."""
    return f"{strike:g}"


@dataclass(frozen=True)
class BreezeInstrument:
    """Not part of the BrokerClient ABC. The richer instrument-
    identification shape Breeze's option API needs -- see the module
    docstring's "Symbol resolution" section for why this is a small
    dataclass rather than Angel/Dhan's flat (exchange, token) tuple."""

    stock_code: str
    exchange_code: str
    product_type: str
    expiry_date: str  # ISO datetime string, Breeze's expected wire format
    right: str  # "call" | "put"
    strike_price: str


@dataclass(frozen=True)
class AccountInfo:
    """Not part of the BrokerClient ABC -- optional, adapter-specific,
    matching AngelOneBroker/DhanBroker's own AccountInfo shape."""

    client_id: str
    name: str
    email: str


@dataclass(frozen=True)
class FundsSnapshot:
    available_cash: float
    used_margin: float


class ICICIBreezeBroker(BrokerClient):
    def __init__(
        self,
        config: TradingConfig,
        *,
        breeze_client_factory: Callable[[str], Any] | None = None,
        instrument_resolver: Callable[[str], BreezeInstrument] | None = None,
        read_only: bool | None = None,
    ) -> None:
        self._config = config
        # Both injectable purely for testability (see module docstring) --
        # production callers pass neither and get the real implementations.
        self._breeze_client_factory = breeze_client_factory or self._default_breeze_client_factory
        self._instrument_resolver = instrument_resolver or self._default_instrument_resolver
        self._breeze_client: Any = None
        self._connected = False
        # None -> resolved from ICICI_BREEZE_READ_ONLY at construction time.
        # An explicit True/False always wins over the environment. Checked
        # BEFORE is_live in every mutating method below, so it can never be
        # bypassed by TRADING_MODE=live -- see ReadOnlyModeError's docstring.
        self._read_only = read_only if read_only is not None else _read_only_enabled_by_env()

    def __repr__(self) -> str:
        # Never include self._config.credentials or any session token here.
        return f"ICICIBreezeBroker(connected={self._connected}, read_only={self._read_only})"

    @property
    def is_read_only(self) -> bool:
        return self._read_only

    # -- construction helpers (overridable for tests) ----------------------- #
    @staticmethod
    def _default_breeze_client_factory(api_key: str) -> Any:
        try:
            from breeze_connect import BreezeConnect
        except ImportError as exc:
            raise BrokerConfigError(
                "breeze-connect is not installed. Run: pip install breeze-connect"
            ) from exc
        return BreezeConnect(api_key=api_key)

    def _default_instrument_resolver(self, symbol: str) -> BreezeInstrument:
        """Parse an Angel-style tradingsymbol directly into Breeze's
        descriptive option-identification fields. See the module
        docstring's "Symbol resolution" section for why no network/
        security-master lookup is needed for this, unlike Angel/Dhan."""
        match = _SYMBOL_RE.match(symbol.strip().upper())
        if not match:
            raise ValueError(f"ICICIBreezeBroker: could not parse option symbol '{symbol}'")
        underlying, date_code, strike_str, option_type = match.groups()
        try:
            expiry = datetime.strptime(date_code, "%d%b%y").date()
        except ValueError as exc:
            raise ValueError(
                f"ICICIBreezeBroker: bad expiry code '{date_code}' in symbol '{symbol}'"
            ) from exc
        right = "call" if option_type == "CE" else "put"
        return BreezeInstrument(
            stock_code=underlying,
            exchange_code="NFO",
            product_type="options",
            expiry_date=_breeze_expiry(expiry),
            right=right,
            strike_price=_strike_str(float(strike_str)),
        )

    # -- error classification ------------------------------------------------ #
    def _classify_error(self, exc: Exception, context: str) -> Exception:
        """Convert a raw Breeze exception into the project's generic
        broker-error vocabulary. Never re-raise `exc` itself past this point."""
        text = str(exc).lower()
        if any(marker in text for marker in _RATE_LIMIT_MARKERS):
            return BrokerRateLimitError(f"ICICI Breeze rate limit during {context}")
        if any(marker in text for marker in _AUTH_ERROR_MARKERS):
            return BrokerAuthenticationError(f"ICICI Breeze authentication error during {context}")
        return BrokerConnectionError(f"ICICI Breeze {context} failed")

    def _extract_data(self, response: Any, context: str, allow_empty: bool = False) -> Any:
        """Breeze wraps payloads as {"Success": <data>, "Status": <int>,
        "Error": <str|None>} -- see trading/market_data/providers/
        icici_breeze.py's own _unwrap(), which this mirrors (independently,
        see module docstring)."""
        if not isinstance(response, dict):
            raise BrokerConnectionError(f"ICICI Breeze {context}: no response")
        err = response.get("Error")
        if err:
            raise BrokerConnectionError(f"ICICI Breeze {context} failed: {err}")
        data = response.get("Success")
        if data is None:
            if allow_empty:
                return []
            raise BrokerConnectionError(f"ICICI Breeze {context}: empty data in response")
        return data

    def _require_connected(self) -> None:
        if not self._connected or self._breeze_client is None:
            raise BrokerConnectionError("ICICIBreezeBroker is not connected. Call connect() first.")

    def _all_order_rows(self) -> list[dict]:
        today = _breeze_expiry(date.today())
        try:
            response = self._breeze_client.get_order_list(
                exchange_code="NFO", from_date=today, to_date=today
            )
        except Exception as exc:
            raise self._classify_error(exc, context="get_order_list") from exc
        return self._extract_data(response, context="get_order_list", allow_empty=True) or []

    def _fetch_order_row(self, order_id: str) -> dict | None:
        for row in self._all_order_rows():
            if str(row.get("order_id")) == str(order_id):
                return row
        return None

    def _require_not_read_only(self, operation: str) -> None:
        if self._read_only:
            raise ReadOnlyModeError(
                f"ICICIBreezeBroker is in read-only mode (ICICI_BREEZE_READ_ONLY): {operation}() is blocked."
            )

    # -- BrokerClient: connection lifecycle ----------------------------------- #
    def connect(self) -> None:
        creds = self._config.credentials
        if not (creds.icici_breeze_api_key and creds.icici_breeze_api_secret and creds.icici_breeze_session_token):
            raise BrokerConfigError(
                "ICICI_BREEZE_API_KEY / ICICI_BREEZE_API_SECRET / ICICI_BREEZE_SESSION_TOKEN "
                "not fully set. Generate a session token via the Breeze login flow first."
            )

        client = self._breeze_client_factory(creds.icici_breeze_api_key)
        try:
            client.generate_session(
                api_secret=creds.icici_breeze_api_secret, session_token=creds.icici_breeze_session_token
            )
        except Exception as exc:
            raise self._classify_error(exc, context="generate_session") from exc

        self._breeze_client = client
        self._connected = True
        _log.info("ICICIBreezeBroker: session established")

    def disconnect(self) -> None:
        self._connected = False
        self._breeze_client = None

    def is_connected(self) -> bool:
        return self._connected

    # -- BrokerClient: market data --------------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        self._require_connected()
        inst = self._instrument_resolver(symbol)
        try:
            response = self._breeze_client.get_quotes(
                stock_code=inst.stock_code, exchange_code=inst.exchange_code, product_type=inst.product_type,
                expiry_date=inst.expiry_date, right=inst.right, strike_price=inst.strike_price,
            )
        except Exception as exc:
            raise self._classify_error(exc, context="get_quote") from exc
        data = self._extract_data(response, context="get_quote")
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            raise BrokerConnectionError(f"ICICI Breeze get_quote: malformed response for '{symbol}'")
        try:
            last_price = float(row.get("ltp") if row.get("ltp") not in (None, "") else row.get("last"))
        except (TypeError, ValueError) as exc:
            raise BrokerConnectionError(f"ICICI Breeze get_quote: malformed response for '{symbol}'") from exc
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
                "Refusing to place a real ICICI Breeze order: TRADING_MODE is not 'live'. "
                "Use BROKER=paper for development/testing."
            )
        self._require_connected()

        if order_type == OrderType.LIMIT and limit_price is None:
            raise ValueError("ICICIBreezeBroker.place_order: LIMIT order requires limit_price")

        inst = self._instrument_resolver(symbol)
        params = {
            "stock_code": inst.stock_code,
            "exchange_code": inst.exchange_code,
            "product": inst.product_type,
            "action": "buy" if side == OrderSide.BUY else "sell",
            "order_type": "market" if order_type == OrderType.MARKET else "limit",
            "stoploss": "0",
            "quantity": str(quantity),
            "price": str(limit_price) if order_type == OrderType.LIMIT else "0",
            "validity": "day",
            "disclosed_quantity": "0",
            "expiry_date": inst.expiry_date,
            "right": inst.right,
            "strike_price": inst.strike_price,
        }
        try:
            response = self._breeze_client.place_order(**params)
        except Exception as exc:
            raise self._classify_error(exc, context="place_order") from exc

        data = self._extract_data(response, context="place_order")
        order_id = data.get("order_id") if isinstance(data, dict) else None
        if not order_id:
            return OrderResult(
                order_id="", symbol=symbol, side=side, quantity=quantity,
                status="REJECTED", message="ICICI Breeze returned no order id",
            )
        # Breeze's place_order only confirms acceptance, not a fill -- "OPEN"
        # (non-terminal) is the honest status; StrategyExecutionEngine polls
        # get_order() below for the real outcome.
        return OrderResult(order_id=str(order_id), symbol=symbol, side=side, quantity=quantity, status="OPEN")

    def cancel_order(self, order_id: str) -> bool:
        self._require_not_read_only("cancel_order")
        if not self._config.is_live:
            raise LiveTradingDisabledError("Refusing to cancel a real ICICI Breeze order: TRADING_MODE is not 'live'.")
        self._require_connected()
        try:
            response = self._breeze_client.cancel_order(exchange_code="NFO", order_id=str(order_id))
        except Exception as exc:
            raise self._classify_error(exc, context="cancel_order") from exc
        if isinstance(response, dict) and response.get("Error"):
            return False
        return True

    def get_positions(self) -> list[Position]:
        self._require_connected()
        try:
            response = self._breeze_client.get_portfolio_positions()
        except Exception as exc:
            raise self._classify_error(exc, context="get_positions") from exc
        rows = self._extract_data(response, context="get_positions", allow_empty=True) or []

        positions: list[Position] = []
        for row in rows:
            try:
                realised = float(row.get("realized_profit", 0) or 0)
                unrealised = float(row.get("unrealized_profit", 0) or 0)
                positions.append(
                    Position(
                        symbol=row["stock_code"],
                        quantity=int(float(row.get("quantity", 0) or 0)),
                        average_price=float(row.get("average_price", 0) or 0),
                        last_price=float(row.get("ltp", 0) or 0),
                        pnl=realised + unrealised,
                    )
                )
            except (KeyError, TypeError, ValueError):
                _log.warning("ICICIBreezeBroker.get_positions: skipping malformed position row")
        return positions

    def resolve_instrument(self, symbol: str) -> BreezeInstrument:
        """Public wrapper over this adapter's Breeze-specific instrument
        resolution -- not part of BrokerClient. See
        AngelOneBroker.resolve_instrument()/DhanBroker.resolve_instrument()."""
        return self._instrument_resolver(symbol)

    def get_order_book(self) -> list[OrderState]:
        self._require_connected()
        states = []
        for row in self._all_order_rows():
            order_id = str(row.get("order_id", ""))
            quantity = int(float(row.get("quantity", 0) or 0))
            pending = int(float(row.get("pending_quantity", 0) or 0))
            filled = max(quantity - pending, 0)
            status = _BREEZE_STATUS_MAP.get(str(row.get("status", "")).strip().lower(), "OPEN")
            states.append(
                OrderState(order_id=order_id, status=status, filled_quantity=filled,
                           remaining_quantity=max(quantity - filled, 0))
            )
        return states

    def get_open_orders(self) -> list[OrderState]:
        return [state for state in self.get_order_book() if state.status not in TERMINAL_STATUSES]

    # -- optional hooks StrategyExecutionEngine duck-types for -------------------- #
    def get_order(self, order_id: str) -> OrderState:
        row = self._fetch_order_row(order_id)
        if row is None:
            return OrderState(order_id=str(order_id), status="UNKNOWN", filled_quantity=0, remaining_quantity=0)

        quantity = int(float(row.get("quantity", 0) or 0))
        pending = int(float(row.get("pending_quantity", 0) or 0))
        filled = max(quantity - pending, 0)
        status = _BREEZE_STATUS_MAP.get(str(row.get("status", "")).strip().lower(), "OPEN")
        return OrderState(
            order_id=str(order_id), status=status, filled_quantity=filled,
            remaining_quantity=max(quantity - filled, 0),
        )

    def modify_order(self, order_id: str, quantity: int, limit_price: float) -> bool:
        self._require_not_read_only("modify_order")
        self._require_connected()
        row = self._fetch_order_row(order_id)
        if row is None:
            raise BrokerConnectionError(f"ICICI Breeze modify_order: order '{order_id}' not found in order book")
        try:
            response = self._breeze_client.modify_order(
                order_id=str(order_id), exchange_code="NFO", order_type="limit",
                quantity=str(quantity), price=str(limit_price), validity="day",
            )
        except Exception as exc:
            raise self._classify_error(exc, context="modify_order") from exc
        if isinstance(response, dict) and response.get("Error"):
            return False
        return True

    # -- optional, adapter-specific capabilities (not part of BrokerClient) ------ #
    def get_account_info(self) -> AccountInfo:
        self._require_connected()
        try:
            response = self._breeze_client.get_customer_details(
                api_session=self._config.credentials.icici_breeze_session_token
            )
        except Exception as exc:
            raise self._classify_error(exc, context="get_account_info") from exc
        data = self._extract_data(response, context="get_account_info")
        return AccountInfo(
            client_id=str(data.get("idirect_userid", "")),
            name=str(data.get("idirect_user_name", "")),
            email=str(data.get("email_id", "")),
        )

    def get_funds(self) -> FundsSnapshot:
        self._require_connected()
        try:
            response = self._breeze_client.get_funds()
        except Exception as exc:
            raise self._classify_error(exc, context="get_funds") from exc
        data = self._extract_data(response, context="get_funds")
        return FundsSnapshot(
            available_cash=float(data.get("unallocated_balance", 0) or 0),
            used_margin=float(data.get("block_by_trade_balance", 0) or 0),
        )
