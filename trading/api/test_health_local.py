"""
Tests for the control-center health endpoint GET /api/health (Stage 6):
database connected, database failure, response structure, no-auth, and
that the legacy GET /health is left unchanged.

Isolated SQLite DB, no network. Runs like the other test_*_local.py
scripts; folds into the tests/ tree in Stage 8.
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_DB = Path(tempfile.gettempdir()) / "test_health.db"
_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["CONTROL_API_KEY"] = "health-test-key"
os.environ["DISABLE_BACKGROUND_WATCHER"] = "true"
os.environ.setdefault("TRADING_MODE", "paper")

from sqlalchemy.exc import OperationalError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        failures.append(name)


_EXPECTED_KEYS = {"status", "service", "timestamp", "database"}

with TestClient(app) as client:
    # ---- database connected -------------------------------------------
    r = client.get("/api/health")
    check("db connected -> 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("response has exactly {status, service, timestamp, database}",
          set(body) == _EXPECTED_KEYS, str(sorted(body)))
    check("status == ok", body.get("status") == "ok", str(body.get("status")))
    check("service == centralized-algo-backend",
          body.get("service") == "centralized-algo-backend", str(body.get("service")))
    check("database == connected", body.get("database") == "connected", str(body.get("database")))
    try:
        datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00"))
        ts_ok = True
    except Exception:
        ts_ok = False
    check("timestamp is ISO-8601", ts_ok, str(body.get("timestamp")))

    # ---- no auth required -------------------------------------------
    r_noauth = client.get("/api/health")  # no X-API-Key header
    check("no API key required (not 401/403)", r_noauth.status_code not in (401, 403),
          str(r_noauth.status_code))

    # ---- database failure -----------------------------------------
    def _boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("simulated: database is down"))

    with patch("trading.api.health.engine") as fake_engine:
        fake_engine.connect.side_effect = _boom
        r = client.get("/api/health")
    check("db failure -> endpoint does NOT 500", r.status_code != 500, str(r.status_code))
    check("db failure -> 503", r.status_code == 503, str(r.status_code))
    body = r.json()
    check("db-failure response keeps the same 4 keys",
          set(body) == _EXPECTED_KEYS, str(sorted(body)))
    check("db failure -> status == degraded", body.get("status") == "degraded", str(body.get("status")))
    check("db failure -> database field indicates an error",
          isinstance(body.get("database"), str) and body["database"].startswith("error"),
          str(body.get("database")))
    check("db failure -> error string carries no connection string / secret",
          "sqlite" not in body.get("database", "").lower()
          and "://" not in body.get("database", ""),
          str(body.get("database")))
    check("db failure -> service still reported",
          body.get("service") == "centralized-algo-backend", str(body.get("service")))

    # ---- recovers after failure ---------------------------------
    r = client.get("/api/health")
    check("recovers to ok/connected after transient failure",
          r.status_code == 200 and r.json().get("database") == "connected", str(r.status_code))

    # ---- legacy GET /health unchanged -------------------------
    r = client.get("/health")
    check("legacy GET /health still 200", r.status_code == 200, str(r.status_code))
    lb = r.json()
    check("legacy /health body unchanged (status, timestamp_utc, service)",
          set(lb) == {"status", "timestamp_utc", "service"}
          and lb["status"] == "ok"
          and lb["service"] == "central-strategy-monitor",
          str(lb))
    check("legacy /health and /api/health are different shapes",
          "timestamp_utc" in lb and "database" not in lb, "")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All /api/health checks passed.")
