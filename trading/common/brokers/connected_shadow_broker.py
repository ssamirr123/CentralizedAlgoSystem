"""
ConnectedShadowBroker -- Phase 5B: real Angel One market/account/instrument
READ data, wired to fully SIMULATED order execution.

    real_broker  (e.g. AngelOneBroker, read_only=True)  -> connect/get_quote/
                                                            resolve_instrument/
                                                            get_account_info/get_funds
    shadow_broker (ShadowBroker)                        -> place_order/modify_order/
                                                            cancel_order/get_positions/
                                                            get_order/get_order_book

This class is a pure DELEGATOR -- it does not reimplement Angel's request/
response translation (that stays in AngelOneBroker) or order simulation
(that stays in ShadowBroker). It exists only to compose "real reads" with
"simulated writes" behind one BrokerClient, so RiskManager/ExecutionEngine/
BrokerManager don't need to know two different broker objects are involved.

FAIL CLOSED, structurally, not just by convention:

  1. __init__ REQUIRES execution_mode to already equal ExecutionMode.SHADOW
     -- passing anything else (including omitting it) raises ValueError
     immediately, before either broker is touched. There is no default
     that lets this class be constructed silently in a non-SHADOW mode.
  2. The validated mode is captured into a private, unexported attribute
     at construction time. There is no setter -- nothing can change it
     afterwards, even if the TradingAccount this broker is registered
     under later has its own (mutable) execution_mode field changed.
  3. place_order()/modify_order()/cancel_order() NEVER reference
     self._real_broker anywhere in their bodies -- there is no code path
     from those three methods to the real broker at all, structurally,
     independent of the execution_mode check. The check in (1)/(2) is
     redundant defense-in-depth on top of that structural fact, not the
     only thing standing between this class and a real order.
  4. __init__ also REQUIRES the real broker to itself be constructed
     read_only=True (checked via its `is_read_only` property) -- so even
     if some future change accidentally added a call to
     self._real_broker.place_order() in this file, AngelOneBroker's own
     _require_not_read_only() guard would still block it before any
     SmartAPI call.

Positions/orders/P&L reported by this class are ALWAYS the simulated ones
from shadow_broker -- the real account's actual positions/order book are
never read through this class at all (see get_positions()/get_order_book()
below), so simulated and real state can never be confused with each other.
"""
from __future__ import annotations

from typing import Any

from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Position, Quote
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.execution import OrderState
from trading.common.trading_account import ExecutionMode


class ConnectedShadowBroker(BrokerClient):
    # Phase 13 observability: place_order/modify_order/cancel_order are
    # ALWAYS simulated here regardless of the real broker underneath (see
    # module docstring's FAIL CLOSED point 3) -- so this is always True.
    is_simulated = True

    def __init__(
        self,
        real_broker: BrokerClient,
        shadow_broker: ShadowBroker | None = None,
        *,
        execution_mode: ExecutionMode | str = None,  # type: ignore[assignment]
    ) -> None:
        # Fail closed: no default that lets this construct silently.
        if execution_mode is None:
            raise ValueError(
                "ConnectedShadowBroker requires execution_mode=ExecutionMode.SHADOW to be passed "
                "explicitly. Refusing to default to a mode -- fail closed."
            )
        resolved_mode = ExecutionMode(execution_mode)
        if resolved_mode != ExecutionMode.SHADOW:
            raise ValueError(
                f"ConnectedShadowBroker requires execution_mode=SHADOW exactly; got {resolved_mode.value!r}. "
                "Refusing to construct -- fail closed."
            )
        if not getattr(real_broker, "is_read_only", False):
            raise ValueError(
                "ConnectedShadowBroker requires a real_broker constructed with read_only=True "
                "(e.g. AngelOneBroker(config, read_only=True)). Refusing to construct -- fail closed."
            )

        self._execution_mode = resolved_mode  # immutable for the lifetime of this instance; no setter exists
        self._real_broker = real_broker
        self._shadow_broker = shadow_broker or ShadowBroker()

    def __repr__(self) -> str:
        return f"ConnectedShadowBroker(execution_mode={self._execution_mode.value}, real_connected={self._real_broker.is_connected()})"

    @property
    def execution_mode(self) -> ExecutionMode:
        return self._execution_mode

    # -- BrokerClient: connection lifecycle -- REAL ----------------------------- #
    def connect(self) -> None:
        self._real_broker.connect()
        self._shadow_broker.connect()

    def disconnect(self) -> None:
        self._real_broker.disconnect()
        self._shadow_broker.disconnect()

    def is_connected(self) -> bool:
        return self._real_broker.is_connected()

    # -- BrokerClient: market data -- REAL --------------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        return self._real_broker.get_quote(symbol)

    # -- extra, adapter-parity read operations -- REAL --------------------------- #
    def resolve_instrument(self, symbol: str) -> tuple[str, str]:
        return self._real_broker.resolve_instrument(symbol)

    def get_account_info(self) -> Any:
        return self._real_broker.get_account_info()

    def get_funds(self) -> Any:
        return self._real_broker.get_funds()

    # -- BrokerClient: orders -- SIMULATED, ALWAYS -------------------------------- #
    # These three methods never reference self._real_broker anywhere in
    # their bodies -- see the module docstring's "FAIL CLOSED" point 3.
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> OrderResult:
        assert self._execution_mode == ExecutionMode.SHADOW  # redundant, see module docstring
        return self._shadow_broker.place_order(symbol, side, quantity, order_type, limit_price)

    def modify_order(self, order_id: str, quantity: int, limit_price: float) -> bool:
        assert self._execution_mode == ExecutionMode.SHADOW
        return self._shadow_broker.modify_order(order_id, quantity, limit_price)

    def cancel_order(self, order_id: str) -> bool:
        assert self._execution_mode == ExecutionMode.SHADOW
        return self._shadow_broker.cancel_order(order_id)

    def simulate_intent(self, intent) -> OrderResult:
        assert self._execution_mode == ExecutionMode.SHADOW
        return self._shadow_broker.simulate_intent(intent)

    # -- positions/order status -- SIMULATED, ALWAYS ------------------------------- #
    def get_positions(self) -> list[Position]:
        return self._shadow_broker.get_positions()

    def get_order(self, order_id: str) -> OrderState:
        return self._shadow_broker.get_order(order_id)

    def get_order_book(self) -> list[OrderState]:
        return self._shadow_broker.get_order_book()

    def get_open_orders(self) -> list[OrderState]:
        return self._shadow_broker.get_open_orders()

    def fill_pending_order(self, order_id: str, fill_quantity: int | None = None, price: float | None = None) -> None:
        self._shadow_broker.fill_pending_order(order_id, fill_quantity, price)

    def configure_next_order(self, symbol: str, outcome: str, **kwargs: Any) -> None:
        self._shadow_broker.configure_next_order(symbol, outcome, **kwargs)
