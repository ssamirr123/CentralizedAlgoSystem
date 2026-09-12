"""
Shadow execution bridge for DoubleStraddelAlgo (Phase 3).

Mirrors every real order broker/orders.py places into the new
broker-agnostic architecture --

    OrderIntent -> RiskManager -> ExecutionEngine -> StrategyAssignment
    -> TradingAccount -> BrokerManager -> AngelOneBroker

purely for comparison logging. It NEVER affects the real order's outcome
or timing:

  - mirror_place_order() does all of its work (building the intent, running
    RiskManager, calling ExecutionEngine.execute()) on a background daemon
    thread, so orders.py's real call sites return exactly as fast as they
    did before this module existed.
  - every entry point here is best-effort and swallows ALL exceptions
    (logged via logging.exception, never raised) -- see "Error isolation"
    below.
  - cancellation (cancel/cancel_all_pending/cancel_pending_for_tokens) is
    NOT converted into an OrderIntent -- cancelling isn't a new trade
    intent -- it is mirrored as a structured SHADOW_CANCEL_* log event only.
  - every mirrored order additionally runs compare_with_legacy() (see
    below), a pure, side-effect-free function that reconstructs the shape
    broker/orders.py's real Angel placeOrder params would take for the
    SAME raw inputs and checks the new OrderIntent agrees with it
    field-by-field. The result (`match: bool`, `differences: [...]`) is
    attached to the shadow log record under "comparison" -- this is the
    "existing order vs new OrderIntent" comparison tooling, and it never
    calls a broker or submits anything.

CRITICAL SAFETY MECHANISM -- read before changing anything below:

The shadow TradingAccount's BrokerCredentials are hard-coded to four empty
strings, UNCONDITIONALLY, regardless of what ANGELONE_* environment
variables the live algo's own process has (which must be real, since the
live path needs them to log in). This is deliberate and is NOT the same
guarantee as trading_mode="paper" alone:

  - trading_mode="paper" stops AngelOneBroker.place_order()/cancel_order()
    from submitting a real order IF a session was established.
  - but trading.common.config.BrokerCredentials()'s fields default to
    reading the current environment (`_env(...)` factories) -- calling it
    with no arguments on a box where the live algo's real credentials are
    set would hand this "shadow" AngelOneBroker the SAME real credentials,
    and BrokerManager.get_broker() unconditionally calls .connect() the
    first time an account's broker is resolved. That would mean the shadow
    path establishes a SECOND real, authenticated Angel One session on the
    same account -- which could invalidate or otherwise disturb the live
    algo's own session, entirely independent of trading_mode.

Forcing every credential field to "" guarantees AngelOneBroker.connect()
always raises BrokerConfigError before ever importing SmartApi or touching
the network, no matter what the process environment contains. Do not
"simplify" this to `BrokerCredentials()` -- that reintroduces exactly the
risk described above.

Known limitations (see the Phase 3 final report for the full list):
  - orders.py already resolves (tradingsymbol, symboltoken) via
    token_file.token_nifty(). AngelOneBroker.place_order() takes only a
    symbol string and re-resolves symboltoken itself internally (Phase 2's
    own instrument_resolver hook) -- a second, independent lookup against
    the same scrip master. The `token` value received here is kept only as
    OrderIntent.metadata (never passed to AngelOneBroker, which has no
    parameter for it) -- a documented, temporary duplication, not fixed in
    this phase.
  - because the shadow AngelOneBroker can never actually connect (by
    design, see above), the shadow mirror can only ever demonstrate
    RiskManager + OrderIntent + ExecutionEngine + StrategyAssignment +
    TradingAccount + BrokerManager wiring -- it cannot demonstrate a
    genuine "connected AngelOneBroker translates and would-have-placed
    this order" comparison against a real session. That would require a
    deliberate, separate, manually-invoked read-only validation step (Step
    12 of the Phase 3 plan) with real credentials supplied on purpose --
    not something this always-on, automatic mirror should ever do.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from trading.common.broker import LiveTradingDisabledError, OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.instrument import Instrument
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import TradingAccount

_log = logging.getLogger("DoubleStraddelAlgo.shadow")

STRATEGY_ID = "DoubleStraddelAlgo"
ACCOUNT_ID = "ANGEL_MAIN"

# Defense in depth: even though nothing here should ever construct a log
# record containing one of these, refuse to emit any record that does.
_FORBIDDEN_LOG_MARKERS = (
    "api_key", "apikey", "client_id", "clientid", "mpin", "password",
    "totp", "refresh_token", "refreshtoken", "feed_token", "feedtoken",
    "access_token", "accesstoken", "session_token", "sessiontoken",
)


@dataclass(frozen=True)
class _ShadowStack:
    broker_manager: BrokerManager
    strategy_assignment: StrategyAssignment
    risk_manager: RiskManager
    execution_engine: StrategyExecutionEngine


_lock = threading.Lock()
_stack: "_ShadowStack | None" = None


def _build_stack() -> "_ShadowStack":
    """Build the shadow architecture. See the module docstring's CRITICAL
    SAFETY MECHANISM section for why credentials are forced empty here."""
    broker_manager = BrokerManager()

    shadow_config = TradingConfig(
        trading_mode="paper",
        broker_name="angelone",
        credentials=BrokerCredentials(
            angelone_api_key="",
            angelone_client_id="",
            angelone_password="",
            angelone_totp_secret="",
        ),
    )

    from trading.common.brokers.angelone import AngelOneBroker

    account = TradingAccount(account_id=ACCOUNT_ID, account_name="Angel One (shadow)", broker_id="angelone")
    broker_manager.register_account(account, broker_factory=lambda: AngelOneBroker(shadow_config))

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID)

    risk_manager = RiskManager(strategy_assignment)
    execution_engine = StrategyExecutionEngine(
        # Never actually used: execute() always resolves the real per-intent
        # broker via broker_manager. A PaperBroker is used here only because
        # StrategyExecutionEngine.__init__ requires *some* BrokerClient.
        broker=PaperBroker(),
        # allow_market_emergency=True: orders.place_market() is only ever
        # called on the real emergency-square-off path, so a MARKET
        # OrderIntent should stay MARKET in the shadow mirror rather than
        # StrategyExecutionEngine silently degrading it to LIMIT. This is a
        # behavioral setting only -- it has no bearing on the live-order
        # safety guarantee, which comes from the hard-coded empty
        # credentials above, not from this flag.
        config=ExecutionConfig(allow_market_emergency=True),
        risk_manager=risk_manager,
        strategy_assignment=strategy_assignment,
        broker_manager=broker_manager,
    )
    return _ShadowStack(broker_manager, strategy_assignment, risk_manager, execution_engine)


def _get_stack() -> "_ShadowStack":
    """Lazily build the shadow stack on first use -- importing this module,
    or importing broker/orders.py which imports this module, must not by
    itself construct any broker infrastructure."""
    global _stack
    if _stack is None:
        with _lock:
            if _stack is None:
                _stack = _build_stack()
    return _stack


def _reset_stack_for_tests(stack: "_ShadowStack | None" = None) -> None:
    """Test-only seam: force the next _get_stack() call to rebuild (or to
    return a caller-supplied fake stack). Not used by production code."""
    global _stack
    with _lock:
        _stack = stack


def _to_instrument(symbol: str) -> Instrument:
    """Minimal, best-effort Instrument for the given Angel tradingsymbol.

    Deliberately shallow: only option_type is derived (from the symbol's
    own CE/PE suffix, which needs no external lookup). underlying/expiry/
    strike are NOT parsed here, even though DoubleStraddelAlgo/
    strategy/expiry.py already knows how -- duplicating that positional
    parsing here would be a second, independently-maintained place that
    assumes Angel's exact symbol layout, and getting it subtly wrong would
    be worse than leaving it blank. Documented limitation, not fixed here.
    """
    suffix = symbol[-2:] if len(symbol) >= 2 else ""
    option_type = suffix if suffix in ("CE", "PE") else ""
    return Instrument(symbol=symbol, exchange="NFO", option_type=option_type)


def _to_order_intent(symbol, side, qty, order_type, price, trigger_price, reason, token) -> OrderIntent:
    return OrderIntent(
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
        symbol=str(symbol),
        exchange="NFO",
        instrument=_to_instrument(str(symbol)),
        side=OrderSide(str(side).upper()),
        quantity=int(qty),
        order_type=OrderType(str(order_type).upper()),
        limit_price=price,
        trigger_price=trigger_price,
        reason=reason,
        # The Angel symboltoken the strategy already resolved is kept here
        # as migration metadata only -- AngelOneBroker never sees it (it
        # has no parameter for it and re-resolves the token from `symbol`
        # itself). See the module docstring's "Known limitations".
        metadata={"shadow": True, "angelone_symboltoken": str(token)},
    )


def compare_with_legacy(symbol, token, qty, side, order_type, price, intent: OrderIntent) -> dict:
    """Pure comparison tool: 'existing order vs new OrderIntent', without
    ever submitting anything anywhere.

    Reconstructs the shape broker/orders.py's real Angel placeOrder params
    would take for these SAME raw inputs (the exact inputs the OrderIntent
    was itself built from -- see _to_order_intent), then checks the
    OrderIntent's own fields agree with it field-by-field. This answers
    "would the new broker-agnostic layer generate the same trading
    instruction as the existing Angel-specific implementation?" without
    needing orders.py to hand over its actual internal params dict (which
    lives inside a retry closure and varies its `price` field per retry
    attempt -- comparing against the same reference-price computation the
    mirror itself already uses is the honest, safe answer, not a live
    capture of exactly what was sent on whichever attempt succeeded).
    """
    order_type_upper = str(order_type).upper()
    legacy = {
        "tradingsymbol": str(symbol),
        "symboltoken": str(token),
        "transactiontype": str(side).upper(),
        "ordertype": order_type_upper,
        "quantity": str(qty),
        "price": str(price) if order_type_upper == "LIMIT" and price is not None else "0",
    }

    differences: list[str] = []
    if intent.symbol != legacy["tradingsymbol"]:
        differences.append(f"symbol: intent={intent.symbol!r} legacy={legacy['tradingsymbol']!r}")
    if intent.metadata.get("angelone_symboltoken") != legacy["symboltoken"]:
        differences.append(
            f"symboltoken: intent={intent.metadata.get('angelone_symboltoken')!r} legacy={legacy['symboltoken']!r}"
        )
    if intent.side.value != legacy["transactiontype"]:
        differences.append(f"side: intent={intent.side.value!r} legacy={legacy['transactiontype']!r}")
    if intent.order_type.value != legacy["ordertype"]:
        differences.append(f"order_type: intent={intent.order_type.value!r} legacy={legacy['ordertype']!r}")
    if intent.quantity != int(legacy["quantity"]):
        differences.append(f"quantity: intent={intent.quantity!r} legacy={legacy['quantity']!r}")
    if legacy["ordertype"] == "LIMIT":
        intent_price = str(intent.limit_price) if intent.limit_price is not None else None
        if intent_price != legacy["price"]:
            differences.append(f"price: intent={intent_price!r} legacy={legacy['price']!r}")

    return {"match": not differences, "differences": differences, "legacy": legacy}


def _assert_no_secrets(record: dict) -> None:
    text = str(record).lower()
    for marker in _FORBIDDEN_LOG_MARKERS:
        if marker in text:
            raise ValueError(f"shadow log record contains forbidden marker: {marker}")


def _log_comparison(
    intent: OrderIntent, risk_result, execution_result, live_order_id, note: str = "", comparison: dict | None = None,
) -> None:
    record = {
        "strategy": intent.strategy_id,
        "operation": "place_order",
        "symbol": intent.symbol,
        "symboltoken": intent.metadata.get("angelone_symboltoken"),
        "side": intent.side.value,
        "quantity": intent.quantity,
        "order_type": intent.order_type.value,
        "price": intent.limit_price,
        "trigger_price": intent.trigger_price,
        "correlation_id": intent.correlation_id,
        "client_order_id": intent.client_order_id,
        "timestamp": intent.created_at,
        "reason": intent.reason,
        "shadow_mode": "paper",
        "broker": "angelone",
        "account": intent.account_id,
        "live_order_id": live_order_id,
        "risk_result": {"allowed": risk_result.allowed, "reason": risk_result.reason},
        "execution_result": (
            None
            if execution_result is None
            else {"success": execution_result.success, "status": execution_result.status, "message": execution_result.message}
        ),
        "comparison": comparison,
        "note": note,
    }
    _assert_no_secrets(record)
    _log.info("[SHADOW] %s", record)


def _mirror_place_order(symbol, token, qty, side, order_type, price, trigger_price, reason, live_order_id) -> None:
    intent = _to_order_intent(symbol, side, qty, order_type, price, trigger_price, reason, token)
    comparison = compare_with_legacy(symbol, token, qty, side, order_type, price, intent)
    stack = _get_stack()

    risk_result = stack.risk_manager.validate(intent)
    if not risk_result.allowed:
        _log_comparison(
            intent, risk_result, execution_result=None, live_order_id=live_order_id,
            note="risk_rejected", comparison=comparison,
        )
        return

    try:
        execution_result = stack.execution_engine.execute(intent)
    except LiveTradingDisabledError:
        # Expected safety behavior, not a failure -- see module docstring.
        # Never bypassed, never retried through another path.
        _log_comparison(
            intent, risk_result, execution_result=None, live_order_id=live_order_id,
            note="blocked_by_live_trading_guard", comparison=comparison,
        )
        return

    _log_comparison(intent, risk_result, execution_result, live_order_id, comparison=comparison)


def mirror_place_order(
    *, symbol, token, qty, side, order_type, price=None, trigger_price=None, reason="", live_order_id=None,
) -> None:
    """Best-effort shadow mirror of a real order placement.

    Runs on a background daemon thread and can NEVER raise back into the
    caller -- see "Error isolation" in the module docstring.
    """

    def _run():
        try:
            _mirror_place_order(symbol, token, qty, side, order_type, price, trigger_price, reason, live_order_id)
        except Exception:
            _log.exception("[SHADOW] mirror_place_order failed (ignored, live order unaffected)")

    threading.Thread(target=_run, daemon=True).start()


def mirror_cancel(order_id) -> None:
    """Cancellation is NOT a new trade intent -- mirrored as a structured
    log event only, per the Phase 3 spec. Never touches RiskManager/
    ExecutionEngine/BrokerManager."""
    try:
        _log.info(
            "[SHADOW] %s",
            {"strategy": STRATEGY_ID, "operation": "SHADOW_CANCEL_REQUEST", "scope": "order_id",
             "order_id": order_id, "shadow_mode": "paper"},
        )
    except Exception:
        _log.exception("[SHADOW] mirror_cancel failed (ignored)")


def mirror_cancel_all_pending() -> None:
    try:
        _log.info(
            "[SHADOW] %s",
            {"strategy": STRATEGY_ID, "operation": "SHADOW_CANCEL_ALL", "shadow_mode": "paper"},
        )
    except Exception:
        _log.exception("[SHADOW] mirror_cancel_all_pending failed (ignored)")


def mirror_cancel_pending_for_tokens(tokens) -> None:
    try:
        _log.info(
            "[SHADOW] %s",
            {"strategy": STRATEGY_ID, "operation": "SHADOW_CANCEL_REQUEST", "scope": "tokens",
             "tokens": [str(t) for t in tokens], "shadow_mode": "paper"},
        )
    except Exception:
        _log.exception("[SHADOW] mirror_cancel_pending_for_tokens failed (ignored)")
