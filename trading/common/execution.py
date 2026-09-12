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
from typing import Callable

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
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED,
    IdempotencyRecord,
    IdempotencyStore,
    compute_intent_hash,
)
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_canary import LiveCanaryGuard
from trading.common.observability import (
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
from trading.common.trading_account import ExecutionMode

# Statuses that mean "nothing left to manage" across brokers. Adapters may
# use their own vocabulary beyond this (e.g. "TRIGGER PENDING" is NOT
# terminal) — anything not in this set is treated as still-open.
TERMINAL_STATUSES = {"FILLED", "COMPLETE", "REJECTED", "CANCELLED"}


@dataclass(frozen=True)
class OrderState:
    """Point-in-time status of a previously placed order. An adapter that
    supports pending-order management returns this from get_order()."""

    order_id: str
    status: str
    filled_quantity: int
    remaining_quantity: int


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
        # Observability health is NEVER optional in the sense of "may be
        # absent" -- Blocker C requires that metrics/audit/alert failures
        # can never crash execute() regardless of whether the caller wired
        # in a shared instance. A caller MAY share one across engines (to
        # aggregate health centrally); if none is given, a private one is
        # still always active.
        self._observability_health = observability_health or ObservabilityHealth()

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
        for attempt in range(1, cfg.max_retries + 1):
            try:
                self._throttle()
                result = fn(attempt)
                if result:
                    return result
            except LiveTradingDisabledError:
                raise
            except Exception as exc:
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
            result = active_broker.place_order(symbol, side, quantity, OrderType.LIMIT, price)
            if result.status == "REJECTED":
                raise RuntimeError(result.message or "order rejected")
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
            result = active_broker.place_order(symbol, side, quantity, OrderType.MARKET)
            if result.status == "REJECTED":
                raise RuntimeError(result.message or "order rejected")
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
        """Full pipeline (Phase 14.6 order, fail-closed at every step):

            0. Central kill switch (Blocker fix)          -- unconditional, every mode
            1. Idempotency replay (Blocker D)              -- authoritative persistent store
            2. RiskManager.validate()                      -- unchanged, 15 checks
            3. Resolve TradingAccount                       -- needed for mode branching below
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

        if self._metrics is not None:
            self._obs("metrics", "record_order_intent", lambda: self._metrics.record_order_intent(sid))
        if self._audit_trail is not None:
            self._obs(
                "audit_trail", "append(ORDER_INTENT_CREATED)",
                lambda: self._audit_trail.append(
                    EVENT_ORDER_INTENT_CREATED, correlation_id=cid, strategy_id=sid,
                    symbol=intent.symbol, side=intent.side.value, quantity=intent.quantity,
                    order_type=intent.order_type.value, account_id=intent.account_id,
                ),
            )

        started = time.monotonic()

        # -- 2: RiskManager -- unchanged, always all 15 checks. ---- #
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

        # -- 3: resolve the TradingAccount (metadata only -- does not connect). ---- #
        try:
            account_id = self._strategy_assignment.get_account_id(intent.strategy_id)
        except UnknownAssignmentError as exc:
            return self._fail(intent, str(exc), started=started)

        try:
            account = self._broker_manager.get_account(account_id)
        except UnknownAccountError as exc:
            return self._fail(intent, str(exc), started=started)

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

        if intent.order_type == OrderType.MARKET:
            order_result = self.place_market_emergency(intent.symbol, intent.side, intent.quantity, broker=broker)
        else:
            order_result = self.place_limit(intent.symbol, intent.side, intent.quantity, broker=broker)

        if self._metrics is not None:
            self._obs("metrics", "record_execution_latency", lambda: self._metrics.record_execution_latency(sid, time.monotonic() - started))

        if order_result is None:
            # Genuinely ambiguous: the broker never definitively answered
            # (retries exhausted). Deliberately NOT persisted to the
            # idempotency store as a definitive outcome -- see
            # trading/common/idempotency_store.py's module docstring for
            # why this specific case is an honest, documented boundary.
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
                ),
            )
            self._obs(
                "audit_trail", "append(BROKER_ORDER_PLACED)",
                lambda: self._audit_trail.append(
                    EVENT_BROKER_ORDER_PLACED, correlation_id=cid, strategy_id=sid,
                    order_id=result.order_id, account_id=account_id, broker_id=account.broker_id, simulated=broker.is_simulated,
                ),
            )
            if result.status in TERMINAL_STATUSES:
                self._obs(
                    "audit_trail", "append(FILL)",
                    lambda: self._audit_trail.append(
                        EVENT_FILL, correlation_id=cid, strategy_id=sid, order_id=result.order_id,
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
        from trading.common.idempotency_store import IdempotencyKeyReuseError

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

    def _fail(self, intent: OrderIntent, reason: str, *, started: float) -> ExecutionResult:
        """Shared tail for every execute() failure path after the risk
        check passed: records latency/error metrics and an execution_error
        alert, then returns the same ExecutionResult.rejected() shape every
        caller already expects."""
        if self._metrics is not None:
            self._obs("metrics", "record_execution_latency", lambda: self._metrics.record_execution_latency(intent.strategy_id, time.monotonic() - started))
            self._obs("metrics", "record_error", lambda: self._metrics.record_error(f"execution:{intent.strategy_id}"))
        if self._alerts is not None:
            self._obs("alerts", "execution_error", lambda: self._alerts.execution_error(intent.strategy_id, reason, correlation_id=intent.correlation_id))
        return ExecutionResult.rejected(intent, reason)

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
