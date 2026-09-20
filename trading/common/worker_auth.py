"""
WorkerAuthRegistry -- Phase 16.12: a per-worker machine-authentication
secret registry, SEPARATE from every existing authentication lane:

  - Human operators authenticate via username/password -> a short-lived
    JWT Bearer token (trading/api/security/tokens.py, Phase 15D.7).
  - The existing "machine" lane (trading/api/deps.py's
    _service_principal_from_key()) is ONE fixed shared secret
    (CONTROL_API_KEY) granting a single anonymous "service" identity
    VIEW-only rights -- it has no per-machine identity at all and is
    already documented as "can never start/stop/restart a process".

Neither is suitable for a worker: a worker needs a distinct identity
(worker_id-bound) and needs to perform write-heavy calls (register,
heartbeat, submit an OrderIntent) that go well beyond VIEW. Rather than
weaken CONTROL_API_KEY's documented VIEW-only guarantee, or fold worker
traffic into the human RBAC model, this module is a THIRD, independent,
narrow lane used ONLY by the new /api/worker/* routes
(trading/api/worker_routes.py) -- it grants no Permission, no RBAC role,
and cannot be used to call any existing operator/admin route.

Secrets are provisioned CENTRALLY, out of band, before a worker's first
call: an operator sets WORKER_AUTH_SECRETS (see from_env()) in the
central TCC's own environment/secret store -- the same worker_id/secret
pair is then given to that one worker via ITS OWN environment
(WORKER_AUTH_SECRET). A worker's very first HTTP call (register) already
presents its secret; there is no unauthenticated bootstrap/mint-a-secret
endpoint anywhere in this module or in worker_routes.py.

This registry NEVER returns a stored secret to a caller -- verify() only
ever returns a bool. No secret is ever logged (callers must not log the
`presented` value either) or included in any API response schema.
"""
from __future__ import annotations

import hmac
import os
import threading

__all__ = ["WorkerAuthRegistry"]


class WorkerAuthRegistry:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = dict(secrets or {})
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, raw: str | None = None) -> "WorkerAuthRegistry":
        """Parses WORKER_AUTH_SECRETS (or the given `raw` string, for
        tests) as a comma-separated "worker_id:secret" list, e.g.
        "worker-cvn:abc123,worker-ds:def456,worker-vh:ghi789". Unset/empty
        (the default -- no production value invented) means NO worker can
        authenticate until an operator explicitly provisions one, which is
        the correct fail-closed default, matching every other
        not-yet-configured limit/secret in this codebase."""
        value = raw if raw is not None else os.environ.get("WORKER_AUTH_SECRETS", "")
        secrets: dict[str, str] = {}
        for pair in value.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            worker_id, secret = pair.split(":", 1)
            worker_id, secret = worker_id.strip(), secret.strip()
            if worker_id and secret:
                secrets[worker_id] = secret
        return cls(secrets)

    def provision(self, worker_id: str, secret: str) -> None:
        """Adds/replaces one worker's secret. Additive, so tests and an
        eventual admin provisioning route can each call this without
        needing to reconstruct the whole registry."""
        with self._lock:
            self._secrets[worker_id] = secret

    def revoke(self, worker_id: str) -> None:
        with self._lock:
            self._secrets.pop(worker_id, None)

    def is_provisioned(self, worker_id: str) -> bool:
        """Read-only existence check -- never reveals the secret itself."""
        with self._lock:
            return worker_id in self._secrets

    def verify(self, worker_id: str, presented: str | None) -> bool:
        """Constant-time comparison (hmac.compare_digest), mirroring the
        exact discipline trading/api/deps.py's own
        _service_principal_from_key() already uses for CONTROL_API_KEY --
        never a plain `==`, which would leak timing information about how
        many leading characters matched."""
        if not presented:
            return False
        with self._lock:
            expected = self._secrets.get(worker_id)
        if expected is None:
            return False
        return hmac.compare_digest(presented, expected)
