"""
Hedge entry / exit logic (deliverable 10).

- Hedge is bought FIRST (10:15) as LIMIT orders, far-OTM CE + PE.
- Distance is 500 pts on expiry day, else 1000 pts.
- Hedge stays active all day; it is NOT touched at the 14:14 morning exit.
- Hedge exits only at 15:25 (final) or during emergency square-off (market).
"""
import config
import token_file
import websocket_feed as wf
from datetime import datetime
from broker import orders
from strategy.expiry import is_expiry_day, get_hedge_strikes
from state.store import save


def _now_str():
    return datetime.now().strftime('%H:%M:%S')


def get_atm():
    """Current NIFTY ATM strike, rounded to STRIKE_STEP."""
    # Bounded wait on the live WS tick (index token is subscribed on every
    # socket open - see websocket_feed._on_open). Previously this fell back to
    # a raw, unguarded config.objconn.ltpData() call with no timeout, which
    # could hang the single-threaded engine loop indefinitely on a stalled
    # broker response. wf.get_ltp's REST fallback is try/except-wrapped and
    # bound by SmartConnect's own request timeout, so it can only ever delay,
    # never freeze, the loop.
    ltp = wf.wait_ltp(config.INDEX_TOKEN, timeout=10, fallback_rest=False)
    if ltp is None:
        ltp = wf.get_ltp(config.INDEX_TOKEN, fallback_rest=True, exchange=config.INDEX_EXCH)
    if ltp is None:
        raise RuntimeError('get_atm: no NIFTY spot LTP available (WS + REST both failed)')
    return round(ltp / config.STRIKE_STEP) * config.STRIKE_STEP


def enter_hedge(state):
    """Buy the far-OTM hedge. Returns True if the hedge is (now or already) active,
    False if either leg failed to place - callers MUST skip straddle entry on False,
    since selling naked without the hedge defeats the whole point of the strategy."""
    if state.get('hedge_active'):
        print('[HEDGE] already active (recovered) - skipping duplicate entry')
        return True
    atm = get_atm()
    expiry = is_expiry_day()
    ce_k, pe_k = get_hedge_strikes(atm, expiry)
    ce_sym, ce_tok = token_file.token_nifty(f'{ce_k}CE')
    pe_sym, pe_tok = token_file.token_nifty(f'{pe_k}PE')
    wf.subscribe(ce_tok)
    wf.subscribe(pe_tok)
    print(f'[HEDGE ENTRY] ATM={atm} expiry_day={expiry} CE_strike={ce_k} PE_strike={pe_k}')
    ce_oid = orders.place_limit(ce_sym, ce_tok, config.LOT_QTY, 'BUY')
    pe_oid = orders.place_limit(pe_sym, pe_tok, config.LOT_QTY, 'BUY')
    entry_time = _now_str()
    # Record whatever happened on each leg regardless of outcome (oid=None on a
    # failed leg) so a leg that DID get bought is never lost track of - exit_hedge
    # below still squares off any leg with a real oid even if the pair isn't fully
    # active.
    state['hedge'] = {
        'ce': {'sym': ce_sym, 'tok': ce_tok, 'strike': ce_k, 'oid': ce_oid,
               'entry': wf.get_ltp(ce_tok) if ce_oid else None, 'entry_time': entry_time},
        'pe': {'sym': pe_sym, 'tok': pe_tok, 'strike': pe_k, 'oid': pe_oid,
               'entry': wf.get_ltp(pe_tok) if pe_oid else None, 'entry_time': entry_time},
    }
    both_ok = ce_oid is not None and pe_oid is not None
    state['hedge_active'] = both_ok
    save(state)
    if both_ok:
        print(f'[HEDGE ENTRY] entry_time={entry_time} '
              f'CE_entry={state["hedge"]["ce"]["entry"]} '
              f'PE_entry={state["hedge"]["pe"]["entry"]}')
    else:
        print(f'[HEDGE ENTRY FAILED] CE_oid={ce_oid} PE_oid={pe_oid} - '
              f'hedge NOT active, straddle entry will be skipped this session for safety')
    return both_ok


def exit_hedge(state, emergency=False):
    h = state.get('hedge')
    if not h:
        return
    place = orders.place_market if emergency else orders.place_limit
    for leg in ('ce', 'pe'):
        legdata = h.get(leg)
        if not legdata or legdata.get('oid') is None:
            continue   # this leg was never actually bought - nothing to sell
        place(legdata['sym'], legdata['tok'], config.LOT_QTY, 'SELL')
        legdata['exit'] = wf.get_ltp(legdata['tok'])
        legdata['exit_time'] = _now_str()
        print(f'[HEDGE EXIT] {leg.upper()} {legdata["sym"]} exit_price={legdata["exit"]} '
              f'exit_time={legdata["exit_time"]} {"(EMERGENCY)" if emergency else ""}')
    state['hedge_active'] = False
    save(state)

