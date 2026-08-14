"""
Trade-state persistence (deliverable: trade state management + crash recovery).

State is written atomically to a JSON file after every meaningful change, so the
strategy can be restarted mid-day and resume without duplicating entries.
"""
import json
import os
import threading
from datetime import date
import config

_lock = threading.Lock()


def _fresh_state():
    return {'hedge_active': False, 'date': date.today().isoformat()}


def load():
    """Load persisted state, or return a fresh skeleton.

    State is scoped to a single trading day: if state.json was written on a
    previous day, it's stale (yesterday's symbols/tokens/entries no longer
    apply) so it's discarded and the file recreated fresh for today.
    """
    today = date.today().isoformat()
    if os.path.exists(config.STATE_FILE):
        try:
            with open(config.STATE_FILE) as f:
                state = json.load(f)
            if state.get('date') != today:
                print(f"[STATE] state.json is from {state.get('date', 'unknown date')} "
                      f"- discarding stale state, starting fresh for {today}")
            else:
                print('[STATE] recovered existing state.json')
                return state
        except Exception as e:
            print(f'[STATE] load failed ({e}) - starting fresh')
    fresh = _fresh_state()
    save(fresh)
    return fresh


def save(state):
    """Atomically persist the state dict. Never raises."""
    with _lock:
        try:
            state['date'] = date.today().isoformat()
            tmp = config.STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, config.STATE_FILE)   # atomic on POSIX
        except Exception as e:
            print(f'[STATE] save failed (ignored): {e}')


def already_done(state, key):
    """
    Duplicate-order protection after restart: True if this session/hedge was
    already entered (deliverable: prevent duplicate orders after restart).
    """
    if key == 'hedge':
        return bool(state.get('hedge_active'))
    return bool(state.get(key, {}).get('entered'))

