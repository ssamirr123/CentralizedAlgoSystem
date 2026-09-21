"""
Broker-agnostic strategy execution engine.

trading.common.broker.BrokerClient gives every adapter (paper, Zerodha,
AngelOne, ICICI Breeze) the same primitives: connect, get_quote,
place_order, cancel_order, get_positions. That's enough for a fire-and-
forget market order, but every strategy that actually manages LIMIT orders
(trading/algos/DoubleStraddelAlgo/broker/orders.py is the reference
example) has reimplemented the same order-lifecycle logic directly against
its broker's raw SDK: retry-with-reprice, pending-order timeout handling,
partial-fill detection, and throttled/coalesced reads. This module lifts
that logic out and implements it once, against BrokerClient only, so it
works unchanged for whichever broker a strategy is configured with.

Pending-order lifecycle management (re-pricing, timeout handling) is an
OPTIONAL capability an adapter opts into by implementing two extra methods
that BrokerClient does not declare as abstract:

    get_order(order_id: str) -> OrderState
    modify_order(order_id: str, quantity: int, limit_price: float) -> bool

They're duck-typed rather than added to the BrokerClient ABC so that
adapters which fill synchronously (PaperBroker) or haven't implemented them
yet (the still-stubbed Zerodha/AngelOne/ICICI adapters) are unaffected:
StrategyExecutionEngine simply trusts place_order()'s returned status as
final when an adapter has nothing more to report.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from trading.common.broker import (
    BrokerClient,
    LiveTradingDisabledError,
    OrderResult,
    OrderSide,
    OrderType,
)
from trading.common.alerts import AlertManager
from trading.common.broker_manager import BrokerManager, UnknownAccountError
from trading.common.broker_response_validation import validate_broker_response
from trading.common.idempotency_store import (
    STATUS_AMBIGUOUS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    IdempotencyRecord,
    IdempotencyStore,
    compute_intent_hash,
)
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_canary import LiveCanaryGuard
from trading.common.observability import (
    EVENT_AUTHORIZATION_STATE_GATE,
    EVENT_BROKER_ORDER_PLACED,
    EVENT_EXECUTION_RESULT,
    EVENT_FILL,
    EVENT_LIVE_CANARY_AUTHORIZATION,
    EVENT_ORDER_INTENT_CREATED,
    EVENT_RISK_DECISION,
    AuditTrail,
    MetricsRegistry,
    ObservabilityHealth,
)
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskManager
from trading.common.strategy_assignment import StrategyAssignment, UnknownAssignmentError
from trading.common.trading_account import (
    AccountAuthorizationError,
    AccountAuthorizationState,
    ExecutionMode,
    TradingAccount,
)

# Phase 15B.1 hard gate: what TradingAccount.authorization_state (Phase 15B)
# must be for a given ExecutionMode before RiskManager or any broker-facing
# gate ever runs. DISABLED/KILLED block every mode unconditionally --
# checked first, independent of the mode-specific table below. Any mode not
# named here (i.e. PAPER/SHADOW) requires nothing beyond "not
# DISABLED/KILLED", since simulated execution was never gated on live
# authorization and READ_ONLY (every new account's own safe default) must
# remain sufficient for it.
_REQUIRED_AUTHORIZATION_STATES: dict[ExecutionMode, frozenset[AccountAuthorizationState]] = {
    ExecutionMode.LIVE: frozenset({AccountAuthorizationState.LIVE_AUTHORIZED}),
    ExecutionMode.LIVE_CANARY: frozenset(
        {AccountAuthorizationState.CANARY_READY, AccountAuthorizationState.LIVE_AUTHORIZED}
    ),
}


def _check_authorization_state(account: TradingAccount, mode: ExecutionMode) -> None:
    """Phase 15B.1: the account's own authorization_state must permit this
    specific execution_mode. This is a NEW, additional hard gate -- it
    supplements, and runs strictly before, every existing gate (RiskManager,
    LiveCanaryGuard, CentralKillSwitch, RiskLimits.is_live_ready()); it
    replaces none of them, and passing it is NEVER sufficient on its own
    for an order to reach a broker.

    Raises AccountAuthorizationError (never returns a value) on any
    rejection -- execute() catches this and converts it into a rejected
    ExecutionResult, exactly like UnknownAssignmentError/UnknownAccountError
    are already handled. Fail-closed: an execution_mode with no entry in
    _REQUIRED_AUTHORIZATION_STATES (i.e. PAPER/SHADOW) is permitted through
    this specific gate as long as the account isn't DISABLED/KILLED --
    anything else this method doesn't recognize is handled by execute()'s
    own pre-existing "unrecognized execution_mode" rejection further down,
    not by silently approving it here."""
    state = account.authorization_state
    if not isinstance(state, AccountAuthorizationState):
        # Defensive: TradingAccount.__post_init__ already coerces/validates
        # this at construction time, so this should be unreachable -- but a
        # gate that trusts an untyped value is exactly the class of bug this
        # phase exists to close. Fail closed rather than assume.
        raise AccountAuthorizationError(
            account.account_id, str(state), "authorization_state is missing or not a recognized AccountAuthorizationState"
        )
    if state in (AccountAuthorizationState.DISABLED, AccountAuthorizationState.KILLED):
        raise AccountAuthorizationError(
            account.account_id, state.value, "no execution permitted in any mode"
        )
    if not account.enabled:
        raise AccountAuthorizationError(account.account_id, state.value, "account is disabled")

    required = _REQUIRED_AUTHORIZATION_STATES.get(mode)
    if required is not None and state not in required:
        allowed = ", ".join(sorted(s.value for s in required))
        raise AccountAuthorizationError(
            account.account_id, state.value,
            f"{mode.value} execution requires authorization_state in {{{allowed}}}",
        )


# Phase 16.2: public alias so a read-only diagnostic (e.g.
# trading.common.assignment_readiness) can reuse this exact gate to REPORT
# whether an assignment would currently pass it, without duplicating
# _REQUIRED_AUTHORIZATION_STATES or changing this function's behavior in
# any way -- execute() itself still calls the private name below unchanged.
check_authorization_state_for_mode = _check_authorization_state

# Statuses that mean "nothing left to manage" across brokers. Adapters may
# use their own vocabulary beyond this (e.g. "TRIGGER PENDING" is NOT
# terminal) — anything not in this set is treated as still-open.
TERMINAL_STATUSES = {"FILLED", "COMPLETE", "REJECTED", "CANCELLED"}


class AmbiguousOrderStateError(RuntimeError):
    """Phase 15D-DR (Area E/J): raised when a broker call made specifically
    to SUBMIT an order raised an exception -- a network timeout, connection
    reset, rate limit, or any other failure during the mutating call itself
    -- rather than returning a definitive OrderResult. This is fundamentally
    different from a confirmed REJECTED response: here, it is impossible to
    tell whether the broker actually received and processed the request
    before failing to answer. Never safe to blindly retry (a retry could
    submit a second real order for what may already be a live one) --
    `_retry()` re-raises this immediately instead of looping, and
    `execute()` persists it as STATUS_AMBIGUOUS and returns a distinct,
    non-retryable ExecutionResult requiring reconciliation before any
    future action on this idempotency key."""


class ConfirmedRejectionError(RuntimeError):
    """Phase 15D.3-R: raised when the broker returned a definitive,
    non-exceptional REJECTED OrderResult carrying NO order_id -- i.e. the
    broker unambiguously told us the order was never created (e.g.
    AngelOne's own place_order() returning status="REJECTED" after a
    clean API-level rejection such as AG7002 "IP not registered", with no
    exception raised and no order_id issued). This is deliberately
    distinct from AmbiguousOrderStateError: there, the mutating call
    itself raised and the outcome is genuinely unknown; here, the broker
    answered normally and definitively said "no".

    Still safe to retry with a new price/attempt while attempts remain
    (Phase 15D-DR's own `test_confirmed_rejected_status_is_still_safely_
    retried_with_new_price` is unaffected by this class -- see `_retry()`,
    which retries on this exception exactly like any other and only
    changes behavior once every attempt is exhausted). If EVERY attempt
    ends in this same confirmed rejection, `_retry()` re-raises it instead
    of returning a bare None, and `execute()` persists a terminal
    STATUS_REJECTED idempotency outcome -- closing the gap where a
    definitively-rejected order (no ambiguity at all) was previously left
    at STATUS_PENDING forever, indistinguishable from a genuinely
    unresolved attempt (see docs/phase-15d-2-live-canary-report.md's
    Attempt 2 and docs/phase-15d-3-post-canary-verification-report.md).

    A REJECTED OrderResult that DOES carry an order_id (an inconsistent,
    suspicious broker response this codebase has never actually observed
    but must not blindly trust) is deliberately NOT wrapped in this class
    -- see place_limit()/place_market_emergency()'s own `_call()`, which
    routes that case through AmbiguousOrderStateError instead, requiring
    reconciliation rather than confidently declaring a rejection."""


@dataclass(frozen=True)
class OrderState:
    """Point-in-time status of a previously placed order. An adapter that
    supports pending-order management returns this from get_order()."""

    order_id: str
    status: str
    filled_quantity: int
    remaining_quantity: int
    # Phase 15D-RECON: optional, additive -- defaults keep every existing
    # get_order()/get_order_book() implementation (shadow_broker, dhan,
    # icici_breeze, connected_shadow_broker) unchanged; only an adapter
    # that chooses to populate them (AngelOneBroker, see angelone.py) lets
    # trading.common.reconciliation compare an order's ACTUAL symbol/side
    # against what a strategy originally intended (Area E of that phase's
    # brief) instead of only its status/quantity, which is all this
    # dataclass carried before. Never used by the pending-order management
    # logic in _manage_pending() below, which only reads status/filled_
    # quantity/remaining_quantity exactly as it always has.
    symbol: str = ""
    side: OrderSide | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """Broker-independent outcome of executing an OrderIntent. Nothing
    above this object (a strategy, or whatever called execute()) should
    ever need to look at a raw OrderResult or a broker SDK response."""

    success: bool
    order_id: str = ""
    client_order_id: str = ""
    status: str = ""
    message: str = ""
    filled_quantity: int = 0
    average_price: float | None = None
    account_id: str = ""
    broker_id: str = ""
    # Carried straight from the OrderIntent so "every simulated/executed
    # order has a correlation id, strategy id, and timestamp" holds at this
    # level -- BrokerClient.place_order()'s ABC signature has no channel to
    # thread these down to the broker call itself, so they're attached here
    # instead, at the one place that already has the full intent in scope.
    correlation_id: str = ""
    strategy_id: str = ""
    created_at: str = ""

    def to_json(self) -> str:
        """Phase 14.6 Blocker D: exact serialization for
        IdempotencyRecord.result_json, so a replayed result is
        indistinguishable from the original."""
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "ExecutionResult":
        return cls(**json.loads(payload))

    @classmethod
    def rejected(cls, intent: OrderIntent, reason: str) -> "ExecutionResult":
        return cls(
            success=False, client_order_id=intent.client_order_id, status="REJECTED", message=reason,
            correlation_id=intent.correlation_id, strategy_id=intent.strategy_id, created_at=intent.created_at,
        )

    @classmethod
    def ambiguous(cls, intent: OrderIntent, reason: str) -> "ExecutionResult":
        """Phase 17.1 Section 27 fix: a distinct, non-terminal status for a
        broker call whose outcome is genuinely UNKNOWN (the broker may have
        actually accepted/filled the order) -- deliberately never "REJECTED",
        which would tell a caller such as PortfolioRiskManager it is safe to
        release the exposure/order-count this order reserved. See
        PortfolioRiskManager.commit_reservation()'s own handling of
        status == "AMBIGUOUS"."""
        return cls(
            success=False, client_order_id=intent.client_order_id, status="AMBIGUOUS", message=reason,
            correlation_id=intent.correlation_id, strategy_id=intent.strategy_id, created_at=intent.created_at,
        )

    @classmethod
    def from_order_result(
        cls, order_result: OrderResult, intent: OrderIntent, account_id: str, broker_id: str
    ) -> "ExecutionResult":
        success = order_result.status not in ("REJECTED", "CANCELLED")
        filled = order_result.quantity if success and order_result.status in TERMINAL_STATUSES else 0
        return cls(
            success=success,
            order_id=order_result.order_id,
            client_order_id=intent.client_order_id,
            status=order_result.status,
            message=order_result.message,
            filled_quantity=filled,
            # OrderResult doesn't carry a fill price today; a real adapter
            # would need to start reporting one for this to be non-None.
            average_price=None,
            account_id=account_id,
            broker_id=broker_id,
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            created_at=intent.created_at,
        )


@dataclass
class ExecutionConfig:
    """Tunables for StrategyExecutionEngine. Defaults are broker-neutral —
    a strategy overrides them per its own risk/latency profile."""

    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    # Price bump applied per retry attempt, in price units, walking the
    # limit toward the market so a repeatedly-unfilled order gets more
    # fillable each attempt.
    limit_slippage: float = 0.05
    tick_size: float = 0.05
    pending_timeout_seconds: float = 10.0
    # What to do with a LIMIT order still open after pending_timeout_seconds:
    # "CANCEL" | "MODIFY" (re-price the remainder) | "MARKET" (requires
    # allow_market_emergency=True).
    pending_action: str = "CANCEL"
    allow_market_emergency: bool = False
    # Minimum gap enforced between any two broker API calls made by this
    # engine, so concurrent legs of a multi-leg strategy don't trip a
    # broker's rate limit by hammering it in the same instant.
    min_api_interval_seconds: float = 0.35
    dry_run: bool = False


class StrategyExecutionEngine:
    """Places and manages LIMIT orders against any BrokerClient.

    One engine instance per strategy/broker session — the rate-limit
    throttle and pending-order registry are per-instance state, not global.
    """

    def __init__(
        self,
        broker: BrokerClient,
        config: ExecutionConfig | None = None,
        on_log: Callable[[str], None] | None = None,
        *,
        risk_manager: RiskManager | None = None,
        strategy_assignment: StrategyAssignment | None = None,
        broker_manager: BrokerManager | None = None,
        metrics: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
        canary_guard: LiveCanaryGuard | None = None,
        central_kill_switch: CentralKillSwitch | None = None,
        idempotency_store: IdempotencyStore | None = None,
        observability_health: ObservabilityHealth | None = None,
        live_authorization_store: Any | None = None,
    ) -> None:
        self._broker = broker
        self._config = config or ExecutionConfig()
        self._log = on_log or (lambda msg: None)
        self._api_lock = threading.Lock()
        self._last_api_ts = 0.0
        self._pending_lock = threading.Lock()
        # order_id -> (placed OrderResult, the broker it was placed against).
        # Tracking the broker per order (rather than always assuming
        # self._broker) is what lets cancel_all_pending() do the right
        # thing when execute() has routed different intents to different
        # accounts through this same engine instance.
        self._pending_orders: dict[str, tuple[OrderResult, BrokerClient]] = {}
        # Optional collaborators -- only required for execute(intent).
        # place_limit()/place_market_emergency()/cancel() remain usable on
        # their own without any of these, exactly as before.
        self._risk_manager = risk_manager
        self._strategy_assignment = strategy_assignment
        self._broker_manager = broker_manager
        # Phase 13: all optional, all default None -- see observability.py's
        # module docstring for why no global instance is created here.
        self._metrics = metrics
        self._audit_trail = audit_trail
        self._alerts = alerts
        # Phase 14: optional LIVE_CANARY authorization gate, consulted
        # between the RiskManager check and broker resolution in execute()
        # below. None means "no canary restrictions" -- exactly today's
        # behavior for every non-canary account/caller.
        self._canary_guard = canary_guard
        # Phase 14.6 -- Blocker A: structural LIVE_CANARY enforcement means
        # canary_guard is no longer "attach it if you remember to"; see
        # execute()'s own logic, which now REQUIRES it whenever the
        # resolved account's execution_mode is LIVE_CANARY, independent of
        # whether the caller happened to pass one to __init__.
        #
        # Central kill switch (optional; None = no additional central gate
        # -- LiveCanaryGuard's own kill switch, RiskManager's own KILL_
        # SWITCH check, and every other existing gate are all unaffected
        # either way).
        self._central_kill_switch = central_kill_switch
        # Persistent idempotency store (optional; None = no persistence
        # layer attached -- execute() falls back to relying solely on
        # RiskManager's/LiveCanaryGuard's own in-memory duplicate-key sets,
        # exactly as before Phase 14.6). Passing one is what makes
        # Blocker D's restart-safe guarantee real for this engine instance.
        self._idempotency_store = idempotency_store
        # Phase 15D.5: optional durable, single-use, exactly-scoped human
        # authorization gate (trading.common.live_authorization). None
        # means "no authorization gate attached" -- exactly today's
        # behavior for every existing caller/test, unchanged. When
        # attached, execute() requires intent.metadata["authorization_id"]
        # to reference a still-AUTHORIZED LiveAuthorization whose scope
        # exactly matches this intent, consumed atomically immediately
        # before the broker call -- see execute()'s own comments.
        self._live_authorization_store = live_authorization_store
        # Observability health is NEVER optional in the sense of "may be
        # absent" -- Blocker C requires that metrics/audit/alert failures
        # can never crash execute() regardless of whether the caller wired
        # in a shared instance. A caller MAY share one across engines (to
        # aggregate health centrally); if none is given, a private one is
        # still always active.
        self._observability_health = observability_health or ObservabilityHealth()
        # Phase 15D.3-R: set by _retry() when the LAST attempt of the most
        # recent place_limit()/place_market_emergency() call ended in a
        # confirmed (never ambiguous) broker rejection with every retry
        # exhausted -- consulted only by execute(), immediately after its
        # own call into one of those methods returns None. See _retry()'s
        # own comment for why this is instance state rather than part of
        # place_limit()/place_market_emergency()'s public return contract.
        self._last_confirmed_rejection: ConfirmedRejectionError | None = None

    @property
    def observability_health(self) -> ObservabilityHealth:
        """Phase 14.6 Blocker C: read this to check whether the
        metrics/audit/alerts layer itself is healthy -- completely
        independent of any single order's own success/failure. A
        monitoring/health-check endpoint should read
        `engine.observability_health.healthy` and
        `engine.observability_health.failures()` as its own signal."""
        return self._observability_health

    def _obs(self, component: str, operation: str, fn: Callable[[], None]) -> None:
        """Every metrics/audit_trail/alerts call in execute() goes through
        here -- see ObservabilityHealth's own docstring (Blocker C) for
        why an exception from any of these must never propagate into an
        order's own execution result."""
        self._observability_health.safe_observe(component, operation, fn)

    # -- rate limiting --------------------------------------------------- #
    def _throttle(self) -> None:
        with self._api_lock:
            wait = self._config.min_api_interval_seconds - (time.monotonic() - self._last_api_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_api_ts = time.monotonic()

    def _retry(self, fn: Callable[[int], object], label: str):
        cfg = self._config
        # Phase 15D.3-R: `place_limit()`/`place_market_emergency()` are
        # public APIs called directly by strategies too, not only via
        # execute() -- their established contract on retries-exhausted is
        # to return None (see test_exhausted_retries_returns_none), and
        # this method must not change that for a direct caller. So a
        # confirmed rejection on the LAST attempt is recorded on the
        # engine instance (self._last_confirmed_rejection), NOT raised
        # here -- only execute() consults that attribute (immediately
        # after its own place_limit()/place_market_emergency() call
        # returns None) to decide whether to persist STATUS_REJECTED.
        # Cleared at the start of every _retry() call so stale state from
        # an unrelated earlier call can never leak into this one.
        self._last_confirmed_rejection = None
        for attempt in range(1, cfg.max_retries + 1):
            try:
                self._throttle()
                result = fn(attempt)
                if result:
                    return result
            except LiveTradingDisabledError:
                raise
            except AmbiguousOrderStateError:
                # Phase 15D-DR: never retry a mutating call whose prior
                # attempt's outcome is unknown -- re-raise immediately,
                # exhausting no further attempts, so the caller (place_limit/
                # place_market_emergency/execute()) can surface this as a
                # distinct, non-retryable state instead of silently looping
                # into a possible second real order.
                self._log(f"[EXEC AMBIGUOUS] {label} attempt {attempt}: broker call outcome unknown -- NOT retrying")
                raise
            except ConfirmedRejectionError as exc:
                self._last_confirmed_rejection = exc
                self._log(f"[EXEC RETRY] {label} attempt {attempt}/{cfg.max_retries} confirmed rejection: {exc}")
            except Exception as exc:
                self._last_confirmed_rejection = None  # a different failure mode supersedes any prior rejection signal
                self._log(f"[EXEC RETRY] {label} attempt {attempt}/{cfg.max_retries} error: {exc}")
            time.sleep(cfg.retry_delay_seconds)
        self._log(f"[EXEC FAILED] {label} - {cfg.max_retries} retries exhausted")
        return None

    def _limit_price(self, symbol: str, side: OrderSide, attempt: int, broker: BrokerClient | None = None) -> float:
        active_broker = broker or self._broker
        quote = active_broker.get_quote(symbol)
        bump = self._config.limit_slippage * attempt
        raw = quote.last_price + bump if side == OrderSide.BUY else max(self._config.tick_size, quote.last_price - bump)
        return round(raw / self._config.tick_size) * self._config.tick_size

    # -- public API -------------------------------------------------------- #
    def place_limit(
        self, symbol: str, side: OrderSide, quantity: int, *, broker: BrokerClient | None = None
    ) -> OrderResult | None:
        """Place a LIMIT order with retry+reprice; manage it in the background if it stays open.

        `broker` defaults to the broker this engine was constructed with —
        pass it explicitly (as execute() does) to route this call through a
        different, BrokerManager-resolved account without needing a
        separate engine instance per account.
        """
        active_broker = broker or self._broker

        if self._config.dry_run:
            price = self._limit_price(symbol, side, 1, broker=active_broker)
            result = OrderResult(
                order_id=f"DRYRUN-{int(time.time() * 1000)}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="FILLED",
                message=f"dry-run at {price}",
            )
            self._log(f"[DRY RUN] LIMIT {side.value} {symbol} qty={quantity} price={price} id={result.order_id}")
            return result

        def _call(attempt: int) -> OrderResult:
            price = self._limit_price(symbol, side, attempt, broker=active_broker)
            try:
                result = active_broker.place_order(symbol, side, quantity, OrderType.LIMIT, price)
            except Exception as exc:
                # Phase 15D-DR: the mutating call itself raised -- we cannot
                # tell whether the broker received/processed this request
                # before failing to respond. Never treat this the same as a
                # confirmed REJECTED (which is safe to retry at a new price).
                raise AmbiguousOrderStateError(f"place_order raised during submission: {exc}") from exc
            if result.status == "REJECTED":
                if result.order_id:
                    # Phase 15D.3-R: a REJECTED status paired with a real
                    # order_id is an inconsistent, suspicious response --
                    # never confidently treat this as a safe, closed
                    # rejection. Route it through the SAME ambiguous/
                    # reconciliation-required path as an outright exception.
                    raise AmbiguousOrderStateError(
                        f"broker reported REJECTED but also returned order_id={result.order_id!r} -- "
                        "inconsistent response, cannot safely confirm rejection"
                    )
                raise ConfirmedRejectionError(result.message or "order rejected")
            self._log(
                f"[EXEC] LIMIT {side.value} {symbol} qty={quantity} price={price} "
                f"id={result.order_id} status={result.status}"
            )
            return result

        result = self._retry(_call, f"place_limit({symbol},{side.value})")
        if result and result.status not in TERMINAL_STATUSES:
            with self._pending_lock:
                self._pending_orders[result.order_id] = (result, active_broker)
            # Manage the pending order on a background thread so placement
            # returns immediately — keeps multiple strategy legs effectively
            # simultaneous instead of serialized behind each other's timeout.
            threading.Thread(target=self._manage_pending, args=(result, active_broker), daemon=True).start()
        return result

    def place_market_emergency(
        self, symbol: str, side: OrderSide, quantity: int, *, broker: BrokerClient | None = None
    ) -> OrderResult | None:
        """MARKET order for the emergency square-off path only. Falls back to
        place_limit() unless the strategy has explicitly enabled market orders."""
        active_broker = broker or self._broker

        if not self._config.allow_market_emergency:
            return self.place_limit(symbol, side, quantity, broker=broker)

        if self._config.dry_run:
            result = OrderResult(
                order_id=f"DRYRUN-{int(time.time() * 1000)}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="FILLED",
                message="dry-run market(emergency)",
            )
            self._log(f"[DRY RUN] MARKET(EMERGENCY) {side.value} {symbol} qty={quantity} id={result.order_id}")
            return result

        def _call(_attempt: int) -> OrderResult:
            try:
                result = active_broker.place_order(symbol, side, quantity, OrderType.MARKET)
            except Exception as exc:
                raise AmbiguousOrderStateError(f"place_order raised during submission: {exc}") from exc
            if result.status == "REJECTED":
                if result.order_id:
                    raise AmbiguousOrderStateError(
                        f"broker reported REJECTED but also returned order_id={result.order_id!r} -- "
                        "inconsistent response, cannot safely confirm rejection"
                    )
                raise ConfirmedRejectionError(result.message or "order rejected")
            self._log(f"[EXEC] MARKET(EMERGENCY) {side.value} {symbol} qty={quantity} id={result.order_id} status={result.status}")
            return result

        return self._retry(_call, f"place_market_emergency({symbol},{side.value})")

    def cancel(self, order_id: str, *, broker: BrokerClient | None = None) -> bool:
        active_broker = broker or self._broker

        if self._config.dry_run:
            self._log(f"[DRY RUN] CANCEL id={order_id}")
            return True

        def _call(_attempt: int) -> bool:
            return bool(active_broker.cancel_order(order_id))

        return bool(self._retry(_call, f"cancel({order_id})"))

    def cancel_all_pending(self) -> None:
        """Cancel every order this engine instance is still tracking as open,
        each against the broker it was actually placed against."""
        with self._pending_lock:
            items = list(self._pending_orders.items())
        for order_id, (_placed, broker) in items:
            self.cancel(order_id, broker=broker)

    # -- OrderIntent entry point --------------------------------------------- #
    def execute(self, intent: OrderIntent, context: RiskContext | None = None) -> ExecutionResult:
        """Full pipeline (Phase 15B.1 order, fail-closed at every step):

            0. Central kill switch (Blocker fix)          -- unconditional, every mode
            1. Idempotency replay (Blocker D)              -- authoritative persistent store
            2. Resolve TradingAccount + AuthorizationState hard gate (Phase 15B.1) --
               NEW: runs BEFORE RiskManager, using the account's own
               TradingAccount.authorization_state (Phase 15B). Independent of,
               and in addition to, every gate below it.
            3. RiskManager.validate()                      -- unchanged, 15 checks
            4. Mode-specific structural gate (Blocker A/E):
                 LIVE_CANARY -> REQUIRE a valid, authorizing LiveCanaryGuard
                 LIVE        -> REQUIRE RiskLimits.is_live_ready()
                 PAPER/SHADOW -> no additional gate
                 anything else -> rejected (invalid execution_mode)
            5. BrokerManager.get_broker() -> BrokerClient.place_order()
            6. Broker response validation (Blocker B)       -- unconditional, every mode
            7. Persist the definitive outcome (Blocker D)
            8. Return ExecutionResult -- metrics/audit/alerts (Blocker C) can
               never affect this return value; see self._obs().

        Requires risk_manager, strategy_assignment and broker_manager to
        have been supplied to __init__ -- place_limit()/
        place_market_emergency()/cancel() remain usable standalone without
        them, exactly as before this method existed.

        Note: Phase 1 does not yet thread intent.limit_price/trigger_price
        into pricing -- place_limit() always computes its own limit price
        from the live quote plus configured slippage, as it already did.

        `context` is passed straight through to RiskManager.validate() --
        optional, defaults to None exactly as validate() already defaults
        it to a fresh RiskContext() internally.
        """
        if self._risk_manager is None or self._strategy_assignment is None or self._broker_manager is None:
            raise RuntimeError(
                "execute(intent) requires risk_manager, strategy_assignment and "
                "broker_manager to be supplied to StrategyExecutionEngine.__init__(). "
                "place_limit()/place_market_emergency()/cancel() remain usable standalone."
            )

        cid, sid = intent.correlation_id, intent.strategy_id

        # -- 0: central kill switch -- checked FIRST, before anything else, ---- #
        # for EVERY execution_mode. See trading/common/kill_switch.py's own
        # docstring for why this is unconditional and independent of every
        # other gate below.
        if self._central_kill_switch is not None and self._central_kill_switch.engaged:
            return ExecutionResult.rejected(
                intent, "central kill switch is engaged; all order execution is blocked"
            )

        # -- 1: idempotency replay (Blocker D) -- authoritative, persistent. ---- #
        if self._idempotency_store is not None and intent.idempotency_key:
            replay = self._check_idempotency_replay(intent)
            if replay is not None:
                return replay
            # NOTE: the atomic claim() against this key happens later, at
            # step 5, immediately before the broker is actually called --
            # not here. Claiming this early would poison the key for any
            # intent that gets rejected by the AuthorizationState gate,
            # RiskManager, or LiveCanaryGuard (none of which persist an
            # idempotency outcome today, by design -- a rejected intent was
            # never sent anywhere, so there's nothing to remember), making
            # a legitimate later retry under the same key impossible to
            # distinguish from "unresolved". See step 5 below.

        started = time.monotonic()

        # -- 2: resolve TradingAccount + AuthorizationState hard gate (Phase 15B.1). ---- #
        # Moved ahead of RiskManager (previously resolved only at old step 3,
        # after the risk check) specifically so this NEW gate can run before
        # RiskManager ever sees the intent -- matching the required pipeline
        # order: StrategyExecutionEngine -> AuthorizationState Gate ->
        # RiskManager -> ExecutionMode/LiveCanaryGuard -> Idempotency -> Broker.
        try:
            account_id = self._strategy_assignment.get_account_id(intent.strategy_id)
        except UnknownAssignmentError as exc:
            return self._fail(intent, str(exc), started=started)

        try:
            account = self._broker_manager.get_account(account_id)
        except UnknownAccountError as exc:
            return self._fail(intent, str(exc), started=started)

        try:
            _check_authorization_state(account, account.execution_mode)
        except AccountAuthorizationError as exc:
            if self._audit_trail is not None:
                self._obs(
                    "audit_trail", "append(AUTHORIZATION_STATE_GATE)",
                    lambda: self._audit_trail.append(
                        EVENT_AUTHORIZATION_STATE_GATE, correlation_id=cid, strategy_id=sid, account_id=account_id,
                        authorization_state=exc.authorization_state, execution_mode=account.execution_mode.value,
                        passed=False, reason=exc.reason,
                    ),
                )
            if self._metrics is not None:
                self._obs("metrics", "record_order_rejected", lambda: self._metrics.record_order_rejected(sid, str(exc)))
            return self._fail(intent, str(exc), started=started)

        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(AUTHORIZATION_STATE_GATE)",
                lambda: self._audit_trail.append(
                    EVENT_AUTHORIZATION_STATE_GATE, correlation_id=cid, strategy_id=sid, account_id=account_id,
                    authorization_state=account.authorization_state.value,
                    execution_mode=account.execution_mode.value, passed=True, reason="",
                ),
            )

        if self._metrics is not None:
            self._obs("metrics", "record_order_intent", lambda: self._metrics.record_order_intent(sid))
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(ORDER_INTENT_CREATED)",
                lambda: self._audit_trail.append(
                    EVENT_ORDER_INTENT_CREATED, correlation_id=cid, strategy_id=sid,
                    symbol=intent.symbol, side=intent.side.value, quantity=intent.quantity,
                    order_type=intent.order_type.value, account_id=intent.account_id,
                    idempotency_key=intent.idempotency_key,
                    # Phase 15D-RECON: this event is the ONLY durable record
                    # of "what was expected" that a later reconciliation
                    # pass can reconstruct from (see
                    # trading.common.reconciliation._reconstruct_expected())
                    # -- exchange/product_type/expiry/strike/option_type are
                    # exactly the extra fields that phase's Area A/E need
                    # and this event didn't carry before.
                    exchange=intent.exchange, product_type=intent.product_type.value,
                    expiry=(intent.instrument.expiry if intent.instrument else ""),
                    strike=(intent.instrument.strike if intent.instrument else None),
                    option_type=(intent.instrument.option_type if intent.instrument else ""),
                ),
            )

        # -- 3: RiskManager -- unchanged, always all 15 checks. ---- #
        check = self._risk_manager.validate(intent, context)
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(RISK_DECISION)",
                lambda: self._audit_trail.append(
                    EVENT_RISK_DECISION, correlation_id=cid, strategy_id=sid,
                    status=check.status, reason=check.reason,
                    checks=[{"name": c.name, "passed": c.passed, "reason": c.reason} for c in check.checks],
                ),
            )
        if not check.allowed:
            if self._metrics is not None:
                self._obs("metrics", "record_order_rejected", lambda: self._metrics.record_order_rejected(sid, check.reason))
            if self._alerts is not None:
                self._obs("alerts", "risk_alerts", lambda: self._raise_risk_alerts(intent, check))
            return ExecutionResult.rejected(intent, check.reason)
        if self._metrics is not None:
            self._obs("metrics", "record_order_approved", lambda: self._metrics.record_order_approved(sid))

        # account/account_id were already resolved at step 2 above, ahead of
        # the AuthorizationState gate.

        # -- 4: mode-specific structural gate (Blocker A + Blocker E). ---- #
        mode = account.execution_mode
        if mode == ExecutionMode.LIVE_CANARY:
            if self._canary_guard is None:
                return self._fail(
                    intent,
                    f"LIVE_CANARY execution for account '{account_id}' requires an attached "
                    "LiveCanaryGuard; none was configured on this StrategyExecutionEngine -- "
                    "rejecting rather than executing unguarded live-capable trading.",
                    started=started,
                )
            ctx = context or RiskContext()
            canary_result = self._canary_guard.authorize(
                intent, daily_pnl=ctx.daily_pnl, strategy_pnl=ctx.strategy_pnl, reference_price=ctx.reference_price,
            )
            if self._audit_trail is not None:
                self._obs(
                    "audit_trail", "append(LIVE_CANARY_AUTHORIZATION)",
                    lambda: self._audit_trail.append(
                        EVENT_LIVE_CANARY_AUTHORIZATION, correlation_id=cid, strategy_id=sid,
                        status=canary_result.status, reason=canary_result.reason,
                        checks=[{"name": c.name, "passed": c.passed, "reason": c.reason} for c in canary_result.checks],
                    ),
                )
            if not canary_result.allowed:
                if self._metrics is not None:
                    self._obs("metrics", "record_order_rejected", lambda: self._metrics.record_order_rejected(sid, canary_result.reason))
                return ExecutionResult.rejected(intent, canary_result.reason)
        elif mode == ExecutionMode.LIVE:
            ready, reason = self._risk_manager.get_limits().is_live_ready()
            if not ready:
                return self._fail(intent, f"LIVE execution blocked for account '{account_id}': {reason}", started=started)
        elif mode not in (ExecutionMode.PAPER, ExecutionMode.SHADOW):
            return self._fail(
                intent, f"account '{account_id}' has an unrecognized execution_mode {mode!r}; refusing to execute",
                started=started,
            )

        # -- 5: broker resolution + order placement. ---- #
        try:
            broker = self._broker_manager.get_broker(account_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a rejection, not raised
            return self._fail(intent, f"could not resolve broker for account '{account_id}': {exc}", started=started)

        # Phase 15D.5: consult the durable human-authorization gate BEFORE
        # the idempotency claim (a rejection here must never poison the
        # idempotency key -- see idempotency_store.py's own "claim right
        # before the broker call" rationale, and the AG7002 lesson about
        # never leaving a key in an unresolved state for a rejection that
        # never reached the broker at all). Runs after every other gate
        # (kill switch, RiskManager, LiveCanaryGuard) has already passed,
        # and is itself never a replacement for any of them -- it only
        # additionally requires a specific, still-valid, exactly-scoped
        # human authorization to exist for this exact intent.
        if self._live_authorization_store is not None:
            auth_id = intent.metadata.get("authorization_id", "")
            if not auth_id:
                return self._fail(
                    intent, "live authorization is required but no authorization_id was present on this intent",
                    started=started,
                )
            order_value = intent.limit_price if intent.limit_price is not None else (
                context.reference_price if context is not None and context.reference_price is not None else None
            )
            if order_value is None:
                return self._fail(
                    intent, "cannot verify authorized order value -- no limit_price or reference_price available",
                    started=started,
                )
            try:
                self._live_authorization_store.try_consume(
                    auth_id, account_id=account_id, broker_id=account.broker_id,
                    credential_reference=account.credential_reference, symbol=intent.symbol,
                    side=intent.side.value, quantity=intent.quantity, order_type=intent.order_type.value,
                    product_type=intent.product_type.value, idempotency_key=intent.idempotency_key,
                    order_value=abs(intent.quantity * order_value), correlation_id=intent.correlation_id,
                    # Phase 15D.7: additive -- when the intent carries an
                    # operator_id (set by LiveAuthorizationWorkflow), it
                    # must match the authorization's own operator_id
                    # exactly, same as every other exact-scope field.
                    # Omitted (None) for any intent that doesn't carry one,
                    # preserving every pre-15D.7 call's exact behavior.
                    operator_id=intent.metadata.get("operator_id"),
                )
            except Exception as exc:  # noqa: BLE001 -- LiveAuthorizationError or a genuine store failure, both fail closed
                return self._fail(intent, f"live authorization check failed: {exc}", started=started)

        # Phase 15D-DR (Area C.8/M.7): atomically claim this idempotency key
        # NOW -- immediately before the broker is actually called, after
        # every rejection-capable gate (authorization, RiskManager,
        # LiveCanaryGuard) has already passed. This closes the race between
        # two concurrent execute() calls for the SAME key without poisoning
        # the key for any intent that never reaches this point.
        if self._idempotency_store is not None and intent.idempotency_key:
            claimed = self._idempotency_store.claim(
                intent.idempotency_key, strategy_id=intent.strategy_id, account_id=account_id,
                intent_hash=compute_intent_hash(intent),
            )
            if not claimed:
                return self._fail(
                    intent,
                    f"idempotency_key {intent.idempotency_key!r} is already being processed by a concurrent "
                    "request (or a prior attempt is unresolved) -- refusing to submit a second broker request "
                    "for the same intent.",
                    started=started,
                )

        if intent.order_type == OrderType.MARKET:
            try:
                order_result = self.place_market_emergency(intent.symbol, intent.side, intent.quantity, broker=broker)
            except AmbiguousOrderStateError as exc:
                return self._handle_ambiguous(intent, account_id, exc, started=started)
        else:
            try:
                order_result = self.place_limit(intent.symbol, intent.side, intent.quantity, broker=broker)
            except AmbiguousOrderStateError as exc:
                return self._handle_ambiguous(intent, account_id, exc, started=started)

        if self._metrics is not None:
            self._obs("metrics", "record_execution_latency", lambda: self._metrics.record_execution_latency(sid, time.monotonic() - started))

        if order_result is None:
            # Phase 15D.3-R: distinguish "every attempt ended in the SAME
            # confirmed, non-ambiguous broker rejection" (a known terminal
            # outcome -- persist STATUS_REJECTED) from the genuinely
            # never-ambiguous case where retries failed BEFORE ever
            # reaching the broker's order-placement call at all (e.g.
            # _limit_price()'s own get_quote() failing -- safe to report
            # as a plain failure and safe to leave unpersisted, see
            # trading/common/idempotency_store.py's module docstring). An
            # AmbiguousOrderStateError from the mutating call itself is
            # caught separately above and never reaches here.
            if self._last_confirmed_rejection is not None:
                return self._handle_confirmed_rejection(intent, account_id, self._last_confirmed_rejection, started=started)
            return self._fail(intent, "order execution failed after retries", started=started)

        # -- 6: broker response validation (Blocker B) -- EVERY mode now, not just canary. ---- #
        validation = validate_broker_response(order_result)
        if not validation.valid:
            self._persist_idempotency(intent, account_id, status=STATUS_FAILED, broker_order_id="", result=None)
            return self._fail(intent, f"broker response failed validation: {validation.reason}", started=started)

        result = ExecutionResult.from_order_result(order_result, intent, account_id=account_id, broker_id=account.broker_id)

        # -- 7: persist the definitive outcome (Blocker D). ---- #
        self._persist_idempotency(
            intent, account_id,
            status=STATUS_REJECTED if order_result.status == "REJECTED" else STATUS_COMPLETED,
            broker_order_id=result.order_id, result=result,
        )

        # -- observability (Blocker C: never allowed to affect `result`). ---- #
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(EXECUTION_RESULT)",
                lambda: self._audit_trail.append(
                    EVENT_EXECUTION_RESULT, correlation_id=cid, strategy_id=sid,
                    success=result.success, status=result.status, message=result.message,
                    account_id=account_id, idempotency_key=intent.idempotency_key, broker_order_id=result.order_id,
                ),
            )
            self._obs(
                "audit_trail", "append(BROKER_ORDER_PLACED)",
                lambda: self._audit_trail.append(
                    EVENT_BROKER_ORDER_PLACED, correlation_id=cid, strategy_id=sid,
                    order_id=result.order_id, broker_order_id=result.order_id, account_id=account_id,
                    broker_id=account.broker_id, idempotency_key=intent.idempotency_key, simulated=broker.is_simulated,
                ),
            )
            if result.status in TERMINAL_STATUSES:
                self._obs(
                    "audit_trail", "append(FILL)",
                    lambda: self._audit_trail.append(
                        EVENT_FILL, correlation_id=cid, strategy_id=sid, order_id=result.order_id,
                        broker_order_id=result.order_id, account_id=account_id,
                        idempotency_key=intent.idempotency_key,
                        filled_quantity=result.filled_quantity, simulated=broker.is_simulated,
                    ),
                )
        if result.status in TERMINAL_STATUSES and self._metrics is not None:
            if broker.is_simulated:
                self._obs("metrics", "record_simulated_fill", lambda: self._metrics.record_simulated_fill(sid))
            else:
                self._obs("metrics", "record_real_fill", lambda: self._metrics.record_real_fill(sid))

        # This return is UNCONDITIONAL once reached: nothing above this
        # line, from this point in the method onward, can raise into the
        # caller -- every metrics/audit_trail/alerts call is wrapped in
        # self._obs(). The caller always receives `result`, carrying the
        # real broker order_id and idempotency-relevant fields, regardless
        # of whether any observability call failed.
        return result

    # -- Blocker D: idempotency replay / persistence ------------------------ #
    def _check_idempotency_replay(self, intent: OrderIntent) -> ExecutionResult | None:
        """Returns a cached ExecutionResult if this exact idempotency_key
        has already reached a definitive broker outcome -- the broker is
        NEVER called again in that case, regardless of process restarts,
        because the store (not any in-memory set) is authoritative.

        Raises IdempotencyKeyReuseError (propagating, deliberately NOT
        caught) if the SAME key is reused for a materially different
        intent -- silently replaying the wrong cached result would be
        worse than a loud failure."""
        from trading.common.idempotency_store import AmbiguousIdempotencyStateError, IdempotencyKeyReuseError

        existing = self._idempotency_store.get(intent.idempotency_key)
        if existing is None:
            return None
        if existing.intent_hash != compute_intent_hash(intent):
            raise IdempotencyKeyReuseError(
                f"idempotency_key {intent.idempotency_key!r} was previously used for a different "
                f"OrderIntent (hash {existing.intent_hash} != {compute_intent_hash(intent)}); refusing "
                "to replay a cached result for a mismatched request, and refusing to submit a new "
                "order under a reused key."
            )
        if existing.status in (STATUS_AMBIGUOUS, STATUS_PENDING):
            # Phase 15D-DR: this exact intent's prior broker outcome was
            # never determined (AMBIGUOUS), or a claim for this key is
            # still outstanding -- possibly a concurrent in-flight request,
            # possibly a crashed process that never reached a definitive
            # outcome (PENDING). Both are equally unresolved: never silently
            # replayed as success, never silently treated as safe-to-resubmit
            # -- propagating, deliberately NOT caught, exactly like
            # IdempotencyKeyReuseError above.
            raise AmbiguousIdempotencyStateError(
                f"idempotency_key {intent.idempotency_key!r} has an unresolved {existing.status} prior outcome; "
                "reconcile the actual broker state (order book / positions) before any further action."
            )
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(IDEMPOTENT_REPLAY)",
                lambda: self._audit_trail.append(
                    "IDEMPOTENT_REPLAY", correlation_id=intent.correlation_id, strategy_id=intent.strategy_id,
                    idempotency_key=intent.idempotency_key, broker_order_id=existing.broker_order_id,
                ),
            )
        if existing.result_json:
            return ExecutionResult.from_json(existing.result_json)
        # A FAILED (broker-response-validation-failure) record has no
        # usable cached ExecutionResult -- replay as the same rejection
        # rather than re-attempting a broker call whose prior outcome was
        # ambiguous enough to fail validation in the first place.
        return ExecutionResult.rejected(intent, f"idempotency_key {intent.idempotency_key!r} previously failed: status={existing.status}")

    def _persist_idempotency(
        self, intent: OrderIntent, account_id: str, *, status: str, broker_order_id: str, result: ExecutionResult | None,
    ) -> None:
        if self._idempotency_store is None or not intent.idempotency_key:
            return
        now = intent.created_at
        record = IdempotencyRecord(
            idempotency_key=intent.idempotency_key, strategy_id=intent.strategy_id, account_id=account_id,
            intent_hash=compute_intent_hash(intent), status=status, broker_order_id=broker_order_id,
            result_json=result.to_json() if result is not None else "",
            created_at=now, updated_at=now,
        )
        # Persistence itself is treated as observability-adjacent for
        # failure-handling purposes: a write failure here must not crash
        # execute() (the broker call already happened) -- but it means
        # Blocker D's guarantee is degraded for THIS specific order, which
        # is exactly the kind of thing ObservabilityHealth exists to
        # surface.
        self._obs("idempotency_store", "put", lambda: self._idempotency_store.put(record))

    def _fail(self, intent: OrderIntent, reason: str, *, started: float, status: str = "REJECTED") -> ExecutionResult:
        """Shared tail for every execute() failure path after the risk
        check passed: records latency/error metrics and an execution_error
        alert, then returns the ExecutionResult shape every caller already
        expects. `status="AMBIGUOUS"` (Phase 17.1 Section 27) is the one
        exception -- see ExecutionResult.ambiguous()'s own docstring for why
        an unknown-outcome result must never be indistinguishable from a
        confirmed REJECTED one."""
        if self._metrics is not None:
            self._obs("metrics", "record_execution_latency", lambda: self._metrics.record_execution_latency(intent.strategy_id, time.monotonic() - started))
            self._obs("metrics", "record_error", lambda: self._metrics.record_error(f"execution:{intent.strategy_id}"))
        if self._alerts is not None:
            self._obs("alerts", "execution_error", lambda: self._alerts.execution_error(intent.strategy_id, reason, correlation_id=intent.correlation_id))
        if status == "AMBIGUOUS":
            return ExecutionResult.ambiguous(intent, reason)
        return ExecutionResult.rejected(intent, reason)

    def _handle_ambiguous(
        self, intent: OrderIntent, account_id: str, exc: "AmbiguousOrderStateError", *, started: float,
    ) -> ExecutionResult:
        """Phase 15D-DR (Area E/J): the broker call that would have
        submitted this order raised an exception with an undetermined
        outcome. Persisted as STATUS_AMBIGUOUS (never STATUS_FAILED --
        that would wrongly imply "definitely did not succeed") so a future
        replay attempt for this exact key is blocked via
        AmbiguousIdempotencyStateError rather than silently retried or
        silently allowed through. This engine never attempts reconciliation
        automatically -- see this class's own module docstring and
        docs/phase-15d-deployment-recovery-safety-report.md."""
        reason = f"broker call outcome is ambiguous -- reconciliation required before any further action: {exc}"
        self._persist_idempotency(intent, account_id, status=STATUS_AMBIGUOUS, broker_order_id="", result=None)
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(AMBIGUOUS_ORDER_STATE)",
                lambda: self._audit_trail.append(
                    "AMBIGUOUS_ORDER_STATE", correlation_id=intent.correlation_id, strategy_id=intent.strategy_id,
                    account_id=account_id, idempotency_key=intent.idempotency_key, reason=str(exc),
                ),
            )
        return self._fail(intent, reason, started=started, status="AMBIGUOUS")  # _fail() records metrics/alerts once

    def _handle_confirmed_rejection(
        self, intent: OrderIntent, account_id: str, exc: "ConfirmedRejectionError", *, started: float,
    ) -> ExecutionResult:
        """Phase 15D.3-R: every retry attempt ended in the SAME definitive,
        broker-confirmed rejection (never an ambiguous/unknown outcome --
        see ConfirmedRejectionError's own docstring). Persisted as
        STATUS_REJECTED, a genuine terminal state, closing the gap where
        this exact scenario (Attempt 2's real AG7002 rejection) previously
        left the idempotency record at STATUS_PENDING forever."""
        reason = f"order rejected by broker (confirmed, no order created): {exc}"
        self._persist_idempotency(intent, account_id, status=STATUS_REJECTED, broker_order_id="", result=None)
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(CONFIRMED_REJECTION)",
                lambda: self._audit_trail.append(
                    "CONFIRMED_REJECTION", correlation_id=intent.correlation_id, strategy_id=intent.strategy_id,
                    account_id=account_id, idempotency_key=intent.idempotency_key, reason=str(exc),
                ),
            )
        return self._fail(intent, reason, started=started)

    def _raise_risk_alerts(self, intent: OrderIntent, check) -> None:  # noqa: ANN001 - RiskCheckResult, avoiding an import cycle concern is moot but kept loose intentionally
        """Classify each failed risk check into the matching named alert.
        KILL_SWITCH and MAX_DAILY_LOSS/MAX_STRATEGY_LOSS get their own
        specific alert type; every other failed check is a generic
        risk_breach. All failed checks are alerted, not just the first."""
        for c in check.checks:
            if c.passed:
                continue
            if c.name == "KILL_SWITCH":
                self._alerts.kill_switch(True, reason=c.reason)
            elif c.name in ("MAX_DAILY_LOSS", "MAX_STRATEGY_LOSS"):
                self._alerts.daily_loss_limit(intent.account_id, 0.0, 0.0, strategy_id=intent.strategy_id)
            else:
                self._alerts.risk_breach(intent.strategy_id, c.name, c.reason, correlation_id=intent.correlation_id)

    # -- pending-order management ------------------------------------------ #
    def _manage_pending(self, placed: OrderResult, broker: BrokerClient) -> None:
        time.sleep(self._config.pending_timeout_seconds)
        get_order = getattr(broker, "get_order", None)
        if get_order is None:
            # Adapter can't report pending status — nothing more we can do.
            self._pop_pending(placed.order_id)
            return

        self._throttle()
        try:
            state = get_order(placed.order_id)
        except NotImplementedError:
            self._pop_pending(placed.order_id)
            return

        if state.status in TERMINAL_STATUSES:
            self._pop_pending(placed.order_id)
            return

        remainder = state.remaining_quantity
        if state.filled_quantity and remainder:
            self._log(
                f"[PARTIAL FILL] {placed.symbol} filled={state.filled_quantity}/{placed.quantity} "
                "- managing remainder only"
            )
        if remainder <= 0:
            self._pop_pending(placed.order_id)
            return

        action = self._config.pending_action
        if action == "CANCEL":
            self.cancel(placed.order_id, broker=broker)
        elif action == "MODIFY":
            modify_order = getattr(broker, "modify_order", None)
            if modify_order is not None:
                price = self._limit_price(placed.symbol, placed.side, 2, broker=broker)
                self._throttle()
                modify_order(placed.order_id, remainder, price)
                self._log(f"[EXEC] MODIFY {placed.symbol} id={placed.order_id} qty={remainder} price={price}")
        elif action == "MARKET" and self._config.allow_market_emergency:
            self.cancel(placed.order_id, broker=broker)
            self.place_market_emergency(placed.symbol, placed.side, remainder, broker=broker)

        self._pop_pending(placed.order_id)

    def _pop_pending(self, order_id: str) -> None:
        with self._pending_lock:
            self._pending_orders.pop(order_id, None)
