import config, rest_func, monitor, time
import pandas as pd
from datetime import datetime


def trademanager():
    try:
        _trademanager()
    except Exception as e:
        # A crash in the trading thread -> flag ERROR on the monitor, then re-raise.
        print(f'[FATAL] trademanager crashed: {e}')
        monitor.report('ERROR')
        raise


def _trademanager():
    ce_symbol, ce_token = config.ce_symbol, config.ce_token
    pe_symbol, pe_token = config.pe_symbol, config.pe_token
    qty = int(config.qty)

    for token in (ce_token, pe_token):
        config.in_position[token] = False
        config.entry_price[token] = 0
        config.cum_loss[token] = 0
        config.last_exit_time[token] = 0
        config.reentry_count[token] = 0
    config.combined_risk_level_index = 0
    config.day_stopped = False

    # 'WAIT_ARM' -> (CP>CV) -> 'ARMED' -> (CV>CP) -> fires entries, resets to 'WAIT_ARM'
    signal_state = 'WAIT_ARM'
    firstflagentry = True
    day_done = False

    print(f'[MANAGER] Started for strike {config.locked_strike}  CE={ce_symbol}  PE={pe_symbol}  qty={qty}')
    monitor.report('RUNNING')

    while True:
        dt = datetime.now()

        # --- continuous (tick-level) combined CE+PE premium risk ladder ---
        if not day_done:
            check_combined_risk(ce_symbol, ce_token, pe_symbol, pe_token, qty)
            if config.day_stopped:
                print('[MANAGER] Rule 3 stop reached - ending run for the day')
                day_done = True
                monitor.report('STOPPED')
                break

        # --- stale feed watchdog (falls back to REST LTP polling) ---
        check_stale_feed(ce_token, ce_symbol)
        check_stale_feed(pe_token, pe_symbol)

        # --- EOD square-off (unconditional close of any open leg) ---
        if not day_done and dt.strftime('%H:%M:%S') >= config.EOD_SQUARE_OFF_TIME:
            print('[MANAGER] EOD square-off time reached')
            if config.in_position[ce_token]:
                exit_leg(ce_symbol, ce_token, qty, reason='EOD')
            if config.in_position[pe_token]:
                exit_leg(pe_symbol, pe_token, qty, reason='EOD')
            day_done = True
            monitor.report('STOPPED')
            break

        # --- evaluate the CP/CV signal only once per completed 1-minute candle ---
        if (dt.second == 0) or firstflagentry:
            if (len(config.ohlc_data.get(ce_token, {}).get('Datetime', [])) == 0 or
                    len(config.ohlc_data.get(pe_token, {}).get('Datetime', [])) == 0):
                time.sleep(1)
                continue

            df = rest_func.make_cp_cv(pd.DataFrame(config.ohlc_data[ce_token]),
                                       pd.DataFrame(config.ohlc_data[pe_token]))
            if df.empty:
                firstflagentry = False
                time.sleep(1)
                continue

            count = len(df) - 1
            ts = df['Datetime'][count]
            cp = df['CP'][count]
            cv = df['CV'][count]

            if signal_state == 'WAIT_ARM' and cp > cv:
                signal_state = 'ARMED'
                print(f'[SIGNAL] {ts} ARMED  (CP {cp} > CV {cv})')

            elif signal_state == 'ARMED' and cv > cp:
                print(f'[SIGNAL] {ts} TRIGGERED  (CV {cv} > CP {cp})')
                handle_trigger(ts, ce_symbol, ce_token, pe_symbol, pe_token, qty)
                signal_state = 'WAIT_ARM'   # require a fresh arm->trigger before any further entry

            firstflagentry = False
            time.sleep(1)   # don't re-evaluate the same second twice

        time.sleep(0.2)


# --------------------------------------------------------------------------- #
# Entry / re-entry
# --------------------------------------------------------------------------- #
def handle_trigger(ts, ce_symbol, ce_token, pe_symbol, pe_token, qty):
    if config.day_stopped:
        return
    if ts >= config.NO_NEW_ENTRY_AFTER:
        print(f'[SIGNAL] {ts} trigger ignored - past no-new-entry cutoff ({config.NO_NEW_ENTRY_AFTER})')
        return
    for symbol, token in ((ce_symbol, ce_token), (pe_symbol, pe_token)):
        if config.in_position[token]:
            continue
        # A leg that has already been exited at least once today (last_exit_time>0)
        # is a RE-entry and is subject to the cooldown gate (see can_reenter).
        is_reentry = config.last_exit_time.get(token, 0) > 0
        if is_reentry and not can_reenter(token):
            continue
        enter_leg(symbol, token, qty)
        if is_reentry and config.in_position[token]:
            config.reentry_count[token] = config.reentry_count.get(token, 0) + 1
            print(f'[REENTRY] {symbol} re-entry #{config.reentry_count[token]}')


def can_reenter(token):
    """Re-entry gate: the day hasn't been stopped by Rule 3, and the
    cooldown has elapsed since the last exit (the re-entry itself is only
    ever called from a fresh CV>CP trigger, see handle_trigger). The
    combined-loss ladder (Rules 1-3) is what ultimately caps how many
    times this can happen -- no separate re-entry count limit."""
    if config.day_stopped:
        return False
    last_exit = config.last_exit_time.get(token, 0)
    if last_exit and (time.time() - last_exit) < config.REENTRY_COOLDOWN_SECONDS:
        return False
    return True


def enter_leg(symbol, token, qty):
    ref_price = config.last_ltp.get(token, 0)
    if ref_price <= 0:
        print(f'[ENTRY] {symbol} skipped - no LTP yet')
        return
    filled_qty, avg_price, slippage = rest_func.execute_limit_order(
        symbol, token, qty, 'SELL', ref_price, label='ENTRY')
    if filled_qty > 0:
        config.in_position[token] = True
        config.entry_price[token] = avg_price
        print(f'[ENTRY] {symbol} SOLD qty={filled_qty} @ {avg_price} (slippage={slippage})')
        monitor.report('RUNNING')
    else:
        print(f'[ENTRY] {symbol} FAILED to fill - remains flat')


# --------------------------------------------------------------------------- #
# Exit + risk ladder
# --------------------------------------------------------------------------- #
def exit_leg(symbol, token, qty, reason):
    ref_price = config.last_ltp.get(token, config.entry_price.get(token, 0))
    filled_qty, avg_price, slippage = rest_func.execute_limit_order(
        symbol, token, qty, 'BUY', ref_price, label=f'EXIT_{reason}')
    exit_qty = filled_qty or qty
    exit_price = avg_price or ref_price
    entry_price = config.entry_price.get(token, exit_price)
    pnl = round((entry_price - exit_price) * exit_qty, 2)   # short leg P&L

    if pnl < 0:
        config.cum_loss[token] = config.cum_loss.get(token, 0) + abs(pnl)

    config.in_position[token] = False
    config.entry_price[token] = 0
    config.last_exit_time[token] = time.time()

    print(f'[EXIT] {symbol} BOUGHT qty={exit_qty} @ {exit_price} pnl={pnl} reason={reason}')
    monitor.report('RUNNING')


def _leg_unrealized_loss(token, qty):
    """Current mark-to-market loss on this leg if it's open, else 0.
    (short option: loss when the premium rises above entry)."""
    if not config.in_position.get(token):
        return 0.0
    ltp = config.last_ltp.get(token)
    entry = config.entry_price.get(token, 0)
    if ltp is None:
        return 0.0
    return max(0.0, (ltp - entry) * qty)


def _combined_loss(ce_token, pe_token, qty):
    """Rules 1-3: TOTAL combined CE+PE premium loss per lot -- realized
    losses already booked today on either leg, plus live unrealized MTM
    on whichever leg(s) are currently open."""
    realized = config.cum_loss.get(ce_token, 0.0) + config.cum_loss.get(pe_token, 0.0)
    unrealized = _leg_unrealized_loss(ce_token, qty) + _leg_unrealized_loss(pe_token, qty)
    return realized + unrealized


def _bigger_loser(ce_symbol, ce_token, pe_symbol, pe_token, qty):
    """Whichever OPEN leg's premium has risen more since its own entry
    (Rules 1/2: "if CE premium is increasing more -> CE is losing more")."""
    candidates = [
        (_leg_unrealized_loss(ce_token, qty), ce_symbol, ce_token),
        (_leg_unrealized_loss(pe_token, qty), pe_symbol, pe_token),
    ]
    candidates = [c for c in candidates if config.in_position.get(c[2])]
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def check_combined_risk(ce_symbol, ce_token, pe_symbol, pe_token, qty):
    """Rules 1-3, checked on every loop iteration (not just candle close):
    watch the COMBINED CE+PE premium loss per lot, not each leg's own loss
    in isolation.
      Rule 1 (>=650) / Rule 2 (>=1300): exit ONLY the leg that's losing more,
        keep the other running, allow that leg to re-enter later.
      Rule 3 (>=2000): exit BOTH legs immediately, stop trading for the day.
    """
    level_index = config.combined_risk_level_index
    if level_index >= len(config.RISK_LOSS_LEVELS):
        return
    combined_loss = _combined_loss(ce_token, pe_token, qty)
    threshold = config.RISK_LOSS_LEVELS[level_index] * config.num_lots
    if combined_loss < threshold:
        return

    if level_index >= len(config.RISK_LOSS_LEVELS) - 1:
        # Rule 3: the top of the ladder -- exit both, stop for the day.
        print(f'[RISK] Rule 3: combined loss {combined_loss:.2f} >= {threshold:.2f} '
              f'- exiting BOTH legs, no more trading today')
        if config.in_position.get(ce_token):
            exit_leg(ce_symbol, ce_token, qty, reason='SL_LEVEL_3')
        if config.in_position.get(pe_token):
            exit_leg(pe_symbol, pe_token, qty, reason='SL_LEVEL_3')
        config.combined_risk_level_index += 1
        config.day_stopped = True
        return

    # Rule 1 / Rule 2: exit only the bigger loser, other leg keeps running.
    loser_symbol, loser_token = _bigger_loser(ce_symbol, ce_token, pe_symbol, pe_token, qty)
    if loser_token is None:
        return
    print(f'[RISK] Rule {level_index + 1}: combined loss {combined_loss:.2f} >= {threshold:.2f} '
          f'- exiting losing leg {loser_symbol} only')
    exit_leg(loser_symbol, loser_token, qty, reason=f'SL_LEVEL_{level_index + 1}')
    config.combined_risk_level_index += 1


# --------------------------------------------------------------------------- #
# Stale/disconnected data detection
# --------------------------------------------------------------------------- #
_stale_alerted = {}

def check_stale_feed(token, symbol):
    last_tick = config.last_tick_time.get(token)
    if last_tick is None:
        return   # never received a tick yet -> nothing to compare against
    stale = (time.time() - last_tick) > config.STALE_DATA_SECONDS
    was_alerted = _stale_alerted.get(token, False)
    if stale and not was_alerted:
        _stale_alerted[token] = True
        print(f'[FEED] {symbol} feed STALE - falling back to REST polling')
    elif not stale and was_alerted:
        _stale_alerted[token] = False
        print(f'[FEED] {symbol} feed recovered')
    if stale:
        ltp = rest_func.get_rest_ltp(token)
        if ltp is not None:
            config.last_ltp[token] = ltp
            config.last_tick_time[token] = time.time()
