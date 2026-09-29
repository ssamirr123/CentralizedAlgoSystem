"""
Short-straddle session logic (deliverables 9 SL/target monitoring).

One session = SELL ATM CE + SELL ATM PE simultaneously. Each leg runs on its own
daemon thread and is monitored independently:
    Stop Loss = entry + SL_POINTS   (25)
    Target    = entry - TARGET_POINTS (50)
If one leg exits (SL/target), the other keeps running. All entries and exits are
LIMIT orders (market only when emergency=True).

Each short leg also gets a resting broker STOPLOSS_LIMIT BUY at the same SL
level right after entry (config.PLACE_BROKER_SL), so it stays protected even if
this process dies. Before any other exit of the leg that order is cancelled; if
it had already filled, the leg is recorded as SL-exited and NOT bought again.
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


_SL_CHECK_SECONDS = 10    # how often a leg polls its broker SL for a fill
_DEAD = ('cancelled', 'rejected')
_close_locks = {}         # (session, opt_type) -> Lock: one close at a time per leg
_close_locks_guard = threading.Lock()


def _close_lock(session, opt_type):
    with _close_locks_guard:
        return _close_locks.setdefault((session, opt_type), threading.Lock())


def _place_leg_sl(state, session, opt_type):
    """Place the resting broker SL for an open leg if it doesn't have one."""
    leg = state[session][opt_type]
    if not config.PLACE_BROKER_SL or leg.get('done') or leg.get('sl_oid') or leg.get('entry') is None:
        return
    trigger = leg['entry'] + config.SL_POINTS
    oid = orders.place_stoploss(leg['sym'], leg['tok'], config.LOT_QTY, trigger)
    if oid:
        leg['sl_oid'] = oid
        save(state)
    else:
        print(f'[{session.upper()} {opt_type.upper()}] broker SL NOT placed - software SL only')


def _record_exit(state, session, opt_type, reason, price):
    leg = state[session][opt_type]
    leg['exit'] = price
    leg['exit_reason'] = reason
    leg['exit_time'] = _now_str()
    leg['done'] = True
    save(state)
    # Short leg P&L: SELL so profit = entry - exit.
    entry = leg.get('entry')
    pts = round(entry - price, 2) if (entry is not None and price is not None) else None
    amt = round(pts * int(config.LOT_QTY), 2) if pts is not None else None
    print(f'[{session.upper()} {opt_type.upper()} EXIT] reason={reason} '
          f'exit_price={price} exit_time={leg["exit_time"]} pnl_points={pts} pnl_amount={amt}')


def _sl_filled(state, session, opt_type):
    """True (and the leg recorded as SL-exited) if its broker SL has filled."""
    leg = state[session][opt_type]
    with _close_lock(session, opt_type):
        oid = leg.get('sl_oid')
        if not oid or leg.get('done'):
            return leg.get('done', False)
        status, avg = orders.fresh_status(oid)
        if status != 'complete':
            return False
        _record_exit(state, session, opt_type, 'SL (broker)', avg or wf.get_ltp(leg['tok']))
        return True


def _cancel_leg_sl(state, session, opt_type):
    """Cancel the leg's broker SL before another exit. Returns 'filled' if it
    had already executed (leg recorded; caller must NOT buy again), 'cancelled'
    if it is gone, or 'unknown' if cancellation could not be confirmed."""
    leg = state[session][opt_type]
    oid = leg.get('sl_oid')
    if not oid:
        return 'cancelled'
    if getattr(config, 'DRY_RUN', False):
        orders.cancel(oid, 'STOPLOSS')
        return 'cancelled'
    # Look first: only cancel an order that is still live (re-cancelling a
    # finished one just errors and burns the retry budget).
    status, avg = orders.fresh_status(oid)
    if status not in ('complete',) + _DEAD:
        orders.cancel(oid, 'STOPLOSS')
        status, avg = orders.fresh_status(oid)
    if status == 'complete':
        _record_exit(state, session, opt_type, 'SL (broker)', avg or wf.get_ltp(leg['tok']))
        return 'filled'
    if status in _DEAD:
        return 'cancelled'
    print(f'[{session.upper()} {opt_type.upper()}] broker SL {oid} cancel not confirmed (status={status!r})')
    return 'unknown'


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
    _place_leg_sl(state, session, opt_type)   # no-op if already placed (e.g. after restart)
    sl = entry + config.SL_POINTS
    target = entry - config.TARGET_POINTS
    print(f'[{session.upper()} {opt_type.upper()}] entry={entry} SL={sl} target={target}')
    last_sl_check = time.monotonic()
    while not leg.get('done'):
        if config.kill_switch:                       # risk guard tripped -> stop watching
            return
        if leg.get('sl_oid') and time.monotonic() - last_sl_check >= _SL_CHECK_SECONDS:
            last_sl_check = time.monotonic()
            if _sl_filled(state, session, opt_type):
                return
        ltp = wf.get_ltp(tok)
        if ltp is not None:
            if ltp >= sl:
                _close_leg(state, session, opt_type, 'SL', ltp)
            elif ltp <= target:
                _close_leg(state, session, opt_type, 'Target', ltp)
            if leg.get('done'):
                return
        time.sleep(1)   # poll every second; acts on 1-minute-grade moves


def _close_leg(state, session, opt_type, reason, ltp, emergency=False):
    # The leg's monitor thread and the engine's time exit can race here; the
    # lock makes the second caller see done=True instead of buying again.
    with _close_lock(session, opt_type):
        _close_leg_locked(state, session, opt_type, reason, ltp, emergency)


def _close_leg_locked(state, session, opt_type, reason, ltp, emergency):
    leg = state[session][opt_type]
    if leg.get('done'):
        return
    # Cancel the resting broker SL first so it can't fire after we buy back.
    sl = _cancel_leg_sl(state, session, opt_type)
    if sl == 'filled':
        return                       # broker SL already bought the leg back
    if sl == 'unknown' and reason in ('SL', 'Target'):
        # SL may still be live -> buying now could double the buy-back. Leave
        # the leg open; the monitor retries on its next tick. (Time/emergency
        # exits go ahead: flattening takes priority, and the session's pending
        # orders are cancelled right after.)
        return
    place = orders.place_market if emergency else orders.place_limit
    place(leg['sym'], leg['tok'], config.LOT_QTY, 'BUY')   # buy back the short leg
    _record_exit(state, session, opt_type, reason, ltp)


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
    _place_leg_sl(state, session, 'ce')
    _place_leg_sl(state, session, 'pe')
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


