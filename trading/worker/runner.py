"""
Phase 16.12 -- WorkerRunner: the actual worker process loop.

    register (once)
         |
         v
    loop:
        heartbeat
        evaluate_once()  -> Strategy.generate_order_intents(market_data)
                          -> submit each resulting OrderIntent via TccClient
        sleep(heartbeat_interval_seconds)

WHAT THIS CLASS NEVER DOES (by construction, not configuration):
  - It never starts a strategy's lifecycle (no call to
    /api/strategy-lifecycle/{id}/command exists anywhere in this file) --
    a human operator using the EXISTING control-plane is the only way a
    strategy ever becomes RUNNING/SHADOW (Section 14). This class runs
    its evaluate loop unconditionally once started, but every resulting
    OrderIntentSubmission is still centrally gated by WorkerCoordinator's
    own is_strategy_active() check -- an evaluate loop running against a
    STOPPED strategy simply gets every submission rejected, never
    executed. Restarting this process (Section 15) re-registers with a
    BRAND NEW session_id (see register()) and does not change that.
  - It never imports a broker adapter or holds a broker credential -- see
    the trading.worker package's own docstring and its dedicated
    structural test.
  - It never retries an OrderIntent submission with a NEW submission_id
    or idempotency_key on failure/timeout -- see submit_intent()'s own
    docstring; a caller-driven retry must reuse retry_submission_id().
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from trading.common.order_intent import OrderIntent
from trading.common.strategy import BaseStrategy
from trading.worker.client import TccClient, WorkerAuthenticationError, WorkerTransportError
from trading.worker.config import WorkerConfig

logger = logging.getLogger("trading.worker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerRunner:
    def __init__(
        self, *, config: WorkerConfig, strategy: BaseStrategy, client: TccClient,
        market_data_source=None, clock=None,
    ) -> None:
        self._config = config
        self._strategy = strategy
        self._client = client
        self._market_data_source = market_data_source
        self._clock = clock or _now
        self._session_id: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def register(self) -> dict:
        """Always mints a BRAND NEW session_id server-side (Section 15) --
        this method carries no memory of any prior session this process
        (or a prior process for the same worker_id) may have held."""
        response = self._client.register(
            name=self._config.worker_name, version=self._config.app_version,
            git_sha=self._config.git_sha, host_identity=self._config.host_identity,
        )
        self._session_id = response["session_id"]
        logger.info("worker %r registered: session_id=%s status=%s", self._config.worker_id, self._session_id, response["status"])
        return response

    def heartbeat(self) -> dict:
        if self._session_id is None:
            raise RuntimeError("heartbeat() called before register()")
        return self._client.heartbeat(session_id=self._session_id, strategy_ids=[self._config.strategy_id])

    def _gather_market_data(self):
        required = self._strategy.required_instruments()
        if not required or self._market_data_source is None:
            return None
        quotes = {}
        for instrument in required:
            quote = self._market_data_source.get_quote(instrument)
            if quote is None:
                return None  # fail closed -- Section 46: missing data means no evaluation, not a partial one
            quotes[instrument] = quote
        return quotes

    def evaluate_once(self) -> list[dict]:
        """One evaluation cycle: generate this strategy's current
        OrderIntents (if any) and submit each, in order, via the real
        HTTP transport. Never calls WorkerCoordinator directly -- see the
        module docstring."""
        if self._session_id is None:
            raise RuntimeError("evaluate_once() called before register()")

        market_data = self._gather_market_data()
        if self._strategy.required_instruments() and market_data is None:
            logger.info("strategy %r: required market data unavailable this cycle -- skipping evaluation", self._config.strategy_id)
            return []

        intents: list[OrderIntent] = self._strategy.generate_order_intents(market_data)
        results = []
        evaluation_id = str(uuid.uuid4())
        for intent in intents:
            submission_id = str(uuid.uuid4())
            result = self.submit_intent(intent, evaluation_id=evaluation_id, submission_id=submission_id)
            results.append(result)
        return results

    def submit_intent(self, intent: OrderIntent, *, evaluation_id: str, submission_id: str) -> dict:
        """A caller retrying a submission whose response was lost MUST
        call this again with the SAME `submission_id` (Sections 17/18) --
        never a fresh one, and never a fresh `intent.idempotency_key`
        either, since the intent itself is unchanged."""
        assert self._session_id is not None
        return self._client.submit_order_intent(
            session_id=self._session_id, strategy_id=self._config.strategy_id, evaluation_id=evaluation_id,
            generated_at=self._clock(), submission_id=submission_id, account_id=intent.account_id,
            symbol=intent.symbol, exchange=intent.exchange, side=intent.side.value, quantity=intent.quantity,
            order_type=intent.order_type.value, limit_price=intent.limit_price, trigger_price=intent.trigger_price,
            idempotency_key=intent.idempotency_key, reason=intent.reason,
        )

    def run_forever(self, *, iterations: int | None = None) -> None:
        """The real process entrypoint loop. `iterations=None` (the
        default, for an actual deployed worker) loops until the process is
        killed; a finite `iterations` is used by tests to run a bounded
        number of cycles deterministically. Registers only if this runner
        has not already registered (e.g. a caller that wants explicit
        control over the moment registration happens, or wants to assign
        the worker's strategy ownership before the first heartbeat, may
        call register() itself first) -- calling register() twice in a row
        would otherwise be rejected by WorkerRegistry's own duplicate-
        session protection while the first session is still ONLINE."""
        if self._session_id is None:
            self.register()
        count = 0
        while iterations is None or count < iterations:
            try:
                self.heartbeat()
                self.evaluate_once()
            except (WorkerTransportError, WorkerAuthenticationError) as exc:
                logger.warning("worker cycle failed (will retry next interval): %s", exc)
            count += 1
            if iterations is None or count < iterations:
                time.sleep(self._config.heartbeat_interval_seconds)
