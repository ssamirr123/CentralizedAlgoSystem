"""Local test for POST /api/logs and GET /api/logs filtering."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///test_logs.db"
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
db.query(models.Log).delete()
db.query(models.Algo).delete()
db.query(models.Server).delete()
db.commit()
server = models.Server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING")
db.add(server)
db.commit()
db.close()

client = TestClient(app)
AUTH = {"X-API-Key": "test-key-123"}

# --- no auth -> 401 ---
r = client.post("/api/logs", json={"algo_id": "x", "server_id": "ec2-1", "level": "INFO", "event": "ENTRY"})
check("post log no auth -> 401", r.status_code == 401, str(r.status_code))

# --- unknown server -> 404 ---
r = client.post(
    "/api/logs",
    json={"algo_id": "x", "server_id": "nope", "level": "INFO", "event": "ENTRY"},
    headers=AUTH,
)
check("post log unknown server -> 404", r.status_code == 404, str(r.status_code))

# --- insert 3 logs: ENTRY(INFO), SL(WARNING), EXIT(INFO), on two different days ---
events = [
    {"algo_id": "algo1", "server_id": "ec2-1", "level": "INFO", "event": "ENTRY",
     "details": {"strike": 24750}, "timestamp": "2026-08-10T10:00:00+00:00"},
    {"algo_id": "algo1", "server_id": "ec2-1", "level": "WARNING", "event": "SL",
     "details": {"reason": "stop loss hit"}, "timestamp": "2026-08-10T10:15:00+00:00"},
    {"algo_id": "algo1", "server_id": "ec2-1", "level": "INFO", "event": "EXIT",
     "details": {}, "timestamp": "2026-08-11T09:00:00+00:00"},
]
for e in events:
    r = client.post("/api/logs", json=e, headers=AUTH)
    check(f"post log {e['event']} -> 200", r.status_code == 200, str(r.status_code) + " " + r.text)

# --- GET all logs for algo1 ---
r = client.get("/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1"}, headers=AUTH)
check("get all logs -> 3 entries", len(r.json()) == 3, str(r.json()))

# --- filter by level=WARNING ---
r = client.get("/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", "level": "WARNING"}, headers=AUTH)
rows = r.json()
check("filter level=WARNING -> 1 entry (SL)", len(rows) == 1 and rows[0]["event"] == "SL", str(rows))

# --- filter by event=ENTRY ---
r = client.get("/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", "event": "ENTRY"}, headers=AUTH)
rows = r.json()
check("filter event=ENTRY -> 1 entry", len(rows) == 1 and rows[0]["event"] == "ENTRY", str(rows))

# --- filter by log_date=2026-08-10 -> 2 entries (ENTRY + SL) ---
r = client.get(
    "/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", "log_date": "2026-08-10"}, headers=AUTH,
)
rows = r.json()
check(
    "filter log_date=2026-08-10 -> 2 entries",
    len(rows) == 2 and {row["event"] for row in rows} == {"ENTRY", "SL"},
    str(rows),
)

# --- filter by log_date=2026-08-11 -> 1 entry (EXIT) ---
r = client.get(
    "/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", "log_date": "2026-08-11"}, headers=AUTH,
)
rows = r.json()
check("filter log_date=2026-08-11 -> 1 entry (EXIT)", len(rows) == 1 and rows[0]["event"] == "EXIT", str(rows))

# --- combined filter: level=INFO AND log_date=2026-08-10 -> 1 entry (ENTRY, not SL since SL is WARNING) ---
r = client.get(
    "/api/logs",
    params={"algo_id": "algo1", "server_id": "ec2-1", "level": "INFO", "log_date": "2026-08-10"},
    headers=AUTH,
)
rows = r.json()
check("combined level+date filter -> 1 entry (ENTRY)", len(rows) == 1 and rows[0]["event"] == "ENTRY", str(rows))

# --- bad date format -> 400 ---
r = client.get(
    "/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", "log_date": "not-a-date"}, headers=AUTH,
)
check("bad log_date -> 400", r.status_code == 400, str(r.status_code))

# --- details JSON round-trips correctly ---
r = client.get("/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", "event": "ENTRY"}, headers=AUTH)
check("details JSON round-trips", r.json()[0]["details"] == {"strike": 24750}, str(r.json()[0]))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
