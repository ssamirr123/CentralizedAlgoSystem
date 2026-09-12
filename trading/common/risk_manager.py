"""
RiskManager -- the centralized pre-trade interception point every
OrderIntent must pass through before it reaches the ExecutionEngine:

    OrderIntent -> RiskManager -> ExecutionEngine

Fourteen named checks run on every validate() call, ALWAYS all fourteen
(not stopping at the first failure), so every decision carries a complete
audit trail -- see RiskCheckResult.checks. The overall decision is
APPROVED only if every check passes; REJECTED otherwise, with `reason`
joining every failed check's own reason.

Fail-closed, by design, in two distinct senses:
  1. Any unexpected exception during evaluation is caught and turned into
     a REJECTED result (an INTERNAL_ERROR check outcome) -- it is never
     allowed to propagate as a silent approval or an unhandled crash.
  2. Checks that are inherently always-applicable (strategy/account
     enabled, kill switch, duplicate protection, instrument validation)
     are evaluated unconditionally with no way to disable them. Checks
     that need external numeric configuration (quantity/exposure/loss/
     value limits) are OPTIONAL -- a limit of None means "not configured,
     not enforced" rather than "configured to zero, blocks everything".
     This is a deliberate choice, not an oversight: defaulting every limit
     to a hard 0 would make RiskManager() (used by every existing caller
     -- ExecutionEngine, the Phase 3 shadow bridge, the Phase 4/5B demos)
     reject every single order the moment this file changed, which is a
     worse failure mode than "this specific limit isn't enforced until
     someone configures it". "Fail closed" here means never approving
     something that failed a check that DID run -- not inventing limits
     nobody asked for.

Reused, not duplicated: STRATEGY_ENABLED/ACCOUNT_ENABLED delegate to the
existing StrategyAssignment (Phase 1); EXECUTION_MODE_ALLOWED reads the
existing TradingAccount.execution_mode (Phase 1/5B) via the new
StrategyAssignment.get_account() accessor.

No Angel One (or any broker) dependency anywhere in this file: every check
operates on the OrderIntent itself, RiskLimits (static config), and
RiskContext (caller-supplied point-in-time facts -- current position/
exposure/P&L/kill-switch state) -- never a broker SDK call.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone

from trading.common.broker import OrderSide, OrderType
from trading.common.order_intent import OrderIntent
from trading.common.strategy_assignment import (
    InvalidAssignmentError,
    StrategyAssignment,
    UnknownAssignmentError,
)
from trading.common.trading_account import ExecutionMode

_ALLOWED_EXCHANGES = {"NFO", "NSE", "BSE", "BFO", "MCX"}

# Simplified NSE cash/F&O session window (IST, local wall clock), Mon-Fri.
# Deliberately does NOT know about exchange holidays -- see
# MARKET_SESSION_VALIDATION's docstring for why that's out of scope here.
_SESSION_START = dt_time(9, 15)
_SESSION_END = dt_time(15, 30)


@dataclass(frozen=True)
class RiskCheckOutcome:
    """One named check's own result -- always recorded, whether it passed
    or failed, so RiskCheckResult.checks is a complete audit trail."""

    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class RiskCheckResult:
    """Backward compatible with Phase 1's shape (`allowed`, `reason`,
    `.allow()`, `.deny()`) -- every field below `reason` is new and
    additive. `reason` is every failed check's own reason, joined."""

    allowed: bool
    reason: str = ""
    status: str = "REJECTED"  # "APPROVED" | "REJECTED" -- explicit, not just a boolean
    timestamp: str = ""
    strategy_id: str = ""
    account_id: str = ""
    correlation_id: str = ""
    checks: tuple[RiskCheckOutcome, ...] = ()

    @classmethod
    def allow(cls) -> "RiskCheckResult":
        return cls(allowed=True, status="APPROVED")

    @classmethod
    def deny(cls, reason: str) -> "RiskCheckResult":
        return cls(allowed=False, reason=reason, status="REJECTED")


#: Blocker E (Phase 14.6) -- the fields LIVE execution requires to be
#: explicitly, positively configured before it may proceed. "max exposure"
#: from the Phase 14.6 brief maps to BOTH exposure fields below (strategy
#: AND account) -- requiring only one would leave the other silently
#: unenforced for LIVE, which is exactly the class of gap this exists to
#: close. max_orders_per_day is new in Phase 14.6 (see RiskLimits' own
#: field below) -- LIVE has never had an order-count cap until now.
LIVE_REQUIRED_LIMIT_FIELDS: tuple[str, ...] = (
    "max_order_quantity", "max_order_value", "max_daily_loss", "max_strategy_loss",
    "max_orders_per_day", "max_strategy_exposure", "max_account_exposure",
)


@dataclass(frozen=True)
class RiskLimits:
    """Static, configured risk thresholds. Every field defaults to None =
    "not configured, this check always passes" -- see the module
    docstring for why that's the deliberate default, not a lazy one.

    That default remains correct for PAPER/SHADOW (Phase 14.6 Blocker E
    test 5) -- it is LIVE specifically that must never be allowed to run
    against an unconfigured RiskLimits. See is_live_ready() below, which
    StrategyExecutionEngine.execute() consults before ever calling a
    broker for a LIVE-mode account."""

    max_order_quantity: int | None = None
    max_position_quantity: int | None = None
    max_strategy_exposure: float | None = None
    max_account_exposure: float | None = None
    max_daily_loss: float | None = None  # magnitude; loss must not exceed this
    max_strategy_loss: float | None = None
    max_order_value: float | None = None
    # Phase 14.6: a per-calendar-day order-count cap, the same shape as
    # LiveCanaryGuard's own max_orders_per_day (Phase 14) -- now available
    # to ANY RiskManager, not just canary accounts. None = unconfigured,
    # matching every other field's convention above.
    max_orders_per_day: int | None = None

    def is_live_ready(self) -> tuple[bool, str]:
        """Blocker E: True only if every field in LIVE_REQUIRED_LIMIT_FIELDS
        is set to an explicit, POSITIVE number. None (unconfigured), zero,
        and negative are all treated identically -- as "not a valid live
        limit" -- per the explicit instruction that null/zero/empty must
        never be silently read as "unlimited". This method makes no
        change to what PAPER/SHADOW/LIVE_CANARY require; it exists purely
        for the caller (StrategyExecutionEngine) to consult when, and only
        when, an intent targets a LIVE-mode account.

        Phase 14.7 fix: a MALFORMED value (e.g. a string where a number is
        expected -- RiskLimits is a plain dataclass and does not enforce
        types at runtime) used to raise TypeError out of this method
        uncaught, which would have propagated out of
        StrategyExecutionEngine.execute() as an unhandled crash rather
        than a clean rejection -- inconsistent with RiskManager.validate()
        itself, which already guarantees "any unexpected exception during
        evaluation is caught and turned into a REJECTED result... never
        allowed to propagate as a silent approval or an unhandled crash"
        (see this module's own docstring). A malformed value is now
        treated exactly like a missing one: fail closed, never crash."""
        missing = []
        for name in LIVE_REQUIRED_LIMIT_FIELDS:
            value = getattr(self, name)
            try:
                invalid = value is None or value <= 0
            except TypeError:
                invalid = True
            if invalid:
                missing.append(name)
        if missing:
            return False, f"LIVE requires explicit, positive risk limits for: {', '.join(missing)}"
        return True, ""


@dataclass(frozen=True)
class RiskContext:
    """Point-in-time facts the caller already knows and RiskManager should
    not have to re-derive (and, per the module docstring, must not fetch
    from a broker itself). Every field defaults to "this check passes
    unless you tell me otherwise", so existing callers that construct
    RiskContext() (or pass none at all) see no behavior change."""

    strategy_enabled: bool = True
    allowed_execution_modes: frozenset[ExecutionMode] | None = None  # None = all modes allowed
    current_position_quantity: int = 0  # existing signed qty for intent.symbol, before this intent
    strategy_exposure: float = 0.0  # existing aggregate notional exposure for the strategy
    account_exposure: float = 0.0  # existing aggregate notional exposure for the account
    daily_pnl: float = 0.0  # today's P&L for the account (negative = loss)
    strategy_pnl: float = 0.0  # today's P&L for the strategy specifically (negative = loss)
    kill_switch_engaged: bool = False
    enforce_market_hours: bool = False  # default False: MARKET_SESSION_VALIDATION always passes unless opted in
    now: datetime | None = None  # injectable "current time", for deterministic tests; defaults to real UTC now
    reference_price: float | None = None  # used for ORDER_VALUE_LIMIT when intent.limit_price is None


class RiskManager:
    def __init__(self, strategy_assignment: StrategyAssignment, limits: RiskLimits | None = None) -> None:
        self._strategy_assignment = strategy_assignment
        self._limits = limits or RiskLimits()
        self._lock = threading.Lock()
        self._seen_idempotency_keys: set[str] = set()
        # Phase 14.6: per-calendar-day order counter backing
        # max_orders_per_day, keyed by strategy_id so one strategy's count
        # never limits another's.
        self._orders_today: dict[tuple[str, date], int] = {}

    def get_limits(self) -> RiskLimits:
        """Read-only accessor for the configured RiskLimits -- additive,
        for callers (e.g. Phase 11's control-center API) that need to
        display current thresholds without reaching into this class's
        private state."""
        return self._limits

    def validate(self, intent: OrderIntent, context: RiskContext | None = None) -> RiskCheckResult:
        context = context or RiskContext()
        checks: list[RiskCheckOutcome] = []

        try:
            checks.append(self._check_strategy_enabled(intent, context))
            checks.append(self._check_account_enabled(intent, context))
            checks.append(self._check_execution_mode_allowed(intent, context))
            checks.append(self._check_instrument_validation(intent, context))
            checks.append(self._check_max_order_quantity(intent, context))
            checks.append(self._check_max_position_quantity(intent, context))
            checks.append(self._check_max_strategy_exposure(intent, context))
            checks.append(self._check_max_account_exposure(intent, context))
            checks.append(self._check_max_daily_loss(intent, context))
            checks.append(self._check_max_strategy_loss(intent, context))
            checks.append(self._check_duplicate_order(intent, context))
            checks.append(self._check_market_session(intent, context))
            checks.append(self._check_kill_switch(intent, context))
            checks.append(self._check_order_value_limit(intent, context))
            checks.append(self._check_max_orders_per_day(intent, context))
        except Exception as exc:  # fail closed -- never let evaluation itself become a silent approval
            checks.append(RiskCheckOutcome(name="INTERNAL_ERROR", passed=False, reason=str(exc)))

        result = self._build_result(intent, checks)
        if result.allowed:
            self._record_order_approved_for_daily_count(intent, context)
        return result

    # -- result assembly ------------------------------------------------------- #
    def _build_result(self, intent: OrderIntent, checks: list[RiskCheckOutcome]) -> RiskCheckResult:
        failed = [c for c in checks if not c.passed]
        approved = not failed
        reason = "; ".join(f"{c.name}: {c.reason}" for c in failed)
        return RiskCheckResult(
            allowed=approved,
            reason=reason,
            status="APPROVED" if approved else "REJECTED",
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy_id=intent.strategy_id,
            account_id=intent.account_id,
            correlation_id=intent.correlation_id,
            checks=tuple(checks),
        )

    # -- 1: strategy enabled ----------------------------------------------------- #
    def _check_strategy_enabled(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        if not context.strategy_enabled:
            return RiskCheckOutcome("STRATEGY_ENABLED", False, f"strategy '{intent.strategy_id}' is disabled")
        return RiskCheckOutcome("STRATEGY_ENABLED", True)

    # -- 2: account enabled (+ assignment exists) --------------------------------- #
    def _check_account_enabled(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        try:
            self._strategy_assignment.validate(intent.strategy_id)
        except (UnknownAssignmentError, InvalidAssignmentError) as exc:
            return RiskCheckOutcome("ACCOUNT_ENABLED", False, str(exc))
        return RiskCheckOutcome("ACCOUNT_ENABLED", True)

    # -- 3: execution mode allowed ------------------------------------------------- #
    def _check_execution_mode_allowed(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        if context.allowed_execution_modes is None:
            return RiskCheckOutcome("EXECUTION_MODE_ALLOWED", True)
        try:
            account = self._strategy_assignment.get_account(intent.strategy_id)
        except (UnknownAssignmentError, InvalidAssignmentError):
            # Already reported by ACCOUNT_ENABLED -- don't double-report as a failure here.
            return RiskCheckOutcome("EXECUTION_MODE_ALLOWED", True, "skipped: no valid assignment")
        if account.execution_mode not in context.allowed_execution_modes:
            allowed = ", ".join(sorted(m.value for m in context.allowed_execution_modes))
            return RiskCheckOutcome(
                "EXECUTION_MODE_ALLOWED", False,
                f"account '{account.account_id}' execution_mode={account.execution_mode.value!r} not in allowed set ({{{allowed}}})",
            )
        return RiskCheckOutcome("EXECUTION_MODE_ALLOWED", True)

    # -- 13: instrument validation (kept early -- other checks assume a sane intent) - #
    def _check_instrument_validation(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        if not intent.symbol:
            return RiskCheckOutcome("INSTRUMENT_VALIDATION", False, "symbol is empty")
        if not intent.exchange:
            return RiskCheckOutcome("INSTRUMENT_VALIDATION", False, "exchange is empty")
        if intent.exchange.upper() not in _ALLOWED_EXCHANGES:
            return RiskCheckOutcome("INSTRUMENT_VALIDATION", False, f"exchange '{intent.exchange}' is not a recognized exchange")
        if not isinstance(intent.side, OrderSide):
            return RiskCheckOutcome("INSTRUMENT_VALIDATION", False, f"invalid order side: {intent.side!r}")
        if not isinstance(intent.order_type, OrderType):
            return RiskCheckOutcome("INSTRUMENT_VALIDATION", False, f"invalid order type: {intent.order_type!r}")
        if intent.order_type == OrderType.LIMIT and (intent.limit_price is None or intent.limit_price <= 0):
            return RiskCheckOutcome("INSTRUMENT_VALIDATION", False, "LIMIT order requires a positive limit_price")
        return RiskCheckOutcome("INSTRUMENT_VALIDATION", True)

    # -- 4: maximum order quantity ------------------------------------------------- #
    def _check_max_order_quantity(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        if intent.quantity <= 0:
            return RiskCheckOutcome("MAX_ORDER_QUANTITY", False, "quantity must be positive")
        limit = self._limits.max_order_quantity
        if limit is not None and intent.quantity > limit:
            return RiskCheckOutcome("MAX_ORDER_QUANTITY", False, f"quantity {intent.quantity} exceeds max_order_quantity {limit}")
        return RiskCheckOutcome("MAX_ORDER_QUANTITY", True)

    # -- 5: maximum position quantity ------------------------------------------------ #
    def _check_max_position_quantity(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_position_quantity
        if limit is None:
            return RiskCheckOutcome("MAX_POSITION_QUANTITY", True)
        signed = intent.quantity if intent.side == OrderSide.BUY else -intent.quantity
        resulting = abs(context.current_position_quantity + signed)
        if resulting > limit:
            return RiskCheckOutcome(
                "MAX_POSITION_QUANTITY", False,
                f"resulting position {resulting} would exceed max_position_quantity {limit}",
            )
        return RiskCheckOutcome("MAX_POSITION_QUANTITY", True)

    # -- 6: maximum strategy exposure -------------------------------------------------- #
    def _check_max_strategy_exposure(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_strategy_exposure
        if limit is None:
            return RiskCheckOutcome("MAX_STRATEGY_EXPOSURE", True)
        order_value = self._order_value(intent, context)
        resulting = context.strategy_exposure + order_value
        if resulting > limit:
            return RiskCheckOutcome(
                "MAX_STRATEGY_EXPOSURE", False,
                f"resulting strategy exposure {resulting:.2f} would exceed max_strategy_exposure {limit:.2f}",
            )
        return RiskCheckOutcome("MAX_STRATEGY_EXPOSURE", True)

    # -- 7: maximum account exposure --------------------------------------------------- #
    def _check_max_account_exposure(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_account_exposure
        if limit is None:
            return RiskCheckOutcome("MAX_ACCOUNT_EXPOSURE", True)
        order_value = self._order_value(intent, context)
        resulting = context.account_exposure + order_value
        if resulting > limit:
            return RiskCheckOutcome(
                "MAX_ACCOUNT_EXPOSURE", False,
                f"resulting account exposure {resulting:.2f} would exceed max_account_exposure {limit:.2f}",
            )
        return RiskCheckOutcome("MAX_ACCOUNT_EXPOSURE", True)

    # -- 8: maximum daily loss ------------------------------------------------------------ #
    def _check_max_daily_loss(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_daily_loss
        if limit is None:
            return RiskCheckOutcome("MAX_DAILY_LOSS", True)
        if context.daily_pnl <= -abs(limit):
            return RiskCheckOutcome(
                "MAX_DAILY_LOSS", False,
                f"daily P&L {context.daily_pnl:.2f} has breached max_daily_loss {limit:.2f}",
            )
        return RiskCheckOutcome("MAX_DAILY_LOSS", True)

    # -- 9: maximum strategy loss ------------------------------------------------------------- #
    def _check_max_strategy_loss(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_strategy_loss
        if limit is None:
            return RiskCheckOutcome("MAX_STRATEGY_LOSS", True)
        if context.strategy_pnl <= -abs(limit):
            return RiskCheckOutcome(
                "MAX_STRATEGY_LOSS", False,
                f"strategy P&L {context.strategy_pnl:.2f} has breached max_strategy_loss {limit:.2f}",
            )
        return RiskCheckOutcome("MAX_STRATEGY_LOSS", True)

    # -- 10: duplicate order protection ------------------------------------------------------- #
    def _check_duplicate_order(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        key = intent.idempotency_key
        if not key:
            return RiskCheckOutcome("DUPLICATE_ORDER_PROTECTION", True, "no idempotency_key supplied -- not tracked")
        with self._lock:
            if key in self._seen_idempotency_keys:
                return RiskCheckOutcome("DUPLICATE_ORDER_PROTECTION", False, f"idempotency_key {key!r} was already approved")
            self._seen_idempotency_keys.add(key)
        return RiskCheckOutcome("DUPLICATE_ORDER_PROTECTION", True)

    # -- 11: market/session validation ------------------------------------------------------------ #
    def _check_market_session(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        if not context.enforce_market_hours:
            return RiskCheckOutcome("MARKET_SESSION_VALIDATION", True, "not enforced")
        now = context.now or datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return RiskCheckOutcome("MARKET_SESSION_VALIDATION", False, f"{now.date()} is a weekend")
        if not (_SESSION_START <= now.time() <= _SESSION_END):
            return RiskCheckOutcome(
                "MARKET_SESSION_VALIDATION", False,
                f"{now.time()} is outside the session window {_SESSION_START}-{_SESSION_END}",
            )
        return RiskCheckOutcome("MARKET_SESSION_VALIDATION", True)

    # -- 12: kill switch --------------------------------------------------------------------------- #
    def _check_kill_switch(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        if context.kill_switch_engaged:
            return RiskCheckOutcome("KILL_SWITCH", False, "kill switch is engaged")
        return RiskCheckOutcome("KILL_SWITCH", True)

    # -- 14: order value limit ----------------------------------------------------------------------- #
    def _check_order_value_limit(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_order_value
        if limit is None:
            return RiskCheckOutcome("ORDER_VALUE_LIMIT", True)
        value = self._order_value(intent, context)
        if value is None:
            return RiskCheckOutcome("ORDER_VALUE_LIMIT", False, "no price available to compute order value")
        if value > limit:
            return RiskCheckOutcome("ORDER_VALUE_LIMIT", False, f"order value {value:.2f} exceeds max_order_value {limit:.2f}")
        return RiskCheckOutcome("ORDER_VALUE_LIMIT", True)

    # -- 15 (Phase 14.6): maximum number of orders per day ------------------------------------------------- #
    def _check_max_orders_per_day(self, intent: OrderIntent, context: RiskContext) -> RiskCheckOutcome:
        limit = self._limits.max_orders_per_day
        if limit is None:
            return RiskCheckOutcome("MAX_ORDERS_PER_DAY", True)
        key = (intent.strategy_id, (context.now or datetime.now(timezone.utc)).date())
        with self._lock:
            count = self._orders_today.get(key, 0)
        if count >= limit:
            return RiskCheckOutcome(
                "MAX_ORDERS_PER_DAY", False,
                f"{count} orders already approved today for '{intent.strategy_id}'; max_orders_per_day is {limit}",
            )
        return RiskCheckOutcome("MAX_ORDERS_PER_DAY", True)

    def _record_order_approved_for_daily_count(self, intent: OrderIntent, context: RiskContext) -> None:
        """Only called once every check has passed -- a rejected intent
        must never consume the day's order-count budget. Mirrors
        LiveCanaryGuard's own _increment_order_count() precisely."""
        key = (intent.strategy_id, (context.now or datetime.now(timezone.utc)).date())
        with self._lock:
            self._orders_today[key] = self._orders_today.get(key, 0) + 1

    # -- shared helper ------------------------------------------------------------------------------------ #
    def _order_value(self, intent: OrderIntent, context: RiskContext) -> float | None:
        price = intent.limit_price if intent.limit_price is not None else context.reference_price
        if price is None:
            return None
        return abs(intent.quantity * price)
