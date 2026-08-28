"""API-test helpers: seed a server / algo row directly in the DB."""
from __future__ import annotations

import pytest

from trading.database import models


@pytest.fixture
def seed_server(db_session):
    def _make(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING", **kw):
        srv = models.Server(
            name=name, ec2_instance_id=ec2_instance_id, region=region, status=status, **kw
        )
        db_session.add(srv)
        db_session.commit()
        db_session.refresh(srv)
        return srv

    return _make


@pytest.fixture
def seed_algo(db_session):
    def _make(server, name="example_strategy", status="STOPPED", enabled=True, script_path=None):
        algo = models.Algo(
            name=name,
            server_id=server.id,
            script_path=script_path or f"trading/algos/{name}/main.py",
            status=status,
            enabled=enabled,
        )
        db_session.add(algo)
        db_session.commit()
        db_session.refresh(algo)
        return algo

    return _make
