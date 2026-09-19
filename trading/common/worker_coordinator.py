"""
WorkerCoordinator -- Phase 16.9: the CENTRAL, sole authority for turning
a worker-submitted OrderIntent into an executed (PAPER/SHADOW-only)
result.

    Worker                                  Central TCC (THIS MODULE)
    ------                                  --------------------------
    Market Data -> Strategy -> OrderIntent
                                      |
                                      v
                            OrderIntentSubmission
                                      |
                                      v
                      1. submission not a duplicate/stale replay
                      2. worker exists and is ONLINE
                      3. worker session matches (no duplicate session)
                      4. strategy exists
                      5. strategy is assigned to THIS worker (WorkerRegistry
                         is the authority -- a worker cannot claim
                         ownership of a strategy it was never assigned)
                      6. strategy's lifecycle permits evaluation (RUNNING/SHADOW)
                      7. the intent's own strategy_id agrees with the
                         claimed strategy (never trusted blindly)
                      8. a real account assignment exists for the strategy
                         (StrategyAssignment is the sole routing authority --
                         a worker-supplied account_id on the intent is NEVER
                         trusted for routing, exactly as
                         OrderIntent's own docstring already requires)
                      9. the intent carries an idempotency_key, a positive
                         quantity, and a non-empty instrument
                                      |
                                      v
                  PortfolioRiskManager.evaluate_and_reserve()  (Phase 16.10
                      -- ADDITIVE, sits in front of the existing RiskManager,
                      never replaces or weakens it; see
                      trading/common/portfolio_risk.py's own module
                      docstring. Skipped entirely when the intent's
                      idempotency_key already has an IdempotencyStore
                      record -- an idempotent retry must never reserve
                      portfolio exposure/order-count budget twice; it is
                      simply let through to replay exactly as execute()
                      would already handle it.)
                                      |
                                      v
                    StrategyRuntime.execute_worker_intent()
                        -> kill switch / authorization-state / RiskManager /
                           mode gate / broker resolution / idempotency /
                           hard shadow boundary / execute()  -- ALL UNCHANGED,
                           the SAME StrategyRuntime instance every local
                           (non-distributed) run_once() call already uses,
                           so there remains exactly ONE execution engine and
                           ONE hard shadow boundary in the whole system.
                                      |
                                      v
                                OrderIntentResult

A worker can never bypass any step above: it cannot self-authorize
(step 6 only checks lifecycle, never authorization -- that still lives
entirely inside execute()'s own untouched gate), cannot reassign its own
strategy to a different account (step 8 resolves the account centrally,
ignoring intent.account_id), and cannot reach a broker adapter directly
(this module never imports one).
"""
from __future__ import annotations

from datetime import datetime, timezone

from trading.common.portfolio_risk import PortfolioRiskManager
from trading.common.strategy_assignment import StrategyAssignment, UnknownAssignmentError
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError
from trading.common.strategy_runtime import ShadowBoundaryViolation, StrategyRuntime
from trading.common.worker_identity import WorkerStatus
from trading.common.worker_protocol import OrderIntentResult, OrderIntentSubmission
from trading.common.worker_registry import UnknownWorkerError, WorkerRegistry

__all__ = ["DEFAULT_MAX_SUBMISSION_AGE_SECONDS", "WorkerCoordinator"]

DEFAULT_MAX_SUBMISSION_AGE_SECONDS = 30.0


class WorkerCoordinator:
    def __init__(
        self,
        *,
        worker_registry: WorkerRegistry,
        strategy_registry: StrategyRegistry,
        strategy_assignment: StrategyAssignment,
        strategy_runtime: StrategyRuntime,
        portfolio_risk_manager: PortfolioRiskManager | None = None,
        max_submission_age_seconds: float = DEFAULT_MAX_SUBMISSION_AGE_SECONDS,
    ) -> None:
        self._workers = worker_registry
        self._strategies = strategy_registry
        self._assignments = strategy_assignment
        self._runtime = strategy_runtime
        # Phase 16.10 -- optional so every pre-existing caller/test
        # (constructed before this parameter existed) sees zero behavior
        # change: None disables the portfolio-risk gate entirely, falling
        # back to exactly the Phase 16.9 pipeline.
        self._portfolio_risk = portfolio_risk_manager
        self._max_submission_age = max_submission_age_seconds
        self._seen_submission_ids: set[str] = set()

    def submit_order_intent(self, submission: OrderIntentSubmission) -> OrderIntentResult:
        reason = self._validate(submission)
        if reason:
            return OrderIntentResult(
                submission_id=submission.submission_id, accepted=False, execution_result=None, reason=reason,
            )

        reservation_id = self._reserve_portfolio_risk(submission)
        if isinstance(reservation_id, OrderIntentResult):
            return reservation_id

        try:
            result = self._runtime.execute_worker_intent(submission.intent, owning_strategy_id=submission.strategy_id)
        except ShadowBoundaryViolation as exc:
            if reservation_id and self._portfolio_risk is not None:
                self._portfolio_risk.release_reservation(reservation_id)
            return OrderIntentResult(
                submission_id=submission.submission_id, accepted=False, execution_result=None, reason=str(exc),
            )
        except Exception as exc:
            # e.g. IdempotencyKeyReuseError -- execute() deliberately RAISES
            # (rather than returning a rejected ExecutionResult) for a
            # caller-side bug like reusing one idempotency_key for two
            # genuinely different intents (here: across two different
            # strategies/workers). Never let that escape this coordinator
            # uncaught -- fail closed, report it, same as every other
            # rejection path. Mirrors the identical fix already made in
            # trading/common/strategy_runtime.py's own run_once() loop.
            if reservation_id and self._portfolio_risk is not None:
                self._portfolio_risk.release_reservation(reservation_id)
            return OrderIntentResult(
                submission_id=submission.submission_id, accepted=False, execution_result=None, reason=str(exc),
            )

        if reservation_id and self._portfolio_risk is not None:
            self._portfolio_risk.commit_reservation(reservation_id, execution_result=result)

        return OrderIntentResult(
            submission_id=submission.submission_id, accepted=result.success, execution_result=result,
            reason="" if result.success else result.message,
        )

    def _reserve_portfolio_risk(self, submission: OrderIntentSubmission) -> str | None | OrderIntentResult:
        """Returns a reservation_id (str) to commit/release later, None if
        portfolio risk is disabled or was skipped (an idempotent replay --
        see module docstring), or an OrderIntentResult if portfolio risk
        rejects the submission outright."""
        if self._portfolio_risk is None:
            return None

        key = submission.intent.idempotency_key
        if key and self._runtime.idempotency_store.get(key) is not None:
            # Already has a record (COMPLETED/REJECTED/FAILED/AMBIGUOUS/
            # PENDING) -- this is a replay or a key-reuse case execute()
            # itself will handle exactly as it already does. Reserving
            # portfolio risk again here would double-count exposure/order
            # counts for the SAME underlying order (Phase 16.10 Section 23).
            return None

        try:
            account_id = self._assignments.get_account_id(submission.strategy_id)
        except UnknownAssignmentError:
            # Already reported by _validate()'s own check -- unreachable
            # in practice, kept for defense in depth.
            return OrderIntentResult(
                submission_id=submission.submission_id, accepted=False, execution_result=None,
                reason=f"no account assignment exists for strategy {submission.strategy_id!r}",
            )

        decision = self._portfolio_risk.evaluate_and_reserve(
            submission.intent, strategy_id=submission.strategy_id, account_id=account_id,
        )
        if not decision.allowed:
            return OrderIntentResult(
                submission_id=submission.submission_id, accepted=False, execution_result=None,
                reason=f"{decision.reason_code}: {decision.message}",
            )
        return decision.reservation_id

    def _validate(self, submission: OrderIntentSubmission) -> str:
        if submission.submission_id in self._seen_submission_ids:
            return "duplicate submission_id -- already processed"

        try:
            generated_at = datetime.fromisoformat(submission.generated_at)
        except (ValueError, TypeError):
            return "malformed generated_at timestamp"
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age > self._max_submission_age:
            return f"stale submission: generated_at is {age:.1f}s old (max {self._max_submission_age:.1f}s)"

        try:
            worker = self._workers.get_worker(submission.worker_id)
        except UnknownWorkerError:
            return f"unknown worker: {submission.worker_id!r}"
        if worker.status != WorkerStatus.ONLINE:
            return f"worker {submission.worker_id!r} is not ONLINE (status={worker.status.value})"
        if worker.session_id != submission.session_id:
            return "session_id does not match the worker's current active session -- stale or duplicate process"

        try:
            self._strategies.get(submission.strategy_id)
        except UnknownStrategyError:
            return f"unknown strategy: {submission.strategy_id!r}"

        owner = self._workers.get_strategy_owner(submission.strategy_id)
        if owner != submission.worker_id:
            return f"strategy {submission.strategy_id!r} is not assigned to worker {submission.worker_id!r} (owner={owner!r})"

        if not self._runtime.is_strategy_active(submission.strategy_id):
            return f"strategy {submission.strategy_id!r} is not in an active lifecycle state (RUNNING/SHADOW)"

        if submission.intent.strategy_id != submission.strategy_id:
            return "OrderIntent.strategy_id does not match the submitting strategy"

        try:
            self._assignments.get_account_id(submission.strategy_id)
        except UnknownAssignmentError:
            return f"no account assignment exists for strategy {submission.strategy_id!r}"

        if not submission.intent.idempotency_key:
            return "OrderIntent is missing an idempotency_key"
        if submission.intent.quantity <= 0:
            return "OrderIntent has a non-positive quantity"
        if not submission.intent.symbol:
            return "OrderIntent has no instrument/symbol"

        self._seen_submission_ids.add(submission.submission_id)
        return ""
