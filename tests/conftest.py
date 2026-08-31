"""
Shared pytest fixtures for the whole suite.

Isolation model
---------------
DATABASE_URL is pinned (below, before any project import) to a dedicated
SQLite file in a throwaway temp dir -- never the old shared test_api.db.
The `_clean_db` autouse fixture drops and recreates every table before
each test, so every test starts from an empty, well-formed schema.

Nothing here touches real infrastructure: DISABLE_BACKGROUND_WATCHER is
on, TRADING_MODE is paper, broker creds / AWS creds / Telegram creds are
cleared, and `_no_network` (autouse) makes any real socket connect or DNS
lookup raise loudly.
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

# --- environment: set BEFORE importing anything from trading/ or backend/ ---
_TMP_DIR = Path(tempfile.mkdtemp(prefix="cas_pytest_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR / 'test.db'}"
os.environ["CONTROL_API_KEY"] = "test-key"
os.environ["DISABLE_BACKGROUND_WATCHER"] = "true"
os.environ["TRADING_MODE"] = "paper"
os.environ["BROKER"] = "paper"
# Stage 18 auth: fixed test signing secret; plain-http test client.
os.environ["AUTH_SECRET_KEY"] = "test-secret-" + "a" * 40
os.environ["AUTH_COOKIE_SECURE"] = "false"
os.environ.pop("AUTH_BOOTSTRAP_ADMIN_USERNAME", None)
os.environ.pop("AUTH_BOOTSTRAP_ADMIN_PASSWORD", None)
# Make sure nothing can reach a real external service.
for _var in (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "LAMBDA_FUNCTION_NAME",
    "ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_ACCESS_TOKEN",
    "ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET",
    "ICICI_BREEZE_API_KEY", "ICICI_BREEZE_API_SECRET", "ICICI_BREEZE_SESSION_TOKEN",
):
    os.environ.pop(_var, None)

from trading.database.connection import SessionLocal, engine  # noqa: E402
from trading.database import models  # noqa: E402,F401  (registers tables)
from trading.database.connection import Base  # noqa: E402

API_KEY = os.environ["CONTROL_API_KEY"]

# Exercise the canonical logging path: one structured JSON stdout handler
# on root (Stage 10). ensure_ascii escapes the alert-service emoji, so the
# old cp1252 UnicodeEncodeError can no longer happen mid-test.
from trading.common.logger import configure_logging  # noqa: E402

configure_logging()


# --------------------------------------------------------------------------
# Network kill-switch
# --------------------------------------------------------------------------
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(scope="session", autouse=True)
def _no_network():
    """Any real outbound socket connect to a NON-loopback address raises.
    Loopback is left alone -- the Windows Proactor event loop and the
    TestClient portal need socketpair()/self-pipe on 127.0.0.1. SQLite is
    a local file. Anything reaching for the internet (real
    requests/httpx/urllib/boto) fails loudly instead."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _host_of(address):
        if isinstance(address, tuple):
            return address[0]
        return address

    def _guard(fn):
        def _wrapped(self, address, *a, **k):
            if _host_of(address) not in _LOOPBACK:
                raise RuntimeError(f"network access disabled during tests (blocked connect to {address!r})")
            return fn(self, address, *a, **k)
        return _wrapped

    socket.socket.connect = _guard(real_connect)
    socket.socket.connect_ex = _guard(real_connect_ex)
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def db_engine():
    return engine


@pytest.fixture(autouse=True)
def _clean_db(db_engine):
    """Fresh schema for every test."""
    Base.metadata.drop_all(bind=db_engine)
    Base.metadata.create_all(bind=db_engine)
    yield
    Base.metadata.drop_all(bind=db_engine)
    Base.metadata.create_all(bind=db_engine)


@pytest.fixture
def db_session():
    """A SQLAlchemy session bound to the test DB. Caller need not commit
    for the row to be visible to the app -- same engine, same file."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------
# Application / client
# --------------------------------------------------------------------------
@pytest.fixture
def app():
    from trading.api.app import create_app

    return create_app()


@pytest.fixture
def client(app):
    """TestClient with the lifespan run (init_db + watcher-skip)."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def api_key():
    return API_KEY


@pytest.fixture(scope="session")
def auth(api_key):
    return {"X-API-Key": api_key}


# --------------------------------------------------------------------------
# Mocked Lambda / SSM orchestrator (control-center API side)
# --------------------------------------------------------------------------
@pytest.fixture
def mock_lambda():
    """Patches the control-center API's Lambda client so no boto3 / AWS
    call is ever made. Yields a namespace with `.invoke` and
    `.invoke_async` MagicMocks; `.invoke` has a benign default return."""
    from types import SimpleNamespace
    from unittest.mock import patch

    with patch("trading.api.routes.invoke_orchestrator") as m_invoke, \
         patch("trading.api.routes.invoke_orchestrator_async") as m_async:
        m_invoke.return_value = {
            "success": True, "job_id": "cmd-test", "status": "STARTING",
        }
        yield SimpleNamespace(invoke=m_invoke, invoke_async=m_async)
