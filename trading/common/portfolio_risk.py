"""
PortfolioRiskManager -- Phase 16.10: a CENTRAL, additive risk layer sitting
between WorkerCoordinator (Phase 16.9) and the existing, UNCHANGED
RiskManager (Phase 6+):

    Worker OrderIntent
          |
          v
    WorkerCoordinator's own 10 existing checks (Phase 16.9, unchanged)
          |
          v
    PortfolioRiskManager.evaluate_and_reserve()   <-- THIS MODULE
          |
          v
    StrategyRuntime.execute_worker_intent()
          -> kill switch -> idempotency replay -> authorization -> existing
             RiskManager.validate() -> mode gate -> broker resolution ->
             LiveAuthorization -> idempotency claim -> broker call
             (ALL UNCHANGED -- see trading/common/risk_manager.py and
             trading/common/execution.py, neither modified by this phase)

WHY THIS SITS BEFORE THE EXISTING RiskManager, NOT INSIDE IT:
  RiskManager (Phase 6) is strictly single-intent, single-account-limits
  scoped -- it has no notion of "all the strategies sharing this account"
  or "every account in the portfolio" at all (confirmed by reading its
  RiskContext: every exposure/P&L field is a caller-supplied point-in-time
  fact for ONE strategy/account, never aggregated). Rewriting it to grow
  that aggregation would touch a 486-line, already-audited, 15-check
  fail-closed gate that every existing caller (ExecutionEngine, demos,
  Phase 6-15D tests) depends on staying byte-for-byte the same. Adding a
  new, separate, additive gate in front of it is both safer (existing
  RiskManager tests remain valid unmodified) and clearer (a portfolio-wide
  rejection and a single-intent rejection are visibly different decisions
  with different reason codes, never conflated).

WHAT THIS MODULE DOES NOT DO:
  - It never imports a broker adapter/SDK and never calls a broker.
  - It never grants LiveAuthorization, never touches AccountAuthorizationState,
    and never bypasses the kill switch -- "portfolio risk allowed" means
    only "risk acceptable", never "authorized to trade live" (see the
    module-level PortfolioRiskDecision docstring below).
  - It never weakens, replaces, or duplicates a check the existing
    RiskManager already performs -- MAX_ORDER_QUANTITY, MAX_POSITION_QUANTITY,
    DUPLICATE_ORDER_PROTECTION, MARKET_SESSION_VALIDATION, KILL_SWITCH,
    STRATEGY_ENABLED, ACCOUNT_ENABLED, EXECUTION_MODE_ALLOWED,
    INSTRUMENT_VALIDATION remain exclusively RiskManager's job.

EXPOSURE MODEL (see PortfolioRiskLimits/PortfolioRiskSnapshot docstrings):
  Exposure here is NOTIONAL/QUANTITY based (quantity * price, gross,
  additive across positions), NOT delta-adjusted portfolio risk. No Greeks
  (delta/gamma/theta/vega) exist anywhere in this repository's normalized
  market-data model (trading/market_data/) as of this phase -- see
  docs/phase-16-10-central-portfolio-risk-report.md Section 5 for the
  repository evidence. Inventing a delta-adjusted number here would be
  fabricating data this repository cannot currently back.

WHY A PRIVATE LEDGER, NOT ShadowBroker.get_positions():
  ShadowBroker (Phase 16.5) already tracks a real per-symbol position/P&L
  book, but it is scoped to ONE broker instance (one account) and has no
  strategy_id at all (BrokerClient.place_order()'s ABC signature carries
  none) -- it cannot answer "what is CombinedVWAP's own exposure" when
  DoubleStraddle shares the same account. This module's ledger is keyed by
  (strategy_id, account_id, symbol) so strategy/account/portfolio numbers
  are always summed from ONE internally-consistent source, using the exact
  same average-cost/realize-on-close arithmetic ShadowBroker._update_position
  already uses (see _apply_fill_to_position below) -- not a competing
  concept, an extension of the same one to a dimension ShadowBroker cannot
  see. It does not replace ShadowBroker's own bookkeeping, which continues
  unchanged.

KNOWN, DOCUMENTED LIMITATIONS (see the Phase 16.10 report for the full list):
  - ExecutionResult.average_price is not currently populated anywhere in
    the execution pipeline (trading/common/execution.py's own
    from_order_result() hardcodes average_price=None -- OrderResult carries
    no fill price today, a pre-existing gap, not introduced by this phase).
    commit_reservation() therefore books the RESERVATION's own price (the
    same intent.limit_price/reference_price already used, and already
    risk-checked, at evaluate_and_reserve() time) rather than fabricating
    or mis-reading a fill price that does not exist yet.
  - Unrealized (mark-to-market) P&L is UNSUPPORTED: no independent live
    market mark is reliably attached to every worker submission. Only
    REALIZED P&L (booked when a position is actually reduced/closed by an
    opposite-side fill, using the ledger's own average-cost basis) backs
    the daily-loss checks below -- reporting 0.0 for "no closes yet" is
    correct (nothing has realized), not a fabricated "no risk" value.
  - Concentration is grouped by exact instrument SYMBOL, not by a shared
    "underlying" (e.g. every NIFTY option strike together): OrderIntent.
    instrument (the only field carrying `underlying`) is optional and not
    populated by any of the three integrated strategies today.
  - "Open order" tracking is best-effort: an order whose ExecutionResult
    status is not terminal (COMPLETE/FILLED) is counted as open and stays
    open until commit_reservation()/release_reservation() is called again
    for it or resolve_open_order() is called explicitly -- this module does
    not poll broker state to auto-resolve a still-open shadow order.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum

from trading.common.broker import OrderSide
from trading.common.execution import TERMINAL_STATUSES, ExecutionResult
from trading.common.order_intent import OrderIntent

__all__ = [
    "ConflictInfo",
    "ConflictType",
    "PortfolioRiskDecision",
    "PortfolioRiskLimits",
    "PortfolioRiskManager",
    "PortfolioRiskSnapshot",
    "REASON_ACCOUNT_EXPOSURE_LIMIT",
    "REASON_ACCOUNT_LOSS_LIMIT",
    "REASON_ALLOWED",
    "REASON_INVALID_RISK_STATE",
    "REASON_OPEN_ORDER_LIMIT",
    "REASON_ORDER_COUNT_LIMIT",
    "REASON_PORTFOLIO_EXPOSURE_LIMIT",
    "REASON_PORTFOLIO_LOSS_LIMIT",
    "REASON_RISK_DATA_UNAVAILABLE",
    "REASON_STRATEGY_EXPOSURE_LIMIT",
    "REASON_STRATEGY_LOSS_LIMIT",
]

REASON_ALLOWED = "ALLOWED"
REASON_STRATEGY_LOSS_LIMIT = "STRATEGY_LOSS_LIMIT"
REASON_ACCOUNT_LOSS_LIMIT = "ACCOUNT_LOSS_LIMIT"
REASON_PORTFOLIO_LOSS_LIMIT = "PORTFOLIO_LOSS_LIMIT"
REASON_STRATEGY_EXPOSURE_LIMIT = "STRATEGY_EXPOSURE_LIMIT"
REASON_ACCOUNT_EXPOSURE_LIMIT = "ACCOUNT_EXPOSURE_LIMIT"
REASON_PORTFOLIO_EXPOSURE_LIMIT = "PORTFOLIO_EXPOSURE_LIMIT"
REASON_ORDER_COUNT_LIMIT = "ORDER_COUNT_LIMIT"
REASON_OPEN_ORDER_LIMIT = "OPEN_ORDER_LIMIT"
REASON_INVALID_RISK_STATE = "INVALID_RISK_STATE"
REASON_RISK_DATA_UNAVAILABLE = "RISK_DATA_UNAVAILABLE"

_PORTFOLIO_SCOPE = "*"


class ConflictType(str, Enum):
    NONE = "NONE"
    DUPLICATE = "DUPLICATE"
    OPPOSING = "OPPOSING"
    CONCENTRATION = "CONCENTRATION"
    SELF_OFFSETTING = "SELF_OFFSETTING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ConflictInfo:
    """Factual metadata only -- see the module docstring: no conflict type
    here causes rejection by default. A caller may opt a specific type into
    `PortfolioRiskManager(hard_block_conflicts=...)` if a real business
    rule justifies it; Phase 16.10 configures none."""

    conflict_type: ConflictType
    symbol: str
    counter_strategy_id: str = ""
    note: str = ""


@dataclass(frozen=True)
class PortfolioRiskLimits:
    """Every field defaults to None = "not configured, not enforced" --
    the exact same convention trading.common.risk_manager.RiskLimits
    already uses, for the same reason (see that module's own docstring):
    a hard 0 default would reject every order the moment this file is
    wired in, which is a worse failure mode than "not enforced until
    someone configures it". No production value is invented here -- see
    docs/phase-16-10-central-portfolio-risk-report.md Section 4."""

    max_strategy_daily_loss: float | None = None
    max_account_daily_loss: float | None = None
    max_portfolio_daily_loss: float | None = None

    max_strategy_exposure: float | None = None
    max_account_exposure: float | None = None
    max_portfolio_exposure: float | None = None

    max_strategy_open_orders: int | None = None
    max_account_open_orders: int | None = None
    max_portfolio_open_orders: int | None = None

    max_strategy_orders_per_day: int | None = None
    max_account_orders_per_day: int | None = None
    max_portfolio_orders_per_day: int | None = None


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    """An IMMUTABLE point-in-time read -- building one never mutates any
    state (see get_snapshot(), a pure read used by the read-only API
    routes; Phase 15D.6 in trading/common/risk_manager.py hit exactly this
    class of bug when a "preview" call silently consumed idempotency/order-
    count budget -- see that module's dry_run parameter and this module's
    own docstring). Every *_exposure/*_daily_pnl field already includes any
    currently-outstanding reservations, not just committed fills."""

    timestamp: str

    strategy_id: str
    strategy_daily_pnl: float
    strategy_exposure: float
    strategy_open_orders: int
    strategy_orders_today: int

    account_id: str
    account_daily_pnl: float
    account_exposure: float
    account_open_orders: int
    account_orders_today: int

    portfolio_daily_pnl: float
    portfolio_exposure: float
    portfolio_open_orders: int
    portfolio_orders_today: int

    proposed_order_notional: float | None = None
    projected_strategy_exposure: float | None = None
    projected_account_exposure: float | None = None
    projected_portfolio_exposure: float | None = None


@dataclass(frozen=True)
class PortfolioRiskDecision:
    """`allowed=True` means RISK ACCEPTABLE ONLY -- never "authorized to
    trade live". AccountAuthorizationState/LiveAuthorization remain fully
    independent gates inside StrategyExecutionEngine.execute(), evaluated
    (or not) after this decision, exactly as before this phase."""

    allowed: bool
    reason_code: str
    message: str = ""
    snapshot: PortfolioRiskSnapshot | None = None
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    conflicts: tuple[ConflictInfo, ...] = ()
    reservation_id: str | None = None


@dataclass
class _LedgerPosition:
    quantity: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class _Outstanding:
    """A reservation (not yet committed) OR a committed-but-still-OPEN
    order (see commit_reservation()) -- either way, its notional/side must
    keep counting toward exposure/open-order numbers until it is released
    or resolved."""

    reservation_id: str
    strategy_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    notional: float
    order_count_day: date
    idempotency_key: str = ""


def _apply_fill_to_position(existing: _LedgerPosition | None, side: OrderSide, fill_qty: int, fill_price: float) -> _LedgerPosition:
    """The SAME average-cost / realize-on-close algorithm as
    trading.common.brokers.shadow_broker.ShadowBroker._update_position,
    applied here per (strategy_id, account_id, symbol) instead of per
    (broker_instance, symbol) -- see the module docstring for why this is
    an extension of that existing, already-tested arithmetic, not a
    competing reimplementation invented from scratch."""
    signed_qty = fill_qty if side == OrderSide.BUY else -fill_qty
    if existing is None or existing.quantity == 0:
        realized = existing.realized_pnl if existing else 0.0
        return _LedgerPosition(quantity=signed_qty, avg_price=fill_price, realized_pnl=realized)

    same_direction = (existing.quantity > 0) == (signed_qty > 0)
    new_qty = existing.quantity + signed_qty

    if same_direction:
        total_qty = abs(existing.quantity) + abs(signed_qty)
        avg_price = ((existing.avg_price * abs(existing.quantity)) + (fill_price * abs(signed_qty))) / total_qty
        realized = existing.realized_pnl
    else:
        closing_qty = min(abs(signed_qty), abs(existing.quantity))
        direction = 1 if existing.quantity > 0 else -1
        realized = existing.realized_pnl + direction * (fill_price - existing.avg_price) * closing_qty
        if new_qty == 0:
            avg_price = 0.0
        elif (new_qty > 0) == (existing.quantity > 0):
            avg_price = existing.avg_price  # partial close, same direction remains
        else:
            avg_price = fill_price  # flipped direction: closed fully + opened the other way

    return _LedgerPosition(quantity=new_qty, avg_price=avg_price, realized_pnl=realized)


class PortfolioRiskManager:
    def __init__(
        self,
        *,
        portfolio_limits: PortfolioRiskLimits | None = None,
        strategy_limits: dict[str, PortfolioRiskLimits] | None = None,
        account_limits: dict[str, PortfolioRiskLimits] | None = None,
        concentration_warn_ratio: float = 0.75,
        hard_block_conflicts: frozenset[ConflictType] = frozenset(),
        clock=None,
    ) -> None:
        self._portfolio_limits = portfolio_limits or PortfolioRiskLimits()
        self._strategy_limits: dict[str, PortfolioRiskLimits] = dict(strategy_limits or {})
        self._account_limits: dict[str, PortfolioRiskLimits] = dict(account_limits or {})
        self._concentration_warn_ratio = concentration_warn_ratio
        self._hard_block_conflicts = hard_block_conflicts
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._lock = threading.RLock()
        self._ledger: dict[tuple[str, str, str], _LedgerPosition] = {}
        self._reservations: dict[str, _Outstanding] = {}
        self._open_orders: dict[str, _Outstanding] = {}
        self._order_counts: dict[tuple[str, str, date], int] = {}

    # -- limit configuration (owned centrally -- see module docstring) ------ #
    def set_strategy_limits(self, strategy_id: str, limits: PortfolioRiskLimits) -> None:
        with self._lock:
            self._strategy_limits[strategy_id] = limits

    def set_account_limits(self, account_id: str, limits: PortfolioRiskLimits) -> None:
        with self._lock:
            self._account_limits[account_id] = limits

    def set_portfolio_limits(self, limits: PortfolioRiskLimits) -> None:
        with self._lock:
            self._portfolio_limits = limits

    def get_strategy_limits(self, strategy_id: str) -> PortfolioRiskLimits:
        return self._strategy_limits.get(strategy_id, PortfolioRiskLimits())

    def get_account_limits(self, account_id: str) -> PortfolioRiskLimits:
        return self._account_limits.get(account_id, PortfolioRiskLimits())

    def get_portfolio_limits(self) -> PortfolioRiskLimits:
        return self._portfolio_limits

    # -- pure, side-effect-free read (Phase 15D.6 discipline) ---------------- #
    def get_snapshot(self, *, strategy_id: str = "", account_id: str = "") -> PortfolioRiskSnapshot:
        with self._lock:
            return self._build_snapshot(strategy_id, account_id)

    # -- the central gate ------------------------------------------------------ #
    def evaluate_and_reserve(
        self,
        intent: OrderIntent,
        *,
        strategy_id: str,
        account_id: str,
        reference_price: float | None = None,
    ) -> PortfolioRiskDecision:
        try:
            with self._lock:
                return self._evaluate_and_reserve_locked(intent, strategy_id, account_id, reference_price)
        except Exception as exc:  # fail closed -- never a silent approval or an unhandled crash
            return PortfolioRiskDecision(
                allowed=False, reason_code=REASON_INVALID_RISK_STATE,
                message=f"portfolio risk evaluation failed: {exc}",
            )

    def _evaluate_and_reserve_locked(
        self, intent: OrderIntent, strategy_id: str, account_id: str, reference_price: float | None,
    ) -> PortfolioRiskDecision:
        snapshot = self._build_snapshot(strategy_id, account_id)

        price = intent.limit_price if intent.limit_price is not None else reference_price
        order_notional = None if price is None else abs(intent.quantity * price)

        strategy_limits = self.get_strategy_limits(strategy_id)
        account_limits = self.get_account_limits(account_id)
        portfolio_limits = self._portfolio_limits

        exposure_limit_configured = any((
            strategy_limits.max_strategy_exposure is not None,
            account_limits.max_account_exposure is not None,
            portfolio_limits.max_portfolio_exposure is not None,
        ))
        if order_notional is None and exposure_limit_configured:
            return PortfolioRiskDecision(
                allowed=False, reason_code=REASON_RISK_DATA_UNAVAILABLE,
                message="an exposure limit is configured but no price is available (intent.limit_price and reference_price are both None) to compute this intent's notional value",
                snapshot=snapshot,
                violations=(REASON_RISK_DATA_UNAVAILABLE,),
            )

        conflicts = self._classify_conflicts(intent, strategy_id, order_notional)

        violations: list[str] = []

        if strategy_limits.max_strategy_daily_loss is not None and snapshot.strategy_daily_pnl <= -abs(strategy_limits.max_strategy_daily_loss):
            violations.append(REASON_STRATEGY_LOSS_LIMIT)
        if account_limits.max_account_daily_loss is not None and snapshot.account_daily_pnl <= -abs(account_limits.max_account_daily_loss):
            violations.append(REASON_ACCOUNT_LOSS_LIMIT)
        if portfolio_limits.max_portfolio_daily_loss is not None and snapshot.portfolio_daily_pnl <= -abs(portfolio_limits.max_portfolio_daily_loss):
            violations.append(REASON_PORTFOLIO_LOSS_LIMIT)

        projected_strategy_exposure = snapshot.strategy_exposure + (order_notional or 0.0)
        projected_account_exposure = snapshot.account_exposure + (order_notional or 0.0)
        projected_portfolio_exposure = snapshot.portfolio_exposure + (order_notional or 0.0)

        if strategy_limits.max_strategy_exposure is not None and projected_strategy_exposure > strategy_limits.max_strategy_exposure:
            violations.append(REASON_STRATEGY_EXPOSURE_LIMIT)
        if account_limits.max_account_exposure is not None and projected_account_exposure > account_limits.max_account_exposure:
            violations.append(REASON_ACCOUNT_EXPOSURE_LIMIT)
        if portfolio_limits.max_portfolio_exposure is not None and projected_portfolio_exposure > portfolio_limits.max_portfolio_exposure:
            violations.append(REASON_PORTFOLIO_EXPOSURE_LIMIT)

        if strategy_limits.max_strategy_orders_per_day is not None and snapshot.strategy_orders_today >= strategy_limits.max_strategy_orders_per_day:
            violations.append(REASON_ORDER_COUNT_LIMIT)
        if account_limits.max_account_orders_per_day is not None and snapshot.account_orders_today >= account_limits.max_account_orders_per_day:
            violations.append(REASON_ORDER_COUNT_LIMIT)
        if portfolio_limits.max_portfolio_orders_per_day is not None and snapshot.portfolio_orders_today >= portfolio_limits.max_portfolio_orders_per_day:
            violations.append(REASON_ORDER_COUNT_LIMIT)

        if strategy_limits.max_strategy_open_orders is not None and snapshot.strategy_open_orders >= strategy_limits.max_strategy_open_orders:
            violations.append(REASON_OPEN_ORDER_LIMIT)
        if account_limits.max_account_open_orders is not None and snapshot.account_open_orders >= account_limits.max_account_open_orders:
            violations.append(REASON_OPEN_ORDER_LIMIT)
        if portfolio_limits.max_portfolio_open_orders is not None and snapshot.portfolio_open_orders >= portfolio_limits.max_portfolio_open_orders:
            violations.append(REASON_OPEN_ORDER_LIMIT)

        if any(c.conflict_type in self._hard_block_conflicts for c in conflicts):
            violations.append(f"CONFLICT_BLOCKED: {', '.join(sorted({c.conflict_type.value for c in conflicts if c.conflict_type in self._hard_block_conflicts}))}")

        snapshot_with_projection = replace(
            snapshot,
            proposed_order_notional=order_notional,
            projected_strategy_exposure=projected_strategy_exposure,
            projected_account_exposure=projected_account_exposure,
            projected_portfolio_exposure=projected_portfolio_exposure,
        )

        if violations:
            return PortfolioRiskDecision(
                allowed=False, reason_code=violations[0],
                message=f"portfolio risk rejected: {'; '.join(violations)}",
                snapshot=snapshot_with_projection, violations=tuple(violations), conflicts=conflicts,
            )

        reservation_id = str(uuid.uuid4())
        today = self._clock().date()
        outstanding = _Outstanding(
            reservation_id=reservation_id, strategy_id=strategy_id, account_id=account_id,
            symbol=intent.symbol, side=intent.side, quantity=intent.quantity,
            price=price if price is not None else 0.0, notional=order_notional or 0.0,
            order_count_day=today, idempotency_key=intent.idempotency_key or "",
        )
        self._reservations[reservation_id] = outstanding
        self._order_counts[("strategy", strategy_id, today)] = snapshot.strategy_orders_today + 1
        self._order_counts[("account", account_id, today)] = snapshot.account_orders_today + 1
        self._order_counts[(_PORTFOLIO_SCOPE, _PORTFOLIO_SCOPE, today)] = snapshot.portfolio_orders_today + 1

        return PortfolioRiskDecision(
            allowed=True, reason_code=REASON_ALLOWED, snapshot=snapshot_with_projection,
            conflicts=conflicts, reservation_id=reservation_id,
        )

    # -- resolving a reservation ---------------------------------------------- #
    def commit_reservation(self, reservation_id: str, *, execution_result: ExecutionResult | None) -> None:
        """Converts a reservation into either a permanent ledger fill
        (terminal/COMPLETE) or a still-open order (see module docstring),
        or discards it (rejected/failed) -- called exactly once per
        reservation by the coordinator, after execute_worker_intent()
        returns. A missing/unknown reservation_id is a silent no-op (it may
        have already been resolved, or never existed for this manager
        instance) -- never raises into the caller's execution path."""
        with self._lock:
            outstanding = self._reservations.pop(reservation_id, None)
            if outstanding is None:
                return
            if execution_result is not None and execution_result.status == "AMBIGUOUS":
                # Phase 17.1 Section 27 fix: the broker outcome is UNKNOWN --
                # it may have actually accepted/filled this order. Releasing
                # the reservation here (as every other failure path does)
                # would silently understate real exposure/order-count if the
                # order in fact went through. Keep the exposure/order-count
                # footprint exactly like a still-open order, until an
                # operator/reconciliation flow explicitly resolves it via
                # resolve_open_order() -- never auto-released on a timer or
                # on the next unrelated call.
                self._open_orders[reservation_id] = outstanding
                return
            if execution_result is None or not execution_result.success:
                self._rollback_order_count(outstanding)
                return
            if execution_result.status in TERMINAL_STATUSES:
                key = (outstanding.strategy_id, outstanding.account_id, outstanding.symbol)
                self._ledger[key] = _apply_fill_to_position(
                    self._ledger.get(key), outstanding.side, outstanding.quantity, outstanding.price,
                )
            else:
                # OPEN / partially filled -- keep its exposure/open-order
                # footprint until resolve_open_order() is called; not
                # auto-polled in this phase (see module docstring).
                self._open_orders[reservation_id] = outstanding

    def release_reservation(self, reservation_id: str) -> None:
        """A rejected/failed downstream execution must give back both the
        exposure it reserved AND the order-count budget it consumed --
        mirroring RiskManager's own '_record_order_approved_for_daily_count
        only called once every check has passed' discipline."""
        with self._lock:
            outstanding = self._reservations.pop(reservation_id, None)
            if outstanding is None:
                return
            self._rollback_order_count(outstanding)

    def resolve_open_order(self, reservation_id: str) -> None:
        """Explicit, manual resolution of a still-OPEN order recorded by
        commit_reservation() -- e.g. an operator confirms it was later
        filled or cancelled. Not wired to any automatic broker polling in
        this phase (see module docstring)."""
        with self._lock:
            self._open_orders.pop(reservation_id, None)

    # -- Phase 17.1-R Remediation E: reconciliation feedback ------------------ #
    def find_reservation_by_idempotency_key(self, idempotency_key: str) -> str | None:
        """Read-only lookup used by ReconciliationService to translate a
        resolved idempotency_key back into the reservation_id it must
        commit/release -- searches both still-pending reservations and
        already-open (including ambiguous-held) entries. Returns None if no
        reservation was ever made for this key (e.g. portfolio risk was
        disabled, or the key was a replay that skipped reservation
        entirely) -- a caller must treat that as "nothing to resolve here",
        never as an error."""
        if not idempotency_key:
            return None
        with self._lock:
            for outstanding in (*self._reservations.values(), *self._open_orders.values()):
                if outstanding.idempotency_key == idempotency_key:
                    return outstanding.reservation_id
        return None

    def resolve_reconciliation(self, reservation_id: str, *, found: bool, fill_price: float | None = None) -> bool:
        """Resolves a reservation that Section 27's AMBIGUOUS-outcome fix
        left held open (in self._open_orders, never auto-released) once
        reconciliation has produced a definitive answer against the real
        broker:

          found=True  (reconciliation FOUND the order at the broker) ->
              COMMIT: apply the fill to the ledger exactly like a normal
              terminal commit_reservation() would have, using the actual
              reconciled fill_price when known, else the original
              reservation price.
          found=False (reconciliation proves the order does NOT exist) ->
              RELEASE: give back the exposure/order-count budget, exactly
              like release_reservation() -- consistent with this phase's
              explicit fail-closed NOT_FOUND policy (Section 23): this
              method never places a new order and never implies one is
              authorized; it only stops reserving budget for one that
              provably never happened.

        Looks in self._open_orders ONLY (never self._reservations) --
        an AMBIGUOUS outcome is always filed there by commit_reservation(),
        never left in self._reservations. Returns False (a no-op) if no
        such open reservation exists -- already resolved by a concurrent
        caller, or never held open in the first place; never raises,
        mirroring every other resolution method in this class."""
        with self._lock:
            outstanding = self._open_orders.pop(reservation_id, None)
            if outstanding is None:
                return False
            if found:
                price = fill_price if fill_price is not None else outstanding.price
                key = (outstanding.strategy_id, outstanding.account_id, outstanding.symbol)
                self._ledger[key] = _apply_fill_to_position(
                    self._ledger.get(key), outstanding.side, outstanding.quantity, price,
                )
            else:
                self._rollback_order_count(outstanding)
            return True

    def _rollback_order_count(self, outstanding: _Outstanding) -> None:
        day = outstanding.order_count_day
        for scope_id, key_id in (
            ("strategy", outstanding.strategy_id), ("account", outstanding.account_id),
            (_PORTFOLIO_SCOPE, _PORTFOLIO_SCOPE),
        ):
            key = (scope_id, key_id, day)
            if self._order_counts.get(key, 0) > 0:
                self._order_counts[key] -= 1

    # -- snapshot assembly ------------------------------------------------------ #
    def _outstanding_all(self) -> list[_Outstanding]:
        return list(self._reservations.values()) + list(self._open_orders.values())

    def _build_snapshot(self, strategy_id: str, account_id: str) -> PortfolioRiskSnapshot:
        today = self._clock().date()
        outstanding = self._outstanding_all()

        def exposure_for(predicate) -> float:
            total = sum(abs(pos.quantity * pos.avg_price) for key, pos in self._ledger.items() if predicate(key))
            total += sum(o.notional for o in outstanding if predicate((o.strategy_id, o.account_id, o.symbol)))
            return total

        def pnl_for(predicate) -> float:
            return sum(pos.realized_pnl for key, pos in self._ledger.items() if predicate(key))

        def open_orders_for(match_strategy: str | None, match_account: str | None) -> int:
            return sum(
                1 for o in outstanding
                if (match_strategy is None or o.strategy_id == match_strategy)
                and (match_account is None or o.account_id == match_account)
            )

        strategy_exposure = exposure_for(lambda key: key[0] == strategy_id) if strategy_id else 0.0
        account_exposure = exposure_for(lambda key: key[1] == account_id) if account_id else 0.0
        portfolio_exposure = exposure_for(lambda key: True)

        strategy_pnl = pnl_for(lambda key: key[0] == strategy_id) if strategy_id else 0.0
        account_pnl = pnl_for(lambda key: key[1] == account_id) if account_id else 0.0
        portfolio_pnl = pnl_for(lambda key: True)

        return PortfolioRiskSnapshot(
            timestamp=self._clock().isoformat(),
            strategy_id=strategy_id,
            strategy_daily_pnl=strategy_pnl,
            strategy_exposure=strategy_exposure,
            strategy_open_orders=open_orders_for(strategy_id or None, None) if strategy_id else 0,
            strategy_orders_today=self._order_counts.get(("strategy", strategy_id, today), 0) if strategy_id else 0,
            account_id=account_id,
            account_daily_pnl=account_pnl,
            account_exposure=account_exposure,
            account_open_orders=open_orders_for(None, account_id or None) if account_id else 0,
            account_orders_today=self._order_counts.get(("account", account_id, today), 0) if account_id else 0,
            portfolio_daily_pnl=portfolio_pnl,
            portfolio_exposure=portfolio_exposure,
            portfolio_open_orders=len(outstanding),
            portfolio_orders_today=self._order_counts.get((_PORTFOLIO_SCOPE, _PORTFOLIO_SCOPE, today), 0),
        )

    # -- conflict classification (informational only -- see module docstring) - #
    def _classify_conflicts(self, intent: OrderIntent, strategy_id: str, order_notional: float | None) -> tuple[ConflictInfo, ...]:
        symbol = intent.symbol
        proposed_signed = intent.quantity if intent.side == OrderSide.BUY else -intent.quantity

        same_symbol: list[tuple[str, int]] = [
            (key[0], pos.quantity) for key, pos in self._ledger.items() if key[2] == symbol and pos.quantity != 0
        ]
        for o in self._outstanding_all():
            if o.symbol == symbol:
                signed = o.quantity if o.side == OrderSide.BUY else -o.quantity
                same_symbol.append((o.strategy_id, signed))

        conflicts: list[ConflictInfo] = []

        own_qty = sum(q for s, q in same_symbol if s == strategy_id)
        if own_qty != 0:
            if (own_qty > 0) == (proposed_signed > 0):
                conflicts.append(ConflictInfo(ConflictType.DUPLICATE, symbol, counter_strategy_id=strategy_id,
                                               note="this strategy already holds a same-direction position/order in this instrument"))
            else:
                conflicts.append(ConflictInfo(ConflictType.SELF_OFFSETTING, symbol, counter_strategy_id=strategy_id,
                                               note="this order would reduce/flatten the strategy's own existing position"))

        other_strategies = {s for s, q in same_symbol if s != strategy_id}
        for other in sorted(other_strategies):
            other_qty = sum(q for s, q in same_symbol if s == other)
            if other_qty == 0:
                continue
            if (other_qty > 0) != (proposed_signed > 0):
                conflicts.append(ConflictInfo(ConflictType.OPPOSING, symbol, counter_strategy_id=other,
                                               note=f"strategy {other!r} holds an opposite-direction position/order in this instrument -- gross exposure preserved, not netted; may be a legitimate hedge"))

        concentration = self._concentration_conflict(symbol, order_notional)
        if concentration is not None:
            conflicts.append(concentration)

        if not conflicts:
            conflicts.append(ConflictInfo(ConflictType.NONE, symbol))
        return tuple(conflicts)

    def _concentration_conflict(self, symbol: str, order_notional: float | None) -> ConflictInfo | None:
        if order_notional is None:
            return ConflictInfo(ConflictType.UNKNOWN, symbol, note="no price available to compute concentration")

        outstanding = self._outstanding_all()
        existing_symbol_total = sum(abs(pos.quantity * pos.avg_price) for key, pos in self._ledger.items() if key[2] == symbol)
        existing_symbol_total += sum(o.notional for o in outstanding if o.symbol == symbol)

        existing_portfolio_total = sum(abs(pos.quantity * pos.avg_price) for pos in self._ledger.values())
        existing_portfolio_total += sum(o.notional for o in outstanding)

        if existing_portfolio_total <= 0:
            # This would be the very first exposure in the whole portfolio
            # -- "100% concentrated in the only thing that exists" is a
            # trivial, meaningless flag, not a real concentration signal.
            return None

        symbol_total = existing_symbol_total + order_notional
        portfolio_total = existing_portfolio_total + order_notional
        ratio = symbol_total / portfolio_total
        if ratio >= self._concentration_warn_ratio:
            return ConflictInfo(ConflictType.CONCENTRATION, symbol, note=f"{symbol} would be {ratio:.0%} of portfolio notional exposure")
        return None
