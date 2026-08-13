"""
Local test harness for the control-center API, using FastAPI's TestClient
against the real app (backend.main.app) with SQLite + a mocked Lambda
client. Not a permanent part of the deployment.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///test_api.db"
os.environ["CONTROL_API_KEY"] = "test-key-123"
os.environ["DISABLE_BACKGROUND_WATCHER"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402
from trading.database import models  # noqa: E402
from trading.database.connection import SessionLocal, init_db  # noqa: E402

failures = []


def check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    print(f"[{status_}] {name} {detail}")
    if not condition:
        failures.append(name)


init_db()

# seed a server row (algo/server registration isn't in this milestone's
# scope -- just need one to test against)
db = SessionLocal()
db.query(models.Command).delete()
db.query(models.Algo).delete()
db.query(models.Server).delete()
db.commit()
server = models.Server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING")
db.add(server)
db.commit()
db.close()

client = TestClient(app)
AUTH = {"X-API-Key": "test-key-123"}

# --- auth enforcement ---
r = client.post("/api/algo/start", json={"algo_id": "example_strategy", "server_id": "ec2-1"})
check("no API key -> 401", r.status_code == 401, str(r.status_code))

r = client.post(
    "/api/algo/start",
    json={"algo_id": "example_strategy", "server_id": "ec2-1"},
    headers={"X-API-Key": "wrong-key"},
)
check("wrong API key -> 401", r.status_code == 401, str(r.status_code))

# --- unknown server ---
r = client.post("/api/algo/start", json={"algo_id": "x", "server_id": "nonexistent"}, headers=AUTH)
check("unknown server -> 404", r.status_code == 404, str(r.status_code))

# --- start_algo happy path (mocked Lambda) ---
with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    mock_invoke.return_value = {
        "success": True, "job_id": "cmd-abc", "server_id": "i-test",
        "algo_id": "example_strategy", "status": "STARTING",
    }
    r = client.post(
        "/api/algo/start",
        json={"algo_id": "example_strategy", "server_id": "ec2-1", "requested_by": "test-user"},
        headers=AUTH,
    )
    check("start_algo -> 200", r.status_code == 200, str(r.status_code) + " " + r.text)
    body = r.json()
    check(
        "start_algo response shape",
        body["success"] is True and body["job_id"] == "cmd-abc" and body["status"] == "STARTING" and body["command_id"] is not None,
        str(body),
    )
    check(
        "invoke_orchestrator called with correct action/algo_id",
        mock_invoke.call_args.args[0] == "start_algo" and mock_invoke.call_args.kwargs.get("algo_id") == "example_strategy",
        str(mock_invoke.call_args),
    )
    command_id = body["command_id"]

# verify a Command audit row was actually created in the DB
db = SessionLocal()
cmd_row = db.query(models.Command).filter(models.Command.id == command_id).one_or_none()
check(
    "Command row persisted with job_id + PENDING->STARTING status",
    cmd_row is not None and cmd_row.job_id == "cmd-abc" and cmd_row.status == "STARTING" and cmd_row.requested_by == "test-user",
    str(cmd_row.__dict__ if cmd_row else None),
)
db.close()

# --- start_algo: Lambda invoke fails ---
with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    from trading.api.lambda_client import LambdaInvokeError
    mock_invoke.side_effect = LambdaInvokeError("boom")
    r = client.post(
        "/api/algo/start",
        json={"algo_id": "example_strategy", "server_id": "ec2-1"},
        headers=AUTH,
    )
    check("start_algo Lambda failure -> 200 with success:false (not a 500)", r.status_code == 200, str(r.status_code))
    check("start_algo Lambda failure -> status FAILED", r.json()["status"] == "FAILED", str(r.json()))

# --- get_command_status: still in progress ---
with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    mock_invoke.return_value = {"job_id": "cmd-abc", "ssm_status": "InProgress", "status": "IN_PROGRESS"}
    r = client.get(f"/api/command/{command_id}", headers=AUTH)
    check("get_command still in progress -> 200", r.status_code == 200, str(r.status_code))
    check("get_command still in progress -> status unchanged (STARTING)", r.json()["status"] == "STARTING", str(r.json()))

# --- get_command_status: resolves to RUNNING ---
with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
    mock_invoke.return_value = {
        "success": True, "job_id": "cmd-abc", "ssm_status": "Success",
        "algo": "example_strategy", "status": "RUNNING", "pid": 4242,
    }
    r = client.get(f"/api/command/{command_id}", headers=AUTH)
    check("get_command resolves -> status RUNNING", r.json()["status"] == "RUNNING", str(r.json()))

db = SessionLocal()
cmd_row = db.query(models.Command).filter(models.Command.id == command_id).one_or_none()
check("Command row updated to RUNNING in DB", cmd_row.status == "RUNNING", cmd_row.status)
db.close()

# --- unknown command_id ---
r = client.get("/api/command/999999", headers=AUTH)
check("unknown command_id -> 404", r.status_code == 404, str(r.status_code))

# --- server/status ---
r = client.get("/api/server/status", params={"server_id": "ec2-1"}, headers=AUTH)
check("server/status -> 200 with correct data", r.status_code == 200 and r.json()["ec2_instance_id"] == "i-test", str(r.json()))

# --- logs/pnl/positions: empty but well-formed (nothing ingested yet, expected at this milestone) ---
r = client.get("/api/logs", params={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=AUTH)
check("logs -> 200, empty list", r.status_code == 200 and r.json() == [], str(r.json()))

r = client.get("/api/pnl", params={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=AUTH)
check("pnl -> 200, empty list", r.status_code == 200 and r.json() == [], str(r.json()))

r = client.get("/api/positions", params={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=AUTH)
check("positions -> 200, empty list", r.status_code == 200 and r.json() == [], str(r.json()))

# --- existing old endpoint still works untouched ---
r = client.get("/health")
check("old /health endpoint still works, no auth required", r.status_code == 200, str(r.status_code))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
