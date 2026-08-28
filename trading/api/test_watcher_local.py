"""
Tests for the stale-heartbeat watcher (Stage 7) after its migration to the
canonical control-center model (algos + heartbeats).

Covers: fresh heartbeat, stale heartbeat, alert generation, STOPPED algo
exclusion, no-heartbeat fallback, read-only guarantee, and the
DISABLE_BACKGROUND_WATCHER lifespan behaviour.

Isolated SQLite DB, mocked alert service, no network.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_DB = Path(tempfile.gettempdir()) / "test_watcher.db"
_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["CONTROL_API_KEY"] = "watcher-test-key"
os.environ.setdefault("TRADING_MODE", "paper")
# Keep defaults explicit so the test is independent of the shell env.
os.environ["STALE_THRESHOLD_MINUTES"] = "2"
os.environ["STALE_CHECK_INTERVAL_SECONDS"] = "60"

from trading.database.connection import SessionLocal, init_db  # noqa: E402
from trading.database import models  # noqa: E402
import trading.api.watcher as watcher  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        failures.append(name)


NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def _reset_db():
    for m in (models.Heartbeat, models.Algo, models.Server):
        pass
    init_db()
    db = SessionLocal()
    for m in (models.Heartbeat, models.Algo, models.Server):
        db.query(m).delete()
    db.commit()
    db.close()


def _seed(algo_status="RUNNING", last_hb_age_min=None, algo_updated_age_min=120):
    db = SessionLocal()
    srv = models.Server(name="srv-1", ec2_instance_id="i-1", region="ap-south-1", status="RUNNING")
    db.add(srv)
    db.flush()
    algo = models.Algo(
        name="algo-1", server_id=srv.id, script_path="trading/algos/x/main.py",
        status=algo_status, enabled=True,
    )
    algo.updated_at = NOW - timedelta(minutes=algo_updated_age_min)
    db.add(algo)
    db.flush()
    if last_hb_age_min is not None:
        db.add(models.Heartbeat(
            algo_id=algo.id, server_id=srv.id,
            timestamp=NOW - timedelta(minutes=last_hb_age_min), status=algo_status,
        ))
    db.commit()
    db.close()


init_db()

# ---- 1. fresh heartbeat -> no alert -----------------------------------
_reset_db(); _seed(algo_status="RUNNING", last_hb_age_min=0)
with patch.object(watcher, "alert_service") as al:
    db = SessionLocal()
    alerted = watcher.check_stale_heartbeats_once(db, now=NOW)
    db.close()
check("fresh heartbeat -> nothing alerted", alerted == [], str(alerted))
check("fresh heartbeat -> heartbeat_missing NOT called", not al.heartbeat_missing.called)

# ---- 2. stale heartbeat -> alert -----------------------------------
_reset_db(); _seed(algo_status="RUNNING", last_hb_age_min=10)
with patch.object(watcher, "alert_service") as al:
    db = SessionLocal()
    alerted = watcher.check_stale_heartbeats_once(db, now=NOW)
    db.close()
check("stale heartbeat -> one entry alerted", len(alerted) == 1, str(alerted))
check("stale heartbeat -> (algo, server) correct",
      alerted and alerted[0][0] == "algo-1" and alerted[0][1] == "srv-1", str(alerted))
check("stale heartbeat -> ~10 min silent", alerted and 9.5 <= alerted[0][2] <= 10.5, str(alerted))
check("alert generation: heartbeat_missing called once", al.heartbeat_missing.call_count == 1,
      str(al.heartbeat_missing.call_args))
check("alert uses the existing signature (name, server, minutes=)",
      al.heartbeat_missing.call_args is not None
      and al.heartbeat_missing.call_args.args[:2] == ("algo-1", "srv-1")
      and "minutes" in al.heartbeat_missing.call_args.kwargs,
      str(al.heartbeat_missing.call_args))

# ---- 3. STOPPED algo -> never alerted (even if stale) ---------------
_reset_db(); _seed(algo_status="STOPPED", last_hb_age_min=999)
with patch.object(watcher, "alert_service") as al:
    db = SessionLocal()
    alerted = watcher.check_stale_heartbeats_once(db, now=NOW)
    db.close()
check("STOPPED algo -> not alerted", alerted == [] and not al.heartbeat_missing.called, str(alerted))

# ---- 4. no heartbeat row -> falls back to algo.updated_at ----------
_reset_db(); _seed(algo_status="ERROR", last_hb_age_min=None, algo_updated_age_min=30)
with patch.object(watcher, "alert_service") as al:
    db = SessionLocal()
    alerted = watcher.check_stale_heartbeats_once(db, now=NOW)
    db.close()
check("no heartbeat + old updated_at + non-STOPPED -> alerted", len(alerted) == 1, str(alerted))

_reset_db(); _seed(algo_status="RUNNING", last_hb_age_min=None, algo_updated_age_min=0)
with patch.object(watcher, "alert_service") as al:
    db = SessionLocal()
    alerted = watcher.check_stale_heartbeats_once(db, now=NOW)
    db.close()
check("no heartbeat + fresh updated_at -> not alerted", alerted == [], str(alerted))

# ---- 5. read-only: a scan writes nothing --------------------------
_reset_db(); _seed(algo_status="RUNNING", last_hb_age_min=10)
db = SessionLocal()
before = (
    db.query(models.Algo).count(),
    db.query(models.Heartbeat).count(),
    db.query(models.Algo).first().status,
)
db.close()
with patch.object(watcher, "alert_service"):
    db = SessionLocal()
    watcher.check_stale_heartbeats_once(db, now=NOW)
    db.close()
db = SessionLocal()
after = (
    db.query(models.Algo).count(),
    db.query(models.Heartbeat).count(),
    db.query(models.Algo).first().status,
)
db.close()
check("read-only: algo/heartbeat counts and algo.status unchanged", before == after,
      f"{before} -> {after}")

# ---- 6. DISABLE_BACKGROUND_WATCHER controls scheduling ------------
def _run_lifespan_and_capture_create_task(disabled: bool) -> bool:
    from trading.api.app import create_app
    prev = os.environ.get("DISABLE_BACKGROUND_WATCHER")
    os.environ["DISABLE_BACKGROUND_WATCHER"] = "true" if disabled else ""
    try:
        app = create_app()
        fake_task = MagicMock()
        with patch("trading.api.app.asyncio.create_task", return_value=fake_task) as ct:
            async def _cycle():
                async with app.router.lifespan_context(app):
                    pass
            asyncio.run(_cycle())
            return ct.called
    finally:
        if prev is None:
            os.environ.pop("DISABLE_BACKGROUND_WATCHER", None)
        else:
            os.environ["DISABLE_BACKGROUND_WATCHER"] = prev

check("watcher disabled -> not scheduled", _run_lifespan_and_capture_create_task(disabled=True) is False)
check("watcher enabled -> scheduled", _run_lifespan_and_capture_create_task(disabled=False) is True)

# ---- 7. env vars preserved --------------------------------------
check("STALE_THRESHOLD_MINUTES honored", watcher.STALE_THRESHOLD_MINUTES == 2.0,
      str(watcher.STALE_THRESHOLD_MINUTES))
check("STALE_CHECK_INTERVAL_SECONDS honored", watcher.STALE_CHECK_INTERVAL_SECONDS == 60,
      str(watcher.STALE_CHECK_INTERVAL_SECONDS))

# ---- 8. no trading side-effect imports ------------------------
import inspect  # noqa: E402
src = inspect.getsource(watcher)
for forbidden in ("place_order", "cancel_order", "square_off", "stop_algo", "trading_agent", "create_broker"):
    check(f"watcher source does not reference {forbidden}", forbidden not in src)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All stale-heartbeat watcher checks passed.")
