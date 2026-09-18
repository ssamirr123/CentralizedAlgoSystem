"""
Phase 14 -- LIVE_CANARY authorization: the safety gate that sits between
RiskManager and StrategyExecutionEngine for any intent targeting an
execution_mode=LIVE_CANARY account:

    Strategy -> OrderIntent -> RiskManager -> LIVE_CANARY authorization
        -> ExecutionEngine -> BrokerAdapter

LiveCanaryGuard adds CANARY-SPECIFIC checks ON TOP OF (never instead of,
never bypassing) RiskManager's own 14 checks -- an intent must pass BOTH
gates. Like RiskManager, LiveCanaryGuard NEVER calls a broker itself; it
only inspects the OrderIntent/caller-supplied numbers and its own
internal counters. The broker adapter remains the only component allowed
to call a real order-placement API -- nothing here changes that.

IMPORTANT: building this module is not authorization to place a live
order. LIVE_CANARY is off by default (a TradingAccount must be
deliberately configured with execution_mode=LIVE_CANARY, and a
LiveCanaryGuard must be deliberately constructed and wired into
StrategyExecutionEngine for its checks to run at all), every limit is
fail-closed (CanaryLimits has no defaults -- see its own docstring), and
the actual go/no-go decision for a specific broker/account is made by
trading/preflight/live_canary.py (`python -m trading.preflight.live_canary`),
which must exit non-zero unless every check -- both the ones in this
module and several outside it (real-account validation, adapter
validation status, dedicated-account configuration) -- passes.

Safety requirements this module implements (12, per the Phase 14 brief):
  1.  Dedicated trading account   -> _check_dedicated_account
  2.  Maximum quantity            -> _check_max_quantity
  3.  Maximum order value         -> _check_max_order_value
  4.  Maximum daily loss          -> _check_max_daily_loss
  5.  Maximum strategy loss       -> _check_max_strategy_loss
  6.  Maximum number of orders    -> _check_max_orders_per_day
  7.  Kill switch                 -> _check_kill_switch / engage_kill_switch()
  8.  Duplicate-order protection  -> _check_duplicate
  9.  Idempotency                 -> _check_idempotency_present
  10. Broker response validation  -> validate_broker_response()   (post-call)
  11. Position reconciliation     -> reconcile_position()          (post-call)
  12. Emergency shutdown          -> emergency_shutdown()

Checks 1-9 run inside authorize(), BEFORE the broker is ever called.
Checks 10-11 apply AFTER the broker responds (StrategyExecutionEngine
calls them once it has an OrderResult) -- they cannot run any earlier
because there is nothing to validate/reconcile before a broker has
responded. Check 12 (emergency shutdown) can be triggered at any time and
is consulted by every subsequent authorize() call via check 7's sibling,
_check_shutdown().
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date

from trading.common.alerts import AlertManager
from trading.common.broker import OrderResult
from trading.common.broker_response_validation import validate_broker_response
from trading.common.order_intent import OrderIntent


@dataclass(frozen=True)
class CanaryCheckOutcome:
    """One named check's own result -- always recorded, mirroring
    RiskManager's RiskCheckOutcome exactly, for the same reason: a
    complete audit trail regardless of pass/fail."""

    name: str
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class CanaryAuthorizationResult:
    allowed: bool
    reason: str = ""
    status: str = "REJECTED"  # "AUTHORIZED" | "REJECTED"
    checks: tuple[CanaryCheckOutcome, ...] = ()

    @classmethod
    def allow(cls, checks: list[CanaryCheckOutcome]) -> "CanaryAuthorizationResult":
        return cls(allowed=True, status="AUTHORIZED", checks=tuple(checks))

    @classmethod
    def deny(cls, reason: str, checks: list[CanaryCheckOutcome]) -> "CanaryAuthorizationResult":
        return cls(allowed=False, reason=reason, status="REJECTED", checks=tuple(checks))


@dataclass(frozen=True)
class CanaryLimits:
    """Every field is REQUIRED -- no defaults, anywhere. This is the
    deliberate opposite of RiskManager's RiskLimits (where None means "not
    configured, this check always passes"): a canary with an unconfigured
    limit is a contradiction. A human must consciously choose every number
    before a CanaryLimits (and therefore a LiveCanaryGuard) can even be
    constructed -- __post_init__ enforces every value is positive and
    account_id is non-empty, raising ValueError otherwise."""

    account_id: str
    max_order_quantity: int
    max_order_value: float
    max_daily_loss: float
    max_strategy_loss: float
    max_orders_per_day: int

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("CanaryLimits.account_id must be set to a dedicated canary account id")
        for field_name, value in (
            ("max_order_quantity", self.max_order_quantity),
            ("max_order_value", self.max_order_value),
            ("max_daily_loss", self.max_daily_loss),
            ("max_strategy_loss", self.max_strategy_loss),
            ("max_orders_per_day", self.max_orders_per_day),
        ):
            # Phase 14.7 fix: a malformed (non-numeric) value used to raise
            # a raw, uncaught TypeError here instead of the ValueError this
            # class's own docstring promises for "otherwise" -- a malformed
            # value is now treated identically to an out-of-range one.
            try:
                invalid = value <= 0
            except TypeError:
                raise ValueError(f"CanaryLimits.{field_name} must be a positive number, got {value!r}") from None
            if invalid:
                raise ValueError(f"CanaryLimits.{field_name} must be a positive number, got {value!r}")


class LiveCanaryGuard:
    def __init__(self, limits: CanaryLimits, *, alerts: AlertManager | None = None) -> None:
        self._limits = limits
        self._alerts = alerts
        self._lock = threading.Lock()
        self._seen_idempotency_keys: set[str] = set()
        self._orders_today: dict[date, int] = {}
        self._kill_switch_engaged = False
        self._shutdown = False
        self._shutdown_reason = ""

    @property
    def limits(self) -> CanaryLimits:
        return self._limits

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    # -- 7: kill switch ------------------------------------------------------- #
    def engage_kill_switch(self, *, reason: str = "", by: str = "") -> None:
        with self._lock:
            self._kill_switch_engaged = True
        if self._alerts is not None:
            self._alerts.kill_switch(True, by=by, reason=reason)

    def disengage_kill_switch(self, *, by: str = "") -> None:
        with self._lock:
            self._kill_switch_engaged = False
        if self._alerts is not None:
            self._alerts.kill_switch(False, by=by)

    # -- 12: emergency shutdown ------------------------------------------------ #
    def emergency_shutdown(self, reason: str, *, by: str = "") -> None:
        """Immediately blocks every future authorize() call on THIS
        instance. There is no un-shutdown method, by design -- resuming
        requires constructing a fresh LiveCanaryGuard, which requires a
        fresh, deliberate CanaryLimits (and therefore a fresh human
        decision), not a single flag flip."""
        with self._lock:
            self._shutdown = True
            self._shutdown_reason = reason
            self._kill_switch_engaged = True
        if self._alerts is not None:
            self._alerts.emergency_shutdown(reason, by=by)

    # -- the authorization gate (checks 1, 2, 3, 4, 5, 6, 7, 8, 9) --------------- #
    def authorize(
        self, intent: OrderIntent, *, daily_pnl: float = 0.0, strategy_pnl: float = 0.0,
        reference_price: float | None = None,
    ) -> CanaryAuthorizationResult:
        checks: list[CanaryCheckOutcome] = []
        try:
            checks.append(self._check_shutdown())
            checks.append(self._check_kill_switch())
            checks.append(self._check_dedicated_account(intent))
            checks.append(self._check_idempotency_present(intent))
            checks.append(self._check_duplicate(intent))
            checks.append(self._check_max_quantity(intent))
            checks.append(self._check_max_order_value(intent, reference_price))
            checks.append(self._check_max_daily_loss(daily_pnl))
            checks.append(self._check_max_strategy_loss(strategy_pnl))
            checks.append(self._check_max_orders_per_day())
        except Exception as exc:  # fail closed -- never let evaluation itself become a silent approval
            checks.append(CanaryCheckOutcome("INTERNAL_ERROR", False, str(exc)))

        failed = [c for c in checks if not c.passed]
        if failed:
            reason = "; ".join(f"{c.name}: {c.reason}" for c in failed)
            if self._alerts is not None:
                for c in failed:
                    self._alerts.risk_breach(intent.strategy_id, f"CANARY_{c.name}", c.reason, correlation_id=intent.correlation_id)
            return CanaryAuthorizationResult.deny(reason, checks)

        # Only consumes the daily order budget once every check has passed
        # -- a rejected intent must never count against the day's cap.
        self._increment_order_count()
        return CanaryAuthorizationResult.allow(checks)

    # -- 1: dedicated trading account -------------------------------------------- #
    def _check_dedicated_account(self, intent: OrderIntent) -> CanaryCheckOutcome:
        if intent.account_id != self._limits.account_id:
            return CanaryCheckOutcome(
                "DEDICATED_ACCOUNT", False,
                f"intent targets account '{intent.account_id}'; LIVE_CANARY is restricted to '{self._limits.account_id}'",
            )
        return CanaryCheckOutcome("DEDICATED_ACCOUNT", True)

    # -- 2: maximum quantity ------------------------------------------------------- #
    def _check_max_quantity(self, intent: OrderIntent) -> CanaryCheckOutcome:
        if intent.quantity > self._limits.max_order_quantity:
            return CanaryCheckOutcome(
                "MAX_ORDER_QUANTITY", False,
                f"quantity {intent.quantity} exceeds canary limit {self._limits.max_order_quantity}",
            )
        return CanaryCheckOutcome("MAX_ORDER_QUANTITY", True)

    # -- 3: maximum order value ------------------------------------------------------ #
    def _check_max_order_value(self, intent: OrderIntent, reference_price: float | None) -> CanaryCheckOutcome:
        price = intent.limit_price if intent.limit_price is not None else reference_price
        if price is None:
            return CanaryCheckOutcome("MAX_ORDER_VALUE", False, "no price available to compute order value")
        value = abs(intent.quantity * price)
        if value > self._limits.max_order_value:
            return CanaryCheckOutcome(
                "MAX_ORDER_VALUE", False,
                f"order value {value:.2f} exceeds canary limit {self._limits.max_order_value:.2f}",
            )
        return CanaryCheckOutcome("MAX_ORDER_VALUE", True)

    # -- 4: maximum daily loss ------------------------------------------------------ #
    def _check_max_daily_loss(self, daily_pnl: float) -> CanaryCheckOutcome:
        if daily_pnl <= -abs(self._limits.max_daily_loss):
            return CanaryCheckOutcome(
                "MAX_DAILY_LOSS", False,
                f"daily P&L {daily_pnl:.2f} has breached canary limit {self._limits.max_daily_loss:.2f}",
            )
        return CanaryCheckOutcome("MAX_DAILY_LOSS", True)

    # -- 5: maximum strategy loss ------------------------------------------------------- #
    def _check_max_strategy_loss(self, strategy_pnl: float) -> CanaryCheckOutcome:
        if strategy_pnl <= -abs(self._limits.max_strategy_loss):
            return CanaryCheckOutcome(
                "MAX_STRATEGY_LOSS", False,
                f"strategy P&L {strategy_pnl:.2f} has breached canary limit {self._limits.max_strategy_loss:.2f}",
            )
        return CanaryCheckOutcome("MAX_STRATEGY_LOSS", True)

    # -- 6: maximum number of orders (per calendar day) ------------------------------------ #
    def _check_max_orders_per_day(self) -> CanaryCheckOutcome:
        count = self._orders_today.get(date.today(), 0)
        if count >= self._limits.max_orders_per_day:
            return CanaryCheckOutcome(
                "MAX_ORDERS_PER_DAY", False,
                f"{count} orders already authorized today; canary limit is {self._limits.max_orders_per_day}",
            )
        return CanaryCheckOutcome("MAX_ORDERS_PER_DAY", True)

    def _increment_order_count(self) -> None:
        today = date.today()
        with self._lock:
            self._orders_today[today] = self._orders_today.get(today, 0) + 1

    # -- 7: kill switch (read side) ------------------------------------------------------------ #
    def _check_kill_switch(self) -> CanaryCheckOutcome:
        if self._kill_switch_engaged:
            return CanaryCheckOutcome("KILL_SWITCH", False, "LIVE_CANARY kill switch is engaged")
        return CanaryCheckOutcome("KILL_SWITCH", True)

    def _check_shutdown(self) -> CanaryCheckOutcome:
        if self._shutdown:
            return CanaryCheckOutcome("EMERGENCY_SHUTDOWN", False, self._shutdown_reason or "emergency shutdown engaged")
        return CanaryCheckOutcome("EMERGENCY_SHUTDOWN", True)

    # -- 9: idempotency (required, not merely tracked) ------------------------------------------- #
    def _check_idempotency_present(self, intent: OrderIntent) -> CanaryCheckOutcome:
        if not intent.idempotency_key:
            return CanaryCheckOutcome(
                "IDEMPOTENCY_REQUIRED", False,
                "LIVE_CANARY requires every intent to carry a non-empty idempotency_key",
            )
        return CanaryCheckOutcome("IDEMPOTENCY_REQUIRED", True)

    # -- 8: duplicate-order protection ------------------------------------------------------------- #
    def _check_duplicate(self, intent: OrderIntent) -> CanaryCheckOutcome:
        key = intent.idempotency_key
        if not key:
            return CanaryCheckOutcome("DUPLICATE_ORDER_PROTECTION", True, "skipped: no idempotency_key (already flagged by IDEMPOTENCY_REQUIRED)")
        with self._lock:
            if key in self._seen_idempotency_keys:
                return CanaryCheckOutcome("DUPLICATE_ORDER_PROTECTION", False, f"idempotency_key {key!r} was already authorized")
            self._seen_idempotency_keys.add(key)
        return CanaryCheckOutcome("DUPLICATE_ORDER_PROTECTION", True)

    # -- 10: broker response validation (post-call) ------------------------------------------------------ #
    def validate_broker_response(self, result: OrderResult | None) -> CanaryCheckOutcome:
        """Sanity-checks a broker's OrderResult before StrategyExecutionEngine
        trusts it as a legitimate outcome for a LIVE_CANARY order. Called
        AFTER the broker adapter has already responded -- this can only
        catch a malformed/suspicious response, never prevent the call
        itself (that's what authorize() is for).

        Phase 14.6: delegates to the shared
        trading.common.broker_response_validation module, which
        StrategyExecutionEngine.execute() now also calls unconditionally
        for EVERY execution mode (not just LIVE_CANARY) -- see that
        module's docstring for why the logic used to live only here."""
        outcome = validate_broker_response(result)
        return CanaryCheckOutcome("BROKER_RESPONSE_VALIDATION", outcome.valid, outcome.reason)

    # -- 11: position reconciliation (post-call) --------------------------------------------------------- #
    def reconcile_position(
        self, strategy_id: str, symbol: str, broker_reported_quantity: int, expected_quantity: int,
    ) -> CanaryCheckOutcome:
        """Compares what the broker reports holding against what this
        process expected to hold. A mismatch raises the Phase 13
        unexpected_position alert -- this method does not itself block or
        reverse anything (there is nothing left to block once a fill has
        already happened); it exists to surface the discrepancy loudly and
        immediately so a human can decide whether to emergency_shutdown()."""
        if broker_reported_quantity != expected_quantity:
            if self._alerts is not None:
                self._alerts.unexpected_position(strategy_id, symbol, broker_reported_quantity, expected=expected_quantity)
            return CanaryCheckOutcome(
                "POSITION_RECONCILIATION", False,
                f"broker-reported position {broker_reported_quantity} != expected {expected_quantity} for {symbol}",
            )
        return CanaryCheckOutcome("POSITION_RECONCILIATION", True)
