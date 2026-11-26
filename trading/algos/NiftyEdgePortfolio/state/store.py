"""
Portfolio state, persisted atomically after every change.

Unlike DoubleStraddelAlgo's day-scoped state.json, this state is MULTI-DAY:
overnight legs (OTM4, BTST 9, ST10) and indicator state (ST10 Supertrend)
must survive the nightly restart.
"""
from __future__ import annotations

import json
import os
import threading

import config
from context import Leg

_lock = threading.Lock()


def _fresh():
    return {"legs": [], "sstates": {}, "deferred_exits": [], "equity": {"cum": 0.0, "peak": 0.0}, "version": 1}


def load() -> dict:
    os.makedirs(config.RUNTIME_DIR, exist_ok=True)
    if os.path.exists(config.STATE_FILE):
        try:
            with open(config.STATE_FILE) as fh:
                st = json.load(fh)
            for k, v in _fresh().items():
                st.setdefault(k, v)
            n = sum(1 for l in st["legs"] if l.get("status") == "OPEN")
            print(f"[STATE] recovered {config.STATE_FILE} ({n} open legs)")
            return st
        except Exception as e:
            bad = str(config.STATE_FILE) + ".corrupt"
            os.replace(config.STATE_FILE, bad)
            print(f"[STATE] load failed ({e}) - moved to {bad}, starting fresh. CHECK BROKER POSITIONS.")
    st = _fresh()
    save(st)
    return st


def save(st: dict) -> None:
    with _lock:
        try:
            os.makedirs(config.RUNTIME_DIR, exist_ok=True)
            tmp = str(config.STATE_FILE) + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(st, fh, indent=1, default=str)
            os.replace(tmp, config.STATE_FILE)
        except Exception as e:
            print(f"[STATE] save failed (ignored): {e}")


def legs(st: dict, status: str | None = "OPEN") -> list[Leg]:
    return [Leg.from_dict(d) for d in st["legs"] if status is None or d.get("status") == status]


def put_leg(st: dict, leg: Leg) -> None:
    for i, d in enumerate(st["legs"]):
        if d["id"] == leg.id:
            st["legs"][i] = leg.to_dict()
            break
    else:
        st["legs"].append(leg.to_dict())
    # keep the file small: drop legs closed more than ~30 days ago
    if len(st["legs"]) > 400:
        st["legs"] = [d for d in st["legs"] if d.get("status") == "OPEN"] + [d for d in st["legs"] if d.get("status") != "OPEN"][-200:]
    save(st)
