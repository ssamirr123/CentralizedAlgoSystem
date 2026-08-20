"""Local test for Milestone 12: rate limiting, live server health, heartbeat alerting."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///test_m12.db"
os.environ["CONTROL_API_KEY"] = "test-key-123"
os.environ["DISABLE_BACKGROUND_WATCHER"] = "true"
# Deliberately high here -- functional tests below need many successful
# requests on the same key/window. The actual low-limit blocking behavior
# is tested at the END by monkeypatching the module constant directly,
# since it's read from the env once at import time, not on every request.

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402
from trading.api import deps as api_deps  # noqa: E402
from trading.database import models  # noqa: E402
from trading.database.connection import SessionLocal, init_db  # noqa: E402

failures = []


def check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    print(f"[{status_}] {name} {detail}")
    if not condition:
        failures.append(name)


init_db()
db = SessionLocal()
db.query(models.Heartbeat).delete()
db.query(models.RateLimitWindow).delete()
db.query(models.Algo).delete()
db.query(models.Server).delete()
db.commit()
server = models.Server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="STOPPED")
db.add(server)
db.commit()
db.close()

client = TestClient(app)
AUTH = {"X-API-Key": "test-key-123"}

# =========================== LIVE SERVER HEALTH ===========================

with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    mock_invoke.return_value = {
        "success": True, "server_id": "i-test", "ec2_status": "RUNNING",
        "ssm_status": "ONLINE", "last_ping": "2026-08-13T00:00:00Z", "healthy": True,
    }
    r = client.get("/api/server/status", params={"server_id": "ec2-1", "live": "true"}, headers=AUTH)
    check("live=true -> 200", r.status_code == 200, str(r.status_code) + " " + r.text)
    body = r.json()
    check(
        "live check populates ssm_status/live_check_healthy and updates status",
        body["status"] == "RUNNING" and body["ssm_status"] == "ONLINE" and body["live_check_healthy"] is True,
        str(body),
    )
    check("check_ec2_health invoked (not start/stop)", mock_invoke.call_args.args[0] == "check_ec2_health", str(mock_invoke.call_args))

# verify the DB row was actually updated by the live check
db = SessionLocal()
server_row = db.query(models.Server).filter(models.Server.name == "ec2-1").one()
check("server.status updated in DB from live check", server_row.status == "RUNNING", server_row.status)
db.close()

# live=false (default) -> no Lambda call, no ssm_status
with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    r = client.get("/api/server/status", params={"server_id": "ec2-1"}, headers=AUTH)
    check("live omitted -> no Lambda call", not mock_invoke.called, str(mock_invoke.called))
    check("live omitted -> ssm_status is None", r.json()["ssm_status"] is None, str(r.json()))

# live check Lambda failure -> degrades to cached value, doesn't 500
with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    from trading.api.lambda_client import LambdaInvokeError
    mock_invoke.side_effect = LambdaInvokeError("lambda unreachable")
    r = client.get("/api/server/status", params={"server_id": "ec2-1", "live": "true"}, headers=AUTH)
    check("live check Lambda failure -> 200, not 500", r.status_code == 200, str(r.status_code))
    check("live check Lambda failure -> degrades to cached status, ssm_status None", r.json()["ssm_status"] is None, str(r.json()))

# =========================== HEARTBEAT ALERTING ===========================
# Auto-create fallback is disabled -- an algo must be registered via
# POST /api/algos before it can ever heartbeat.

with patch("trading.api.routes.alert_service") as mock_alerts:
    # heartbeat against an unregistered algo -> 404, no auto-create, no alert
    r = client.post("/api/heartbeat", json={"algo_id": "unregistered_algo", "server_id": "ec2-1", "status": "RUNNING"}, headers=AUTH)
    check("heartbeat for unregistered algo -> 404", r.status_code == 404, str(r.status_code) + " " + r.text)
    check("unregistered algo heartbeat -> no alert fired", not mock_alerts.method_calls, "")

r = client.post("/api/algos", json={"algo_id": "new_algo", "server_id": "ec2-1"}, headers=AUTH)
check("register new_algo -> 201", r.status_code == 201, str(r.status_code) + " " + r.text)

with patch("trading.api.routes.alert_service") as mock_alerts:
    # registered algo defaults to STOPPED; first heartbeat RUNNING -> strategy_recovered (STOPPED -> RUNNING)
    r = client.post("/api/heartbeat", json={"algo_id": "new_algo", "server_id": "ec2-1", "status": "RUNNING"}, headers=AUTH)
    check("first heartbeat RUNNING -> 200", r.status_code == 200, str(r.status_code))
    check("first heartbeat RUNNING -> strategy_recovered fired", mock_alerts.strategy_recovered.called, "")
    check("first heartbeat RUNNING -> no other alert fired", not mock_alerts.strategy_crashed.called and not mock_alerts.strategy_stopped.called, "")

with patch("trading.api.routes.alert_service") as mock_alerts:
    # same status again (RUNNING -> RUNNING) -> no alert (dedup on no-change)
    r = client.post("/api/heartbeat", json={"algo_id": "new_algo", "server_id": "ec2-1", "status": "RUNNING"}, headers=AUTH)
    check("unchanged status -> no alert fired", not mock_alerts.strategy_recovered.called, "")

with patch("trading.api.routes.alert_service") as mock_alerts:
    # RUNNING -> ERROR -> strategy_crashed
    r = client.post("/api/heartbeat", json={"algo_id": "new_algo", "server_id": "ec2-1", "status": "ERROR"}, headers=AUTH)
    check("RUNNING -> ERROR -> strategy_crashed fired", mock_alerts.strategy_crashed.called, "")

with patch("trading.api.routes.alert_service") as mock_alerts:
    # ERROR -> RUNNING -> strategy_recovered
    r = client.post("/api/heartbeat", json={"algo_id": "new_algo", "server_id": "ec2-1", "status": "RUNNING"}, headers=AUTH)
    check("ERROR -> RUNNING -> strategy_recovered fired", mock_alerts.strategy_recovered.called, "")

with patch("trading.api.routes.alert_service") as mock_alerts:
    # RUNNING -> STOPPED -> strategy_stopped
    r = client.post("/api/heartbeat", json={"algo_id": "new_algo", "server_id": "ec2-1", "status": "STOPPED"}, headers=AUTH)
    check("RUNNING -> STOPPED -> strategy_stopped fired", mock_alerts.strategy_stopped.called, "")

r = client.post("/api/algos", json={"algo_id": "another_new_algo", "server_id": "ec2-1"}, headers=AUTH)
check("register another_new_algo -> 201", r.status_code == 201, str(r.status_code) + " " + r.text)

with patch("trading.api.routes.alert_service") as mock_alerts:
    # a SECOND registered algo whose first-ever heartbeat is ERROR -> strategy_crashed ("Status changed to ERROR", not "Initial" -- that reason string was tied to the removed auto-create path)
    r = client.post("/api/heartbeat", json={"algo_id": "another_new_algo", "server_id": "ec2-1", "status": "ERROR"}, headers=AUTH)
    check("second registered algo, first heartbeat ERROR -> strategy_crashed", mock_alerts.strategy_crashed.called, "")
    call_kwargs = mock_alerts.strategy_crashed.call_args.kwargs
    check("ERROR alert reason mentions 'Status changed'", "Status changed" in call_kwargs.get("reason", ""), str(call_kwargs))

# =========================== RATE LIMITING (last -- deliberately exhausts the window) ===========================

db = SessionLocal()
db.query(models.RateLimitWindow).delete()  # clear counts from earlier sections' requests
db.commit()
db.close()

original_max = api_deps.RATE_LIMIT_MAX_REQUESTS
api_deps.RATE_LIMIT_MAX_REQUESTS = 5
try:
    statuses = []
    for _ in range(6):
        r = client.get("/api/servers", headers=AUTH)
        statuses.append(r.status_code)
    check("first 5 requests succeed, 6th is rate-limited", statuses == [200, 200, 200, 200, 200, 429], str(statuses))

    r = client.get("/api/servers", headers=AUTH)
    check("Retry-After header present on 429", "retry-after" in {k.lower() for k in r.headers.keys()}, str(dict(r.headers)))

    # invalid key -> 401 BEFORE rate limiting even queries the DB (auth runs first, cheaper failure)
    r = client.get("/api/servers", headers={"X-API-Key": "wrong"})
    check("invalid key -> 401, not counted against/blocked by rate limit", r.status_code == 401, str(r.status_code))
finally:
    api_deps.RATE_LIMIT_MAX_REQUESTS = original_max

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
