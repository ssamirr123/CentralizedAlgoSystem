"""
Backward-compatibility checks for the legacy monitoring endpoints after
their move into trading/api/legacy.py (Stage 5).

Locks down the contract the existing Streamlit dashboard and the
strategy_agent heartbeat client depend on:
    GET  /health
    POST /update_strategy
    GET  /strategies

Request shape, response shape, status codes, DB writes, and Telegram
alert calls must all be unchanged. Isolated SQLite DB, mocked alert
service, no network. Runs the same way as the other test_*_local.py
scripts; folds into the tests/ tree in Stage 8.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_DB = Path(tempfile.gettempdir()) / "test_legacy_compat.db"
_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["CONTROL_API_KEY"] = "legacy-compat-key"
os.environ["DISABLE_BACKGROUND_WATCHER"] = "true"
os.environ["DAY_LOSS_LIMIT"] = "10000.0"
os.environ.setdefault("TRADING_MODE", "paper")

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402  (import-stable entrypoint)
from trading.database.connection import SessionLocal  # noqa: E402
from trading.database.models import StrategyHeartbeat  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        failures.append(name)


def _hb(**over):
    body = {
        "strategy_name": "compat_s",
        "server_name": "compat_srv",
        "status": "RUNNING",
        "current_mtm": 100.5,
        "day_pnl": 250.75,
        "number_of_trades": 4,
        "last_update_time": "2026-08-28T10:00:00Z",
    }
    body.update(over)
    return body


with patch("trading.api.legacy.alert_service") as alerts:
    with TestClient(app) as client:
        # ---- GET /health ------------------------------------------------
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check(
            "health body shape unchanged",
            set(body) == {"status", "timestamp_utc", "service"}
            and body["status"] == "ok"
            and body["service"] == "central-strategy-monitor",
            str(body),
        )

        # ---- POST /update_strategy : create ---------------------------
        r = client.post("/update_strategy", json=_hb())
        check("POST /update_strategy (new) -> 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check(
            "update_strategy response shape unchanged",
            set(body) == {
                "strategy_name", "server_name", "status", "current_mtm", "day_pnl",
                "number_of_trades", "last_update_time", "received_at",
            },
            str(sorted(body)),
        )
        check("response echoes status RUNNING", body["status"] == "RUNNING", body["status"])
        check("first RUNNING fires strategy_started",
              alerts.strategy_started.called, str(alerts.strategy_started.call_args))

        db = SessionLocal()
        rows = db.query(StrategyHeartbeat).all()
        check("exactly one row persisted", len(rows) == 1, str(len(rows)))
        check("row matches payload",
              rows[0].strategy_name == "compat_s" and rows[0].server_name == "compat_srv"
              and rows[0].current_mtm == 100.5 and rows[0].number_of_trades == 4,
              "")
        db.close()

        # ---- POST /update_strategy : upsert same pair, transition ----
        alerts.reset_mock()
        r = client.post("/update_strategy", json=_hb(status="STOPPED", day_pnl=10.0))
        check("POST /update_strategy (update) -> 200", r.status_code == 200, str(r.status_code))
        db = SessionLocal()
        rows = db.query(StrategyHeartbeat).all()
        check("still exactly one row (upsert, not insert)", len(rows) == 1, str(len(rows)))
        check("row status updated to STOPPED", rows[0].status == "STOPPED", rows[0].status)
        db.close()
        check("RUNNING->STOPPED fires strategy_stopped",
              alerts.strategy_stopped.called, str(alerts.strategy_stopped.call_args))

        alerts.reset_mock()
        client.post("/update_strategy", json=_hb(status="RUNNING"))
        check("STOPPED->RUNNING fires strategy_recovered", alerts.strategy_recovered.called)

        alerts.reset_mock()
        client.post("/update_strategy", json=_hb(status="ERROR"))
        check("RUNNING->ERROR fires strategy_crashed", alerts.strategy_crashed.called)

        # ---- day-loss alert -----------------------------------------
        alerts.reset_mock()
        client.post("/update_strategy", json=_hb(status="RUNNING", day_pnl=-25000.0))
        check("day_pnl beyond DAY_LOSS_LIMIT fires day_loss_exceeded",
              alerts.day_loss_exceeded.called, str(alerts.day_loss_exceeded.call_args))

        # ---- validation (status codes unchanged) --------------------
        r = client.post("/update_strategy", json=_hb(number_of_trades=-1))
        check("negative number_of_trades -> 422", r.status_code == 422, str(r.status_code))
        r = client.post("/update_strategy", json=_hb(status="BOGUS"))
        check("invalid status enum -> 422", r.status_code == 422, str(r.status_code))
        r = client.post("/update_strategy", json={"strategy_name": "x"})
        check("missing required fields -> 422", r.status_code == 422, str(r.status_code))

        # ---- GET /strategies --------------------------------------
        client.post("/update_strategy", json=_hb(strategy_name="compat_s2", status="RUNNING"))
        r = client.get("/strategies")
        check("GET /strategies -> 200", r.status_code == 200, str(r.status_code))
        data = r.json()
        check("strategies returns a list", isinstance(data, list) and len(data) == 2, str(len(data)))
        check(
            "each strategy item shape unchanged",
            all(
                set(item) == {
                    "strategy_name", "server_name", "status", "current_mtm", "day_pnl",
                    "number_of_trades", "last_update_time", "received_at",
                }
                for item in data
            ),
            "",
        )
        check(
            "ordered by received_at desc",
            data[0]["received_at"] >= data[1]["received_at"],
            f'{data[0]["received_at"]} >= {data[1]["received_at"]}',
        )

        # ---- /update_strategy and /strategies require NO auth --------
        r = client.get("/strategies")  # no X-API-Key header
        check("GET /strategies needs no API key", r.status_code == 200, str(r.status_code))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All legacy compatibility checks passed.")
