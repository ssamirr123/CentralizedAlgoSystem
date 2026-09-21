"""
Phase 16.12 -- TccClient: the worker-side HTTP client for
trading/api/worker_routes.py. Uses `requests` (already a repository
dependency, already the library the codebase's own comments associate
with "strategy-side... agents (sync HTTP)" -- see
docs/phase-16-12-distributed-shadow-deployment-validation-report.md
Section 3).

Contains no strategy rules, no risk logic, and no broker code -- it is a
pure, dumb HTTP transport wrapper. Every method returns the JSON response
body (already validated/shaped by the server); this class never
interprets an OrderIntentResult as authorization to do anything further.

RETRY DISCIPLINE (Sections 17/18): `submit_order_intent()` deliberately
does NOT retry automatically -- retrying an OrderIntent submission is the
CALLER's decision (WorkerRunner), and the contract is: a retry MUST reuse
the exact same `submission_id` (and the exact same `idempotency_key`)
passed the first time, never mint fresh ones just because the first
attempt's response was lost or timed out. This class exposes
`submit_order_intent(..., submission_id=...)` accepting a caller-supplied
id specifically so a retry can honor that contract; it never generates
one internally that the caller cannot see and reuse.
"""
from __future__ import annotations

from typing import Any

import requests

__all__ = ["TccClient", "WorkerAuthenticationError", "WorkerTransportError"]


class WorkerTransportError(RuntimeError):
    """A transport-level failure (connection error, timeout, 5xx) -- the
    caller must treat this the same as a lost response: NOT as evidence
    the request failed, only that its outcome is unknown (Section 18)."""


class WorkerAuthenticationError(RuntimeError):
    """The TCC rejected this worker's auth secret (401)."""


class TccClient:
    def __init__(self, *, base_url: str, worker_id: str, auth_secret: str, timeout: float = 5.0, session: requests.Session | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._worker_id = worker_id
        self._auth_secret = auth_secret
        self._timeout = timeout
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"X-Worker-Auth": self._auth_secret}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(f"{self._base_url}{path}", json=body, headers=self._headers(), timeout=self._timeout)
        except requests.RequestException as exc:
            raise WorkerTransportError(str(exc)) from exc
        if response.status_code == 401:
            raise WorkerAuthenticationError(response.text)
        if response.status_code >= 500:
            raise WorkerTransportError(f"server error {response.status_code}: {response.text}")
        return response.json()

    def server_time(self) -> str:
        response = self._session.get(f"{self._base_url}/api/worker/time", timeout=self._timeout)
        response.raise_for_status()
        return response.json()["server_time"]

    def register(self, *, name: str, version: str = "", git_sha: str = "", host_identity: str = "") -> dict[str, Any]:
        return self._post(
            "/api/worker/register",
            {"worker_id": self._worker_id, "name": name, "version": version, "git_sha": git_sha, "host_identity": host_identity},
        )

    def heartbeat(self, *, session_id: str, strategy_ids: list[str] | None = None, runtime_state: str | None = None) -> dict[str, Any]:
        return self._post(
            "/api/worker/heartbeat",
            {"worker_id": self._worker_id, "session_id": session_id, "strategy_ids": strategy_ids or [], "runtime_state": runtime_state},
        )

    def report_runtime(
        self, *, session_id: str, strategy_id: str, runtime_state: str, market_data_status: str = "", last_error: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/worker/runtime",
            {
                "worker_id": self._worker_id, "session_id": session_id, "strategy_id": strategy_id,
                "runtime_state": runtime_state, "market_data_status": market_data_status, "last_error": last_error,
            },
        )

    def submit_order_intent(
        self, *, session_id: str, strategy_id: str, evaluation_id: str, generated_at: str, submission_id: str,
        account_id: str, symbol: str, exchange: str, side: str, quantity: int, order_type: str = "MARKET",
        limit_price: float | None = None, trigger_price: float | None = None, idempotency_key: str = "", reason: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/worker/order-intents",
            {
                "worker_id": self._worker_id, "session_id": session_id, "strategy_id": strategy_id,
                "evaluation_id": evaluation_id, "generated_at": generated_at, "submission_id": submission_id,
                "account_id": account_id, "symbol": symbol, "exchange": exchange, "side": side, "quantity": quantity,
                "order_type": order_type, "limit_price": limit_price, "trigger_price": trigger_price,
                "idempotency_key": idempotency_key, "reason": reason,
            },
        )
