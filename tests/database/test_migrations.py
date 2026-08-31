"""Alembic baseline == create_all() (Stage 3), on fresh isolated DBs."""
from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

_PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def _snapshot(url: str) -> dict:
    eng = create_engine(url)
    insp = inspect(eng)
    snap = {}
    for t in sorted(x for x in insp.get_table_names() if x != "alembic_version"):
        snap[t] = {
            "columns": {c["name"]: (str(c["type"]), bool(c["nullable"])) for c in insp.get_columns(t)},
            "pk": sorted(insp.get_pk_constraint(t).get("constrained_columns") or []),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(t)
            ),
            "unique": sorted(
                (u.get("name"), tuple(sorted(u["column_names"]))) for u in insp.get_unique_constraints(t)
            ),
            "fks": sorted(
                (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                for f in insp.get_foreign_keys(t)
            ),
        }
    eng.dispose()
    return snap


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_PROJECT_ROOT, env=env, capture_output=True, text=True,
    )


def test_baseline_matches_create_all(tmp_path):
    ca_url = f"sqlite:///{tmp_path / 'create_all.db'}"
    al_url = f"sqlite:///{tmp_path / 'alembic.db'}"

    from trading.database.connection import Base
    from trading.database import models  # noqa: F401
    Base.metadata.create_all(bind=create_engine(ca_url))

    r = _alembic(al_url, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    assert _snapshot(ca_url) == _snapshot(al_url)


def test_alembic_check_reports_no_drift(tmp_path):
    url = f"sqlite:///{tmp_path / 'chk.db'}"
    assert _alembic(url, "upgrade", "head").returncode == 0
    r = _alembic(url, "check")
    assert r.returncode == 0, f"drift detected:\n{r.stdout}\n{r.stderr}"


def test_stamp_head_on_create_all_db_is_clean(tmp_path):
    url = f"sqlite:///{tmp_path / 'stamp.db'}"
    from trading.database.connection import Base
    from trading.database import models  # noqa: F401
    Base.metadata.create_all(bind=create_engine(url))

    assert _alembic(url, "stamp", "head").returncode == 0
    assert _alembic(url, "check").returncode == 0


def test_downgrade_upgrade_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path / 'rt.db'}"
    assert _alembic(url, "upgrade", "head").returncode == 0
    assert _alembic(url, "downgrade", "base").returncode == 0
    assert _alembic(url, "upgrade", "head").returncode == 0
