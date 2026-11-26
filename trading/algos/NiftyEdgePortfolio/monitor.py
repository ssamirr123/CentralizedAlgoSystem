"""
Best-effort bridge to the control centre (heartbeat + daily P&L), like the other
AngelOne algos' monitor.py. Every call is wrapped in try/except: monitoring can
never affect trading.
"""
import os
import threading
import time

import config

try:
    from trading.common.heartbeat import ControlCenterHeartbeatAgent
    from trading.common.reporting import report_daily_pnl
except ImportError:
    ControlCenterHeartbeatAgent = None
    report_daily_pnl = None

_NAME = os.environ.get("STRATEGY_NAME")
_SERVER = os.environ.get("SERVER_NAME")
_URL = os.environ.get("API_BASE_URL")
_KEY = os.environ.get("CONTROL_API_KEY")
_agent = None
_ctx = None


def _lots():
    return sum(c["lots"] for c in config.STRATEGIES.values() if c.get("enabled"))


def start():
    global _agent
    if not config.monitoring_enabled or ControlCenterHeartbeatAgent is None:
        return None
    if not (_NAME and _SERVER and _URL and _KEY):
        print("[MONITOR] control-centre heartbeat skipped (STRATEGY_NAME/SERVER_NAME/API_BASE_URL/CONTROL_API_KEY not set)")
        return None
    try:
        _agent = ControlCenterHeartbeatAgent(algo_name=_NAME, server_name=_SERVER, api_base_url=_URL, api_key=_KEY)
        _agent.start()
        threading.Thread(target=_loop, daemon=True).start()
    except Exception as e:
        print(f"[MONITOR] start failed (ignored): {e}")
        _agent = None
    return _agent


def attach(ctx):
    global _ctx
    _ctx = ctx


def _metrics():
    pnl = getattr(_ctx, "day_pnl", 0.0) if _ctx else 0.0
    return round(float(pnl), 2)


def _loop():
    while _agent is not None:
        try:
            _agent.update_metrics(status="RUNNING", pnl=_metrics(), trading_mode="PAPER" if config.DRY_RUN else "LIVE", running_lots=_lots())
            if report_daily_pnl:
                report_daily_pnl(_URL, _KEY, _NAME, _SERVER, pnl=_metrics(), trade_count=0)
        except Exception as e:
            print(f"[MONITOR] report ignored error: {e}")
        time.sleep(60)


def stop(status="STOPPED", ctx=None):
    global _agent
    if ctx is not None:
        attach(ctx)
    if _agent is None:
        return
    try:
        _agent.update_metrics(status=status, pnl=_metrics(), trading_mode="PAPER" if config.DRY_RUN else "LIVE", running_lots=_lots())
        _agent.stop()
    except Exception as e:
        print(f"[MONITOR] stop ignored error: {e}")
    _agent = None
