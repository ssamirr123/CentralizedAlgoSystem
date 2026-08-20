"""
Local test for POST /api/heartbeat and GET /api/algos' last_heartbeat
enrichment. Not a permanent part of the deployment.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///test_heartbeat.db"
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
db = SessionLocal()
db.query(models.Heartbeat).delete()
db.query(models.Algo).delete()
db.query(models.Server).delete()
db.commit()
server = models.Server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING")
db.add(server)
db.commit()
db.close()

client = TestClient(app)
AUTH = {"X-API-Key": "test-key-123"}

# --- heartbeat requires auth like everything else on this router ---
r = client.post("/api/heartbeat", json={"algo_id": "x", "server_id": "ec2-1", "status": "RUNNING"})
check("heartbeat no auth -> 401", r.status_code == 401, str(r.status_code))

# --- heartbeat for unknown server -> 404 ---
r = client.post(
    "/api/heartbeat",
    json={"algo_id": "x", "server_id": "nonexistent", "status": "RUNNING"},
    headers=AUTH,
)
check("heartbeat unknown server -> 404", r.status_code == 404, str(r.status_code))

# --- heartbeat for an unregistered algo -> 404 (auto-create fallback disabled) ---
r = client.post(
    "/api/heartbeat",
    json={"algo_id": "example_strategy", "server_id": "ec2-1", "status": "RUNNING"},
    headers=AUTH,
)
check("heartbeat unregistered algo -> 404", r.status_code == 404, str(r.status_code))

# --- register the algo, then heartbeat succeeds against it ---
r = client.post("/api/algos", json={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=AUTH)
check("register example_strategy -> 201", r.status_code == 201, str(r.status_code) + " " + r.text)

r = client.post(
    "/api/heartbeat",
    json={
        "algo_id": "example_strategy", "server_id": "ec2-1", "status": "RUNNING",
        "cpu": 12.5, "memory": 8.2, "pnl": 1500.0, "position": "SHORT_STRADDLE",
    },
    headers=AUTH,
)
check("heartbeat -> 200", r.status_code == 200, str(r.status_code) + " " + r.text)
check("heartbeat response shape", r.json() == {"success": True, "algo_id": "example_strategy", "server_id": "ec2-1"}, str(r.json()))

# --- verify DB side effects: Heartbeat row, algo.status, server.last_heartbeat ---
db = SessionLocal()
algo = db.query(models.Algo).filter(models.Algo.name == "example_strategy").one()
check("algo.status updated to RUNNING", algo.status == "RUNNING", algo.status)

hb_rows = db.query(models.Heartbeat).filter(models.Heartbeat.algo_id == algo.id).all()
check("heartbeat row inserted with correct fields", (
    len(hb_rows) == 1 and hb_rows[0].cpu == 12.5 and hb_rows[0].memory == 8.2
    and hb_rows[0].pnl == 1500.0 and hb_rows[0].position == "SHORT_STRADDLE"
), str(hb_rows[0].__dict__) if hb_rows else "no rows")

server_row = db.query(models.Server).filter(models.Server.name == "ec2-1").one()
check("server.last_heartbeat updated", server_row.last_heartbeat is not None, str(server_row.last_heartbeat))
check("server.status untouched by algo heartbeat (still RUNNING from setup, not re-derived)", server_row.status == "RUNNING", server_row.status)
db.close()

# --- GET /api/algos now includes last_heartbeat ---
r = client.get("/api/algos", headers=AUTH)
check("algos list -> 200", r.status_code == 200, str(r.status_code))
algos = r.json()
check("algos list has 1 entry", len(algos) == 1, str(algos))
check("algos list entry has last_heartbeat populated", algos[0]["last_heartbeat"] is not None, str(algos[0]))

# --- second heartbeat with different status updates algo.status again ---
time.sleep(0.05)
r = client.post(
    "/api/heartbeat",
    json={"algo_id": "example_strategy", "server_id": "ec2-1", "status": "ERROR"},
    headers=AUTH,
)
check("second heartbeat -> 200", r.status_code == 200, str(r.status_code))

db = SessionLocal()
hb_count = db.query(models.Heartbeat).filter(models.Heartbeat.algo_id == algo.id).count()
check("second heartbeat appended (history, not upserted)", hb_count == 2, hb_count)
algo2 = db.query(models.Algo).filter(models.Algo.name == "example_strategy").one()
check("algo.status updated to ERROR on second heartbeat", algo2.status == "ERROR", algo2.status)
db.close()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
