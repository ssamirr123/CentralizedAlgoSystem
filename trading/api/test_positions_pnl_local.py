"""Local test for POST/GET /api/positions, /api/trades, /api/pnl."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["DATABASE_URL"] = "sqlite:///test_pos_pnl.db"
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
db.query(models.Trade).delete()
db.query(models.Position).delete()
db.query(models.DailyPnl).delete()
db.query(models.Algo).delete()
db.query(models.Server).delete()
db.commit()
server = models.Server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING")
db.add(server)
db.commit()
db.close()

client = TestClient(app)
AUTH = {"X-API-Key": "test-key-123"}

# =========================== POSITIONS ===========================

r = client.post("/api/positions", json={"algo_id": "a1", "server_id": "ec2-1", "symbol": "NIFTY", "quantity": 50, "average_price": 24700.0})
check("post position no auth would 401", client.post("/api/positions", json={"algo_id":"a","server_id":"ec2-1","symbol":"X","quantity":1,"average_price":1.0}).status_code == 401)

r = client.post(
    "/api/positions",
    json={"algo_id": "a1", "server_id": "ec2-1", "symbol": "NIFTY", "quantity": 50, "average_price": 24700.0, "last_price": 24750.0, "pnl": 2500.0},
    headers=AUTH,
)
check("post position -> 200, not closed", r.status_code == 200 and r.json() == {"success": True, "closed": False}, str(r.json()))

r = client.get("/api/positions", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
rows = r.json()
check("get positions -> 1 row with correct fields", (
    len(rows) == 1 and rows[0]["symbol"] == "NIFTY" and rows[0]["quantity"] == 50
    and rows[0]["last_price"] == 24750.0 and rows[0]["pnl"] == 2500.0
), str(rows))

# same symbol again -> UPDATES the existing row (upsert), not a new one
r = client.post(
    "/api/positions",
    json={"algo_id": "a1", "server_id": "ec2-1", "symbol": "NIFTY", "quantity": 75, "average_price": 24710.0, "last_price": 24800.0, "pnl": 6750.0},
    headers=AUTH,
)
r = client.get("/api/positions", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
rows = r.json()
check("position upsert -> still 1 row, updated quantity", len(rows) == 1 and rows[0]["quantity"] == 75, str(rows))

# quantity=0 -> closes (deletes) the position
r = client.post(
    "/api/positions",
    json={"algo_id": "a1", "server_id": "ec2-1", "symbol": "NIFTY", "quantity": 0, "average_price": 24710.0},
    headers=AUTH,
)
check("closing position -> closed:True", r.json() == {"success": True, "closed": True}, str(r.json()))

r = client.get("/api/positions", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
check("closed position -> no rows (deleted, not zero-quantity row)", r.json() == [], str(r.json()))

# =========================== TRADES ===========================

for i, side in enumerate(["BUY", "SELL"]):
    r = client.post(
        "/api/trades",
        json={"algo_id": "a1", "server_id": "ec2-1", "symbol": "NIFTY", "side": side, "quantity": 50, "price": 24700.0 + i, "order_id": f"PAPER-{i}"},
        headers=AUTH,
    )
    check(f"post trade {side} -> 200", r.status_code == 200 and r.json() == {"success": True}, str(r.json()))

r = client.get("/api/trades", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
rows = r.json()
check("get trades -> 2 entries, both persisted (insert-only history)", len(rows) == 2, str(rows))
check("trades have correct sides", {t["side"] for t in rows} == {"BUY", "SELL"}, str(rows))

# a 3rd trade with the SAME symbol should NOT overwrite -- history keeps growing
r = client.post(
    "/api/trades",
    json={"algo_id": "a1", "server_id": "ec2-1", "symbol": "NIFTY", "side": "BUY", "quantity": 25, "price": 24705.0},
    headers=AUTH,
)
r = client.get("/api/trades", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
check("3rd trade -> 3 total (never overwrites)", len(r.json()) == 3, str(r.json()))

# =========================== DAILY PNL ===========================

r = client.post(
    "/api/pnl",
    json={"algo_id": "a1", "server_id": "ec2-1", "pnl": 1500.0, "trade_count": 3, "pnl_date": "2026-08-12"},
    headers=AUTH,
)
check("post pnl -> 200", r.status_code == 200, str(r.status_code) + " " + r.text)

r = client.get("/api/pnl", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
rows = r.json()
check("get pnl -> 1 entry", len(rows) == 1 and rows[0]["pnl"] == 1500.0 and rows[0]["trade_count"] == 3, str(rows))

# same date again -> upserts, doesn't duplicate
r = client.post(
    "/api/pnl",
    json={"algo_id": "a1", "server_id": "ec2-1", "pnl": 2200.0, "trade_count": 4, "pnl_date": "2026-08-12"},
    headers=AUTH,
)
r = client.get("/api/pnl", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
rows = r.json()
check("pnl same date -> still 1 entry, updated value (upsert)", len(rows) == 1 and rows[0]["pnl"] == 2200.0, str(rows))

# different date -> separate row
r = client.post(
    "/api/pnl",
    json={"algo_id": "a1", "server_id": "ec2-1", "pnl": 500.0, "trade_count": 1, "pnl_date": "2026-08-13"},
    headers=AUTH,
)
r = client.get("/api/pnl", params={"algo_id": "a1", "server_id": "ec2-1"}, headers=AUTH)
rows = r.json()
check("pnl different date -> 2 entries total", len(rows) == 2, str(rows))

# omit date -> defaults to today
r = client.post("/api/pnl", json={"algo_id": "a1", "server_id": "ec2-1", "pnl": 999.0, "trade_count": 1}, headers=AUTH)
check("pnl without date -> 200 (defaults to today)", r.status_code == 200, str(r.status_code))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
