"""
Short-straddle session logic (deliverables 9 SL/target monitoring).

One session = SELL ATM CE + SELL ATM PE simultaneously. Each leg runs on its own
daemon thread and is monitored independently:
    Stop Loss = entry + SL_POINTS   (25)
    Target    = entry - TARGET_POINTS (50)
If one leg exits (SL/target), the other keeps running. All entries and exits are
LIMIT orders (market only when emergency=True).
"""
import config
import time
import threading
from datetime import datetime
import token_file
import websocket_feed as wf
from broker import orders
from strategy.hedge import get_atm
from state.store import save


def _now_str():
    return datetime.now().strftime('%H:%M:%S')


def _monitor_leg(state, session, opt_type):
    leg = state[session][opt_type]
    tok = leg['tok']
    entry = leg.get('entry')
    if entry is None:
        # Recovery / missing tick: try hard to establish a real entry price so
        # SL/target are never computed against a bogus 0.
        entry = wf.wait_ltp(tok, timeout=10)
        if entry is None:
            print(f'[{session.upper()} {opt_type.upper()}] no entry price available - '
                  f'leg will only exit on time/emergency')
            # Keep watching only for kill switch / done so time-exit still works.
            while not leg.get('done') and not config.kill_switch:
                time.sleep(1)
            return
        leg['entry'] = entry
        save(state)
    sl = entry + config.SL_POINTS
    target = entry - config.TARGET_POINTS
    print(f'[{session.upper()} {opt_type.upper()}] entry={entry} SL={sl} target={target}')
    while not leg.get('done'):
        if config.kill_switch:                       # risk guard tripped -> stop watching
            return
        ltp = wf.get_ltp(tok)
        if ltp is not None:
            if ltp >= sl:
                _close_leg(state, session, opt_type, 'SL', ltp)
                return
            if ltp <= target:
                _close_leg(state, session, opt_type, 'Target', ltp)
                return
        time.sleep(1)   # poll every second; acts on 1-minute-grade moves


def _close_leg(state, session, opt_type, reason, ltp, emergency=False):
    leg = state[session][opt_type]
    if leg.get('done'):
        return
    place = orders.place_market if emergency else orders.place_limit
    place(leg['sym'], leg['tok'], config.LOT_QTY, 'BUY')   # buy back the short leg
    leg['exit'] = ltp
    leg['exit_reason'] = reason
    leg['exit_time'] = _now_str()
    leg['done'] = True
    save(state)
    # Short leg P&L: SELL so profit = entry - exit.
    entry = leg.get('entry')
    pts = round(entry - ltp, 2) if (entry is not None and ltp is not None) else None
    amt = round(pts * int(config.LOT_QTY), 2) if pts is not None else None
    print(f'[{session.upper()} {opt_type.upper()} EXIT] reason={reason} '
          f'exit_price={ltp} exit_time={leg["exit_time"]} pnl_points={pts} pnl_amount={amt}')


def enter_straddle(state, session):
    """session = 'morning' | 'afternoon'. No re-entry / no duplicate per session."""
    if state.get(session, {}).get('entered'):
        print(f'[{session.upper()}] already entered (recovered) - skip duplicate')
        return
    atm = get_atm()
    ce_sym, ce_tok = token_file.token_nifty(f'{atm}CE')
    pe_sym, pe_tok = token_file.token_nifty(f'{atm}PE')
    wf.subscribe(ce_tok)
    wf.subscribe(pe_tok)
    print(f'[{session.upper()} ENTRY] ATM={atm}')
    orders.place_limit(ce_sym, ce_tok, config.LOT_QTY, 'SELL')
    orders.place_limit(pe_sym, pe_tok, config.LOT_QTY, 'SELL')
    # Wait for a real traded price on each leg so SL/target are computed correctly.
    ce_entry = wf.wait_ltp(ce_tok, timeout=10)
    pe_entry = wf.wait_ltp(pe_tok, timeout=10)
    entry_time = _now_str()
    state[session] = {
        'entered': True,
        'atm': atm,
        'ce': {'sym': ce_sym, 'tok': ce_tok, 'entry': ce_entry, 'entry_time': entry_time, 'done': False},
        'pe': {'sym': pe_sym, 'tok': pe_tok, 'entry': pe_entry, 'entry_time': entry_time, 'done': False},
    }
    save(state)
    print(f'[{session.upper()} ENTRY] entry_time={entry_time} '
          f'CE={ce_sym}@{ce_entry} PE={pe_sym}@{pe_entry}')
    threading.Thread(target=_monitor_leg, args=(state, session, 'ce'), daemon=True).start()
    threading.Thread(target=_monitor_leg, args=(state, session, 'pe'), daemon=True).start()


def resume_monitors(state, session):
    """
    After a crash/restart, re-attach monitor threads to any legs of a session that
    were entered but not yet closed (deliverable: recover after crash/restart).
    """
    sess = state.get(session)
    if not sess or not sess.get('entered'):
        return
    for opt in ('ce', 'pe'):
        leg = sess.get(opt)
        if leg and not leg.get('done'):
            # Re-subscribe so the live feed resumes for this token.
            wf.subscribe(leg['tok'])
            print(f'[{session.upper()} {opt.upper()}] resuming monitor after restart')
            threading.Thread(target=_monitor_leg, args=(state, session, opt), daemon=True).start()


def time_exit_straddle(state, session, emergency=False):
    """Square off any still-open legs of a session and cancel its pending orders."""
    sess = state.get(session)
    if not sess:
        return
    tokens = []
    for opt in ('ce', 'pe'):
        leg = sess.get(opt)
        if leg:
            tokens.append(leg['tok'])
            if not leg.get('done'):
                reason = 'Emergency Exit' if emergency else 'Time Exit'
                _close_leg(state, session, opt, reason, wf.get_ltp(leg['tok']), emergency=emergency)
    # Cancel only THIS session's pending orders so hedge orders are never touched.
    orders.cancel_pending_for_tokens(tokens)


def session_pnl(state, session):
    """
    Return (total_points, total_amount, per_leg_lines) for a straddle session.
    Short legs are SELL, so P&L points = entry - exit.
    """
    sess = state.get(session, {})
    total_pts = 0.0
    total_amt = 0.0
    lines = []
    for opt in ('ce', 'pe'):
        leg = sess.get(opt)
        if not leg:
            continue
        entry = leg.get('entry')
        exit_p = leg.get('exit')
        if entry is None or exit_p is None:
            lines.append(f'{opt.upper()} entry={entry} exit={exit_p} pnl=?')
            continue
        pts = round(entry - exit_p, 2)
        amt = round(pts * int(config.LOT_QTY), 2)
        total_pts += pts
        total_amt += amt
        lines.append(
            f'{opt.upper()} entry={entry} exit={exit_p} '
            f'exit_time={leg.get("exit_time", "-")} pts={pts} amt={amt}'
        )
    return round(total_pts, 2), round(total_amt, 2), lines


def print_session_pnl(state, session):
    """Print a per-leg and total P&L summary for a session (e.g. the morning trade)."""
    total_pts, total_amt, lines = session_pnl(state, session)
    for ln in lines:
        print(f'[{session.upper()} P&L] {ln}')
    print(f'[{session.upper()} P&L] TOTAL points={total_pts} amount={total_amt}')
    return total_pts, total_amt


