"""
Monitoring integration glue - feeds the control-center schema (POST
/api/heartbeat + /api/pnl), the only heartbeat system this algo uses.
Everything here is best-effort and fully wrapped in try/except so a
monitoring problem can never affect or stop the live strategy.

Previously also ran the shared strategy_agent.agent.StrategyHeartbeatAgent
(old, separate /update_strategy monitor) -- removed: that system is
disconnected from this dashboard, and chaining `StrategyHeartbeatAgent(...)
.start()` into config.agent is broken anyway (the shared repo-root
strategy_agent/agent.py's start() returns None, not self, once
project_root is on sys.path, which write_pid_file in main.py requires).

Metrics reported:
    mtm          -> float : current mark-to-market (open + booked)
    pnl          -> float : cumulative day P&L
    trade_count  -> int   : number of completed trades today
    status       -> str   : "RUNNING" | "STOPPED" | "ERROR"
"""

import os
import threading
import time
import atexit

import config

# Uses STRATEGY_NAME/SERVER_NAME/API_BASE_URL/CONTROL_API_KEY -- the exact
# env vars trading_agent.py's START_ALGO injects (see orchestrator.py) --
# rather than config.strategy_name/config.server_name, which are hardcoded
# to values that don't match this algo's actual registered name/server.
try:
    from trading.common.heartbeat import ControlCenterHeartbeatAgent
    from trading.common.reporting import report_daily_pnl, report_position
except ImportError:
    ControlCenterHeartbeatAgent = None
    report_daily_pnl = None
    report_position = None

_CC_ALGO_NAME = os.environ.get("STRATEGY_NAME")
_CC_SERVER_NAME = os.environ.get("SERVER_NAME")
_CC_API_BASE_URL = os.environ.get("API_BASE_URL")
_CC_API_KEY = os.environ.get("CONTROL_API_KEY")
_CC_PNL_REPORT_INTERVAL_SECONDS = 60

_cc_agent = None
_last_cc_pnl_report_monotonic = 0.0


def start():
    """
    Create + start the control-center heartbeat agent (idempotent) and a
    lightweight metrics refresher. Returns the agent (or None if monitoring
    is disabled/failed).
    """
    if not getattr(config, "monitoring_enabled", False):
        return None
    if _cc_agent is not None:
        return _cc_agent
    try:
        _start_control_center_agent()
        # Report the initial RUNNING state.
        report("RUNNING")
        # Refresh live metrics in the background so pnl/mtm stay current even
        # between trades. Runs on a daemon thread -> never blocks trading.
        threading.Thread(target=_refresh_loop, daemon=True).start()
        # Make sure a STOPPED report is sent when the process exits cleanly.
        atexit.register(lambda: stop("STOPPED"))
    except Exception as e:
        print(f"[MONITOR] failed to start agent (ignored): {e}")
    return _cc_agent


def _start_control_center_agent():
    global _cc_agent
    if ControlCenterHeartbeatAgent is None:
        return  # trading.common not importable (e.g. run outside this repo's venv)
    if not (_CC_ALGO_NAME and _CC_SERVER_NAME and _CC_API_BASE_URL and _CC_API_KEY):
        print("[MONITOR] control-center heartbeat skipped: STRATEGY_NAME/SERVER_NAME/"
              "API_BASE_URL/CONTROL_API_KEY not all set (not launched via trading_agent.py?)")
        return
    try:
        _cc_agent = ControlCenterHeartbeatAgent(
            algo_name=_CC_ALGO_NAME,
            server_name=_CC_SERVER_NAME,
            api_base_url=_CC_API_BASE_URL,
            api_key=_CC_API_KEY,
        )
        _cc_agent.start()
    except Exception as e:
        print(f"[MONITOR] failed to start control-center agent (ignored): {e}")
        _cc_agent = None


def _report_control_center(status, pnl, trade_count):
    global _last_cc_pnl_report_monotonic
    if _cc_agent is None:
        return
    try:
        _cc_agent.update_metrics(status=status, pnl=pnl)
        now = time.monotonic()
        if report_daily_pnl is not None and now - _last_cc_pnl_report_monotonic >= _CC_PNL_REPORT_INTERVAL_SECONDS:
            report_daily_pnl(_CC_API_BASE_URL, _CC_API_KEY, _CC_ALGO_NAME, _CC_SERVER_NAME, pnl=pnl, trade_count=trade_count)
            _last_cc_pnl_report_monotonic = now
    except Exception as e:
        print(f"[MONITOR] control-center report ignored error: {e}")


def report(status="RUNNING"):
    """Compute fresh metrics and push them to the control-center agent. Never raises."""
    if _cc_agent is None:
        return
    # obj.position() hits the broker API -- fetch it once per cycle and
    # share it between the pnl/mtm computation and the per-symbol report
    # below, instead of the two of them independently double-calling it
    # (this was tripping Angel One's "exceeding access rate" limit).
    positions = _fetch_positions()
    try:
        mtm, pnl, trade_count = compute_metrics(positions)
        _report_control_center(status, pnl, trade_count)
    except Exception as e:
        print(f"[MONITOR] report({status}) ignored error: {e}")
    _report_positions(positions)


def stop(status="STOPPED"):
    """Send a final status (STOPPED / ERROR) to the control-center agent."""
    if _cc_agent is None:
        return
    try:
        mtm, pnl, trade_count = compute_metrics(_fetch_positions())
        _report_control_center(status, pnl, trade_count)
    except Exception as e:
        print(f"[MONITOR] stop({status}) metric error (ignored): {e}")
    try:
        _cc_agent.stop()
    except Exception as e:
        print(f"[MONITOR] control-center stop ignored error: {e}")


# --------------------------------------------------------------------------- #
# Metric computation (best-effort, broker-backed)
# --------------------------------------------------------------------------- #
def _fetch_positions():
    """Single broker call for the position book, shared by _compute_pnl_mtm
    and _report_positions below -- calling obj.position() twice per report
    cycle was tripping Angel One's "exceeding access rate" limit."""
    try:
        obj = getattr(config, "objconn", None)
        if not obj or not hasattr(obj, "position"):
            return []
        data = obj.position()
        return (data or {}).get("data") or []
    except Exception as e:
        print(f"[MONITOR] position fetch ignored error: {e}")
        return []


def compute_metrics(positions=None):
    """
    Return (mtm, pnl, trade_count).

    Derived from the broker position book (P&L / MTM) and the cached order book
    (trade count). Any failure falls back to safe zeros so nothing breaks.
    """
    if positions is None:
        positions = _fetch_positions()
    return _compute_pnl_mtm(positions) + (_compute_trade_count(),)


def _compute_pnl_mtm(positions):
    mtm = 0.0
    pnl = 0.0
    try:
        for p in positions:
            realised = _to_float(p.get("realised"))
            unrealised = _to_float(p.get("unrealised"))
            net = _to_float(p.get("pnl"))
            # pnl  -> cumulative day P&L (realised + unrealised, fall back to net)
            pnl += (realised + unrealised) if (realised or unrealised) else net
            # mtm  -> current mark-to-market of open positions (fall back to net)
            mtm += unrealised if unrealised else net
    except Exception as e:
        print(f"[MONITOR] pnl/mtm compute error (ignored): {e}")
        return (0.0, 0.0)
    return (round(mtm, 2), round(pnl, 2))


def _report_positions(positions=None):
    """Push per-symbol position rows to POST /api/positions so the
    dashboard's Positions tab has something to show -- compute_metrics()
    above only ever sends an aggregated mtm/pnl total, never a per-symbol
    breakdown. Broker already reports a netqty=0 row for a closed leg,
    which report_position treats as "delete this row" server-side, so no
    separate close-tracking is needed here."""
    if report_position is None or _cc_agent is None:
        return
    try:
        if positions is None:
            positions = _fetch_positions()
        for p in positions:
            symbol = p.get("tradingsymbol")
            if not symbol:
                continue
            realised = _to_float(p.get("realised"))
            unrealised = _to_float(p.get("unrealised"))
            net = _to_float(p.get("pnl"))
            pnl = (realised + unrealised) if (realised or unrealised) else net
            report_position(
                _CC_API_BASE_URL, _CC_API_KEY, _CC_ALGO_NAME, _CC_SERVER_NAME,
                symbol=symbol, quantity=int(_to_float(p.get("netqty"))),
                average_price=_to_float(p.get("avgnetprice")),
                last_price=_to_float(p.get("ltp")) or None, pnl=round(pnl, 2),
            )
    except Exception as e:
        print(f"[MONITOR] position report ignored error: {e}")


def _compute_trade_count():
    try:
        orderbook = getattr(config, "orderbook", None) or []
        return sum(
            1
            for o in orderbook
            if str(o.get("status", "")).lower() == "complete"
        )
    except Exception as e:
        print(f"[MONITOR] trade_count compute error (ignored): {e}")
        return 0


def _to_float(value):
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _refresh_loop():
    """Periodically push fresh metrics (RUNNING) so the server stays current."""
    # 60s rather than the 30s heartbeat -- each cycle hits the broker's
    # position() API, and Angel One's rate limit made a tighter interval
    # too aggressive. Dashboard staleness tolerance covers this gap.
    while True:
        time.sleep(60)
        report("RUNNING")
