"""Phase 16.12: real distributed transport validation.

The central TCC runs as a REAL, separate OS PROCESS (`python -m uvicorn
trading.api.app:create_app --factory`) bound to a real 127.0.0.1 TCP
socket -- not the in-process ASGI TestClient shortcut, and not even an
in-process background thread (which would still share this test's Python
interpreter and module state). Every "worker" in this file is a
`trading.worker.client.TccClient` making REAL `requests` HTTP calls over
that real socket, in the test's own process (a genuine second OS process
per worker is unnecessary to prove the property that matters here: a
worker reaches WorkerCoordinator ONLY through HTTP, never a shortcut --
`TccClient` structurally cannot import WorkerRegistry/WorkerCoordinator,
see trading/worker/__init__.py's own docstring and its structural test).

Strategy-to-worker ownership is assigned via the real
POST /api/workers/{worker_id}/assign endpoint (added in this phase to
close a gap Phases 16.9-16.11 left open), never by reaching into the
server process's memory -- there IS no shared memory here; it is a
genuinely separate process.
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest
import requests

from trading.common.market_data_gateway import FixedMarketDataSource
from trading.worker.client import TccClient, WorkerAuthenticationError
from trading.worker.config import WorkerConfig
from trading.worker.runner import WorkerRunner

_ADMIN_USER = "admin"
_ADMIN_PASSWORD = "Sup3rSecret!2026"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer:
    """A genuinely separate OS process running the real TCC app, with its
    own isolated temp SQLite DB and its own WorkerAuthRegistry/
    OperationalAlertStore/etc. -- no state is shared with the test
    process except over the HTTP socket itself."""

    def __init__(self, *, worker_secrets: str = "", extra_env: dict[str, str] | None = None):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_file.close()
        env = dict(os.environ)
        env.update({
            "DATABASE_URL": f"sqlite:///{self._db_file.name}",
            "AUTH_BOOTSTRAP_ADMIN_USERNAME": _ADMIN_USER,
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD": _ADMIN_PASSWORD,
            "AUTH_SECRET_KEY": os.environ.get("AUTH_SECRET_KEY", "test-secret-" + "a" * 40),
            "WORKER_AUTH_SECRETS": worker_secrets,
            "DISABLE_BACKGROUND_WATCHER": "1",
            "APP_ENV": "development",
            "REALTIME_ENABLED": "false",
        })
        env.update(extra_env or {})
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "trading.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            env=env, cwd=os.getcwd(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        # Drain the child's stdout continuously in a background thread --
        # otherwise, once uvicorn's own (buffered) output fills the OS
        # pipe buffer, its next write() blocks forever and the process
        # never finishes starting up. Keep the last N lines for
        # diagnostics if it exits early or never becomes ready.
        self._output_lines: list[str] = []
        self._output_lock = threading.Lock()

        def _drain():
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                with self._output_lock:
                    self._output_lines.append(line)
                    if len(self._output_lines) > 200:
                        self._output_lines.pop(0)

        self._drain_thread = threading.Thread(target=_drain, daemon=True)
        self._drain_thread.start()

    def _recent_output(self) -> str:
        with self._output_lock:
            return "".join(self._output_lines)

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        last_exc = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"TCC process exited early (code={self._proc.returncode}):\n{self._recent_output()}")
            try:
                r = requests.get(f"{self.base_url}/api/health", timeout=1.0)
                if r.status_code in (200, 503):
                    return
            except requests.RequestException as exc:
                last_exc = exc
            time.sleep(0.2)
        raise RuntimeError(f"TCC process did not become ready in time: {last_exc}\nOutput so far:\n{self._recent_output()}")

    def stop(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)
        try:
            os.unlink(self._db_file.name)
        except OSError:
            pass


@pytest.fixture
def live_server():
    """Function-scoped -- a genuinely fresh, isolated TCC process per
    test (no shared worker/strategy/session state to leak between tests,
    exactly like every other phase's fresh-`_stack()`-per-test pattern).
    Costs a few seconds of process-startup time per test; correctness
    over speed for a safety-critical suite."""
    server = _LiveServer(worker_secrets="worker-cvn:secret-cvn,worker-ds:secret-ds,worker-vh:secret-vh")
    server.wait_ready()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def base_url(live_server) -> str:
    return live_server.base_url


def _login(base_url: str) -> requests.Session:
    session = requests.Session()
    r = session.post(f"{base_url}/api/auth/login", json={"username": _ADMIN_USER, "password": _ADMIN_PASSWORD})
    r.raise_for_status()
    session.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    return session


@pytest.fixture
def admin(live_server) -> requests.Session:
    """One login per test, against that test's own fresh server -- the
    login rate limiter (Phase 15D.7, unweakened by this phase) is scoped
    per-process, so a fresh process per test also means a fresh rate-limit
    window per test."""
    return _login(live_server.base_url)


def _admin_session(base_url: str) -> requests.Session:
    """Kept for the few call sites that need a genuinely fresh session
    (none currently do after the `admin` fixture change) -- retained as a
    thin wrapper around _login for clarity."""
    return _login(base_url)


def _assign_and_start(admin: requests.Session, base_url: str, strategy_id: str, account_id: str) -> None:
    r = admin.post(f"{base_url}/api/assignments", json={"strategy_id": strategy_id, "account_id": account_id})
    assert r.status_code in (201, 409), r.text  # 409 if a prior test in this module already assigned it
    r2 = admin.post(f"{base_url}/api/strategy-lifecycle/{strategy_id}/command", json={"command": "START"})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    # The command endpoint returns 200 even for a REJECTED/NOOP outcome
    # (the HTTP call itself succeeded; the command's own result is in the
    # body) -- ACCEPTED is a genuine start, NOOP means it was already
    # RUNNING/SHADOW from an earlier test sharing this module-scoped
    # server, both are fine; anything else is a real setup failure.
    assert body["result"] in ("ACCEPTED", "NOOP"), body


def _assign_worker(admin: requests.Session, base_url: str, worker_id: str, strategy_id: str) -> None:
    r = admin.post(f"{base_url}/api/workers/{worker_id}/assign", json={"strategy_id": strategy_id})
    assert r.status_code == 200, r.text


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _unique_account_set(prefix: str) -> dict[str, str]:
    """All three demo strategies share ANGEL_MAIN -- the ONLY broker the
    real build_execution_state() marks available (dhan/icici_breeze are
    deliberately marked unavailable there, matching this repository's
    actual production posture -- Phase 8/9 adapters were never validated
    against a real account). Multiple strategies sharing one account is
    an explicitly supported, already-tested scenario (Phase 16.10)."""
    return {"CombinedVwapNifty": "ANGEL_MAIN", "DoubleStraddelAlgo": "ANGEL_MAIN", "Vwap_Algo_Nifty_hedge": "ANGEL_MAIN"}


# --------------------------------------------------------------------------- #
# Local transport acceptance (Section 22)
# --------------------------------------------------------------------------- #
def test_three_workers_register_authenticate_and_appear_online(base_url, admin):
    accounts = _unique_account_set("t1")
    for sid, account in accounts.items():
        _assign_and_start(admin, base_url, sid, account)

    clients = {
        "worker-cvn": TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn"),
        "worker-ds": TccClient(base_url=base_url, worker_id="worker-ds", auth_secret="secret-ds"),
        "worker-vh": TccClient(base_url=base_url, worker_id="worker-vh", auth_secret="secret-vh"),
    }
    sessions = {wid: c.register(name=wid)["session_id"] for wid, c in clients.items()}
    assert all(s for s in sessions.values())

    for wid, sid in (("worker-cvn", "CombinedVwapNifty"), ("worker-ds", "DoubleStraddelAlgo"), ("worker-vh", "Vwap_Algo_Nifty_hedge")):
        _assign_worker(admin, base_url, wid, sid)

    r = admin.get(f"{base_url}/api/operations/workers")
    assert r.status_code == 200
    statuses = {w["worker_id"]: w["status"] for w in r.json()}
    assert statuses == {"worker-cvn": "ONLINE", "worker-ds": "ONLINE", "worker-vh": "ONLINE"}


def test_wrong_worker_secret_is_rejected_over_real_http(base_url):
    client = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="wrong-secret")
    with pytest.raises(WorkerAuthenticationError):
        client.register(name="cvn")


def test_unknown_worker_is_rejected_over_real_http(base_url):
    client = TccClient(base_url=base_url, worker_id="not-provisioned", auth_secret="anything")
    with pytest.raises(WorkerAuthenticationError):
        client.register(name="ghost")


def test_old_session_rejected_after_re_registration_over_real_http():
    # A genuine restart looks, from TCC's perspective, identical to "the
    # old process stopped heartbeating" -- re-registering while the OLD
    # session is still within its heartbeat timeout is correctly refused
    # (Phase 16.9's own duplicate-worker protection: two processes must
    # never both claim ONLINE for the same worker_id at once). This test
    # uses a short WORKER_HEARTBEAT_TIMEOUT_SECONDS (Phase 16.12) so it can
    # prove the REAL restart-after-timeout path deterministically and
    # quickly rather than waiting a real 30s.
    server = _LiveServer(worker_secrets="worker-cvn:secret-cvn", extra_env={"WORKER_HEARTBEAT_TIMEOUT_SECONDS": "1"})
    server.wait_ready()
    try:
        client = TccClient(base_url=server.base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
        old_session = client.register(name="cvn")["session_id"]
        assert client.heartbeat(session_id=old_session)["status"] == "ONLINE"

        time.sleep(1.5)  # exceed the 1s heartbeat timeout -- old session goes stale/OFFLINE

        new = client.register(name="cvn")  # restart / re-registration now succeeds
        assert new["session_id"] != old_session

        raw = requests.post(
            f"{server.base_url}/api/worker/heartbeat",
            json={"worker_id": "worker-cvn", "session_id": old_session},
            headers={"X-Worker-Auth": "secret-cvn"},
        )
        assert raw.status_code == 409  # the OLD session_id is still rejected after the restart -- Section 15
    finally:
        server.stop()


def test_cross_worker_submission_rejected_over_real_http(base_url, admin):
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    ds = TccClient(base_url=base_url, worker_id="worker-ds", auth_secret="secret-ds")
    ds_session = ds.register(name="ds")["session_id"]

    # worker-ds is correctly authenticated with a valid session of its
    # OWN, but CombinedVwapNifty is not (and never will be) assigned to
    # it -- this must be rejected on OWNERSHIP grounds, not session.
    result = ds.submit_order_intent(
        session_id=ds_session, strategy_id="CombinedVwapNifty", evaluation_id="e1",
        generated_at=_now(), submission_id=str(uuid.uuid4()), account_id="ANGEL_MAIN",
        symbol="NIFTY", exchange="NFO", side="BUY", quantity=1, idempotency_key="cross-1",
    )
    assert result["accepted"] is False
    assert "not assigned to worker" in result["reason"]


def test_malformed_order_intent_rejected_over_real_http(base_url, admin):
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    client = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    session_id = client.register(name="cvn")["session_id"]
    _assign_worker(admin, base_url, "worker-cvn", "CombinedVwapNifty")

    raw = requests.post(
        f"{base_url}/api/worker/order-intents",
        json={
            "worker_id": "worker-cvn", "session_id": session_id, "strategy_id": "CombinedVwapNifty",
            "evaluation_id": "e1", "generated_at": _now(), "submission_id": str(uuid.uuid4()),
            "account_id": "ANGEL_MAIN", "symbol": "NIFTY", "exchange": "NFO", "side": "SIDEWAYS", "quantity": 1,
        },
        headers={"X-Worker-Auth": "secret-cvn"},
    )
    assert raw.status_code == 422


# --------------------------------------------------------------------------- #
# Local three-strategy shadow flow (Section 23)
# --------------------------------------------------------------------------- #
def test_three_strategy_shadow_flow_through_real_transport(base_url, admin):
    configs = [
        ("worker-cvn", "secret-cvn", "CombinedVwapNifty", "ANGEL_MAIN"),
        ("worker-ds", "secret-ds", "DoubleStraddelAlgo", "ANGEL_MAIN"),
        ("worker-vh", "secret-vh", "Vwap_Algo_Nifty_hedge", "ANGEL_MAIN"),
    ]
    for _, _, sid, account in configs:
        _assign_and_start(admin, base_url, sid, account)

    results = []
    for wid, secret, sid, account in configs:
        client = TccClient(base_url=base_url, worker_id=wid, auth_secret=secret)
        session_id = client.register(name=wid)["session_id"]
        _assign_worker(admin, base_url, wid, sid)

        result = client.submit_order_intent(
            session_id=session_id, strategy_id=sid, evaluation_id="e1", generated_at=_now(),
            submission_id=str(uuid.uuid4()), account_id=account, symbol="NIFTY", exchange="NFO", side="BUY",
            quantity=1, order_type="LIMIT", limit_price=100.0, idempotency_key=f"{wid}-shadow-flow",
        )
        results.append(result)

    assert all(r["accepted"] for r in results), results
    assert all(r["execution_result"]["success"] for r in results)


# --------------------------------------------------------------------------- #
# Network retry / idempotency across real HTTP (Sections 17/44)
# --------------------------------------------------------------------------- #
def test_duplicate_delivery_over_real_http_yields_one_logical_execution(base_url, admin):
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    client = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    session_id = client.register(name="cvn")["session_id"]
    _assign_worker(admin, base_url, "worker-cvn", "CombinedVwapNifty")

    kwargs = dict(
        session_id=session_id, strategy_id="CombinedVwapNifty", evaluation_id="e1", generated_at=_now(),
        submission_id=str(uuid.uuid4()), account_id="ANGEL_MAIN", symbol="NIFTY", exchange="NFO", side="BUY",
        quantity=1, order_type="LIMIT", limit_price=100.0, idempotency_key="retry-key-1",
    )
    first = client.submit_order_intent(**kwargs)
    second = client.submit_order_intent(**kwargs)  # simulated retry: identical submission_id + idempotency_key

    assert first["accepted"] is True
    assert second == first  # one logical execution, replayed verbatim, not a duplicate


# --------------------------------------------------------------------------- #
# Stale intent over real HTTP (Sections 19/45)
# --------------------------------------------------------------------------- #
def test_stale_intent_rejected_over_real_http(base_url, admin):
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    client = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    session_id = client.register(name="cvn")["session_id"]
    _assign_worker(admin, base_url, "worker-cvn", "CombinedVwapNifty")

    stale_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=999)).isoformat()
    result = client.submit_order_intent(
        session_id=session_id, strategy_id="CombinedVwapNifty", evaluation_id="e1", generated_at=stale_ts,
        submission_id=str(uuid.uuid4()), account_id="ANGEL_MAIN", symbol="NIFTY", exchange="NFO", side="BUY",
        quantity=1, idempotency_key="stale-1",
    )
    assert result["accepted"] is False
    assert "stale" in result["reason"].lower()


# --------------------------------------------------------------------------- #
# Kill switch distributed test (Section 41)
# --------------------------------------------------------------------------- #
def test_kill_switch_rejects_all_three_workers_over_real_http(base_url, admin):
    configs = [
        ("worker-cvn", "secret-cvn", "CombinedVwapNifty", "ANGEL_MAIN"),
        ("worker-ds", "secret-ds", "DoubleStraddelAlgo", "ANGEL_MAIN"),
        ("worker-vh", "secret-vh", "Vwap_Algo_Nifty_hedge", "ANGEL_MAIN"),
    ]
    for _, _, sid, account in configs:
        _assign_and_start(admin, base_url, sid, account)

    r = admin.post(f"{base_url}/api/risk/kill-switch", json={"engaged": True, "reason": "phase 16.12 distributed test"})
    assert r.status_code == 200

    try:
        results = []
        for wid, secret, sid, account in configs:
            client = TccClient(base_url=base_url, worker_id=wid, auth_secret=secret)
            session_id = client.register(name=wid)["session_id"]
            _assign_worker(admin, base_url, wid, sid)
            result = client.submit_order_intent(
                session_id=session_id, strategy_id=sid, evaluation_id="e1", generated_at=_now(),
                submission_id=str(uuid.uuid4()), account_id=account, symbol="NIFTY", exchange="NFO", side="BUY",
                quantity=1, idempotency_key=f"{wid}-kill-test",
            )
            results.append(result)

        assert all(r["accepted"] is False for r in results), results

        ops = admin.get(f"{base_url}/api/operations/summary").json()
        assert ops["safety"]["kill_switch_engaged"] is True
        assert any(a["code"] == "KILL_SWITCH_ENGAGED" for a in ops["active_alerts"])
    finally:
        admin.post(f"{base_url}/api/risk/kill-switch", json={"engaged": False})  # restore safe test state


# --------------------------------------------------------------------------- #
# Portfolio-risk distributed test (Section 42)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def risk_limited_server():
    """A SEPARATE TCC process, started with PORTFOLIO_MAX_EXPOSURE set --
    Phase 16.12's one env-configurable portfolio limit (see
    trading/api/execution_state.py) -- so this module's other tests (which
    rely on an otherwise-unconfigured PortfolioRiskManager) are never
    affected by it."""
    server = _LiveServer(
        worker_secrets="worker-cvn:secret-cvn,worker-ds:secret-ds",
        extra_env={"PORTFOLIO_MAX_EXPOSURE": "150"},
    )
    server.wait_ready()
    try:
        yield server
    finally:
        server.stop()


def test_portfolio_risk_distributed_first_accepted_second_rejected(risk_limited_server):
    base_url = risk_limited_server.base_url
    admin = _admin_session(base_url)
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    _assign_and_start(admin, base_url, "DoubleStraddelAlgo", "ANGEL_MAIN")

    cvn = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    ds = TccClient(base_url=base_url, worker_id="worker-ds", auth_secret="secret-ds")
    cvn_session = cvn.register(name="cvn")["session_id"]
    ds_session = ds.register(name="ds")["session_id"]
    _assign_worker(admin, base_url, "worker-cvn", "CombinedVwapNifty")
    _assign_worker(admin, base_url, "worker-ds", "DoubleStraddelAlgo")

    # PORTFOLIO_MAX_EXPOSURE=150 -- the first 100-notional order fits
    # (100 <= 150); the second would project to 200 > 150 and must be
    # rejected purely by the real, network-reachable PortfolioRiskManager.
    r1 = cvn.submit_order_intent(
        session_id=cvn_session, strategy_id="CombinedVwapNifty", evaluation_id="e1", generated_at=_now(),
        submission_id=str(uuid.uuid4()), account_id="ANGEL_MAIN", symbol="NIFTY", exchange="NFO", side="BUY",
        quantity=1, order_type="LIMIT", limit_price=100.0, idempotency_key="risk-a",
    )
    r2 = ds.submit_order_intent(
        session_id=ds_session, strategy_id="DoubleStraddelAlgo", evaluation_id="e1", generated_at=_now(),
        submission_id=str(uuid.uuid4()), account_id="ANGEL_MAIN", symbol="NIFTY", exchange="NFO", side="BUY",
        quantity=1, order_type="LIMIT", limit_price=100.0, idempotency_key="risk-b",
    )
    assert r1["accepted"] is True
    assert r2["accepted"] is False
    assert "PORTFOLIO_EXPOSURE_LIMIT" in r2["reason"]


# --------------------------------------------------------------------------- #
# Concurrent worker test (Section 43)
# --------------------------------------------------------------------------- #
def test_concurrent_worker_submissions_are_both_independently_authoritative(base_url, admin):
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    _assign_and_start(admin, base_url, "DoubleStraddelAlgo", "ANGEL_MAIN")

    cvn = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    ds = TccClient(base_url=base_url, worker_id="worker-ds", auth_secret="secret-ds")
    cvn_session = cvn.register(name="cvn")["session_id"]
    ds_session = ds.register(name="ds")["session_id"]
    _assign_worker(admin, base_url, "worker-cvn", "CombinedVwapNifty")
    _assign_worker(admin, base_url, "worker-ds", "DoubleStraddelAlgo")

    results: dict[str, dict] = {}

    def submit(name, client, session_id, strategy_id, account_id, key):
        results[name] = client.submit_order_intent(
            session_id=session_id, strategy_id=strategy_id, evaluation_id="e1", generated_at=_now(),
            submission_id=str(uuid.uuid4()), account_id=account_id, symbol="NIFTY", exchange="NFO", side="BUY",
            quantity=1, order_type="LIMIT", limit_price=100.0, idempotency_key=key,
        )

    t1 = threading.Thread(target=submit, args=("a", cvn, cvn_session, "CombinedVwapNifty", "ANGEL_MAIN", "conc-a"))
    t2 = threading.Thread(target=submit, args=("b", ds, ds_session, "DoubleStraddelAlgo", "ANGEL_MAIN", "conc-b"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["a"]["accepted"] is True
    assert results["b"]["accepted"] is True
    assert results["a"]["execution_result"]["account_id"] == "ANGEL_MAIN"
    assert results["b"]["execution_result"]["account_id"] == "ANGEL_MAIN"


# --------------------------------------------------------------------------- #
# No-auto-start / no-auto-resume via a real WorkerRunner (Sections 14/15)
# --------------------------------------------------------------------------- #
def test_worker_runner_never_starts_a_strategy(base_url, admin):
    _assign_and_start(admin, base_url, "CombinedVwapNifty", "ANGEL_MAIN")
    admin.post(f"{base_url}/api/strategy-lifecycle/CombinedVwapNifty/command", json={"command": "STOP"})

    from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy

    strategy = CombinedVwapNiftyStrategy(ce_instrument="NIFTY_CE", pe_instrument="NIFTY_PE", account_id="ANGEL_MAIN")
    config = WorkerConfig(
        tcc_url=base_url, worker_id="worker-cvn", worker_name="cvn", auth_secret="secret-cvn",
        strategy_id="CombinedVwapNifty", heartbeat_interval_seconds=0.01, app_version="test", git_sha="test",
        host_identity="test-host",
    )
    client = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    runner = WorkerRunner(config=config, strategy=strategy, client=client, market_data_source=FixedMarketDataSource())
    runner.register()
    _assign_worker(admin, base_url, "worker-cvn", "CombinedVwapNifty")
    runner.run_forever(iterations=2)  # register/heartbeat/evaluate loop, bounded

    lifecycle = admin.get(f"{base_url}/api/strategy-lifecycle/CombinedVwapNifty").json()
    assert lifecycle["lifecycle_state"] == "STOPPED"  # a running worker process never brings it back


# --------------------------------------------------------------------------- #
# Version mismatch (Section 47)
# --------------------------------------------------------------------------- #
def test_version_mismatch_alert_appears_over_real_http(base_url, admin):
    client = TccClient(base_url=base_url, worker_id="worker-cvn", auth_secret="secret-cvn")
    client.register(name="cvn", git_sha="totally-different-sha-value")

    ops = admin.get(f"{base_url}/api/operations/summary").json()
    tcc_sha = ops["system"]["git_sha"]
    if tcc_sha and tcc_sha != "unknown":
        assert any(a["code"] == "WORKER_VERSION_MISMATCH" and a["source_id"] == "worker-cvn" for a in ops["active_alerts"])
