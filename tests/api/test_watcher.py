"""Stale-heartbeat watcher -- ported from trading/api/test_watcher_local.py."""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import trading.api.watcher as watcher
from trading.database import models

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def seed(db_session):
    def _make(algo_status="RUNNING", last_hb_age_min=None, algo_updated_age_min=120):
        srv = models.Server(name="srv-1", ec2_instance_id="i-1", region="ap-south-1", status="RUNNING")
        db_session.add(srv)
        db_session.flush()
        algo = models.Algo(
            name="algo-1", server_id=srv.id, script_path="trading/algos/x/main.py",
            status=algo_status, enabled=True,
        )
        algo.updated_at = NOW - timedelta(minutes=algo_updated_age_min)
        db_session.add(algo)
        db_session.flush()
        if last_hb_age_min is not None:
            db_session.add(models.Heartbeat(
                algo_id=algo.id, server_id=srv.id,
                timestamp=NOW - timedelta(minutes=last_hb_age_min), status=algo_status,
            ))
        db_session.commit()
        return srv, algo

    return _make


@pytest.fixture
def alerts():
    with patch.object(watcher, "alert_service") as m:
        yield m


def test_fresh_heartbeat_no_alert(seed, alerts, db_session):
    seed(algo_status="RUNNING", last_hb_age_min=0)
    assert watcher.check_stale_heartbeats_once(db_session, now=NOW) == []
    assert not alerts.heartbeat_missing.called


def test_stale_heartbeat_alerts(seed, alerts, db_session):
    seed(algo_status="RUNNING", last_hb_age_min=10)
    alerted = watcher.check_stale_heartbeats_once(db_session, now=NOW)
    assert len(alerted) == 1
    assert alerted[0][0] == "algo-1" and alerted[0][1] == "srv-1"
    assert 9.5 <= alerted[0][2] <= 10.5
    assert alerts.heartbeat_missing.call_count == 1
    call = alerts.heartbeat_missing.call_args
    assert call.args[:2] == ("algo-1", "srv-1")
    assert "minutes" in call.kwargs


def test_stopped_algo_never_alerted(seed, alerts, db_session):
    seed(algo_status="STOPPED", last_hb_age_min=999)
    assert watcher.check_stale_heartbeats_once(db_session, now=NOW) == []
    assert not alerts.heartbeat_missing.called


def test_no_heartbeat_falls_back_to_updated_at(seed, alerts, db_session):
    seed(algo_status="ERROR", last_hb_age_min=None, algo_updated_age_min=30)
    assert len(watcher.check_stale_heartbeats_once(db_session, now=NOW)) == 1


def test_no_heartbeat_fresh_updated_at_not_alerted(seed, alerts, db_session):
    seed(algo_status="RUNNING", last_hb_age_min=None, algo_updated_age_min=0)
    assert watcher.check_stale_heartbeats_once(db_session, now=NOW) == []


def test_scan_is_read_only(seed, db_session):
    seed(algo_status="RUNNING", last_hb_age_min=10)
    before = (
        db_session.query(models.Algo).count(),
        db_session.query(models.Heartbeat).count(),
        db_session.query(models.Algo).first().status,
    )
    with patch.object(watcher, "alert_service"):
        watcher.check_stale_heartbeats_once(db_session, now=NOW)
    db_session.expire_all()
    after = (
        db_session.query(models.Algo).count(),
        db_session.query(models.Heartbeat).count(),
        db_session.query(models.Algo).first().status,
    )
    assert before == after


@pytest.mark.parametrize("disabled, expected_scheduled", [(True, False), (False, True)])
def test_disable_background_watcher_flag(monkeypatch, disabled, expected_scheduled):
    from trading.api.app import create_app

    monkeypatch.setenv("DISABLE_BACKGROUND_WATCHER", "true" if disabled else "")
    app = create_app()

    def _fake_create_task(coro, *a, **k):
        coro.close()  # don't leave an un-awaited coroutine warning
        return MagicMock()

    with patch("trading.api.app.asyncio.create_task", side_effect=_fake_create_task) as ct:
        async def _cycle():
            async with app.router.lifespan_context(app):
                pass
        asyncio.run(_cycle())
    assert ct.called is expected_scheduled


def test_env_vars_preserved():
    assert watcher.STALE_THRESHOLD_MINUTES == 2.0
    assert watcher.STALE_CHECK_INTERVAL_SECONDS == 60


@pytest.mark.parametrize("forbidden", [
    "place_order", "cancel_order", "square_off", "stop_algo", "trading_agent", "create_broker",
])
def test_watcher_has_no_trading_side_effects(forbidden):
    assert forbidden not in inspect.getsource(watcher)
