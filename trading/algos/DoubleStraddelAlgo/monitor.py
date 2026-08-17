"""
Monitoring integration glue.

This module is the *only* place the trading code talks to the heartbeat agent.
Everything here is best-effort and fully wrapped in try/except so a monitoring
problem can never affect or stop the live strategy.

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
import websocket_feed as wf

# Heartbeat sender feeding the control-center schema (POST /api/heartbeat +
# /api/pnl) -- the only one this algo uses. Previously also ran the shared
# strategy_agent.agent.StrategyHeartbeatAgent (old, separate /update_strategy
# monitor), removed entirely: its config.api_base_url is a hardcoded,
# unreachable "http://127.0.0.1:8000", nothing in this dashboard reads
# /update_strategy's target table anyway, and it was retrying and logging
# "Heartbeat send failed" every ~30s forever for zero benefit.
# Uses STRATEGY_NAME/SERVER_NAME/API_BASE_URL/CONTROL_API_KEY -- the exact
# env vars trading_agent.py's START_ALGO injects (see orchestrator.py) --
# rather than config.strategy_name/config.server_name, which are hardcoded
# to values that don't match this algo's actual registered name/server.
try:
    from trading.common.heartbeat import ControlCenterHeartbeatAgent
    from trading.common.reporting import report_daily_pnl
except ImportError:
    ControlCenterHeartbeatAgent = None
    report_daily_pnl = None

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
    try:
        mtm, pnl, trade_count = compute_metrics()
        _report_control_center(status, pnl, trade_count)
    except Exception as e:
        print(f"[MONITOR] report({status}) ignored error: {e}")


def stop(status="STOPPED"):
    """Send a final status (STOPPED / ERROR) to the control-center agent."""
    if _cc_agent is None:
        return
    try:
        mtm, pnl, trade_count = compute_metrics()
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
def compute_metrics():
    """
    Return (mtm, pnl, trade_count).

    Derived from the broker position book (P&L / MTM) and the cached order book
    (trade count). Any failure falls back to safe zeros so nothing breaks.
    """
    return _compute_pnl_mtm() + (_compute_trade_count(),)


def _compute_pnl_mtm():
    # Paper trading never reaches the broker (see broker/orders.py), so the
    # broker position book is always empty in DRY_RUN - PNL has to come from
    # the local strategy state instead.
    if getattr(config, "DRY_RUN", False):
        return _compute_pnl_mtm_sim()
    return _compute_pnl_mtm_live()


def _compute_pnl_mtm_live():
    mtm = 0.0
    pnl = 0.0
    try:
        positions = orders.refresh_positions() or []
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


def _leg_pnl(leg, side, lot):
    """Return (realised, unrealised) rupee P&L for one leg. side: 'BUY' | 'SELL'."""

    entry = leg.get("entry")
    if entry is None:
        return (0.0, 0.0)
    if leg.get("done"):
        exit_p = leg.get("exit")
        if exit_p is None:
            return (0.0, 0.0)
        pts = (entry - exit_p) if side == "SELL" else (exit_p - entry)
        return (round(pts * lot, 2), 0.0)
    ltp = wf.get_ltp(leg.get("tok"))
    if ltp is None:
        return (0.0, 0.0)
    pts = (entry - ltp) if side == "SELL" else (ltp - entry)
    return (0.0, round(pts * lot, 2))


def _compute_pnl_mtm_sim():
    """Paper-trading PNL/MTM computed from config.state (short straddle legs are
    SELL, hedge legs are BUY) since no broker position book exists in DRY_RUN."""
    mtm = 0.0
    pnl = 0.0
    try:
        state = getattr(config, "state", None) or {}
        lot = int(config.LOT_QTY)

        for session in ("morning", "afternoon"):
            sess = state.get(session) or {}
            for opt in ("ce", "pe"):
                leg = sess.get(opt)
                if leg:
                    realised, unrealised = _leg_pnl(leg, "SELL", lot)
                    pnl += realised + unrealised
                    mtm += unrealised

        hedge = state.get("hedge") or {}
        for opt in ("ce", "pe"):
            leg = hedge.get(opt)
            if leg and leg.get("oid") is not None:
                realised, unrealised = _leg_pnl(leg, "BUY", lot)
                pnl += realised + unrealised
                mtm += unrealised
    except Exception as e:
        print(f"[MONITOR] pnl/mtm compute error (ignored): {e}")
        return (0.0, 0.0)
    return (round(mtm, 2), round(pnl, 2))


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
    # Slightly under the 30s heartbeat so values are fresh each heartbeat.
    while True:
        time.sleep(20)
        report("RUNNING")

