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
        config.risk_level_index[token] = 0
        config.risk_disabled[token] = False
        config.cum_loss[token] = 0
        config.last_exit_time[token] = 0
        config.reentry_count[token] = 0

    # 'WAIT_ARM' -> (CP>CV) -> 'ARMED' -> (CV>CP) -> fires entries, resets to 'WAIT_ARM'
    signal_state = 'WAIT_ARM'
    firstflagentry = True
    day_done = False

    print(f'[MANAGER] Started for strike {config.locked_strike}  CE={ce_symbol}  PE={pe_symbol}  qty={qty}')
    monitor.report('RUNNING')

    while True:
        dt = datetime.now()

        # --- continuous (tick-level) risk checks -> exit only the losing leg ---
        check_leg_risk(ce_symbol, ce_token, qty)
        check_leg_risk(pe_symbol, pe_token, qty)

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
    if ts >= config.NO_NEW_ENTRY_AFTER:
        print(f'[SIGNAL] {ts} trigger ignored - past no-new-entry cutoff ({config.NO_NEW_ENTRY_AFTER})')
        return
    for symbol, token in ((ce_symbol, ce_token), (pe_symbol, pe_token)):
        if config.in_position[token] or config.risk_disabled[token]:
            continue
        # A leg that has already been exited at least once today (last_exit_time>0)
        # is a RE-entry and is subject to the cooldown + max-reentries rules.
        is_reentry = config.last_exit_time.get(token, 0) > 0
        if is_reentry and not can_reenter(token):
            continue
        enter_leg(symbol, token, qty)
        if is_reentry and config.in_position[token]:
            config.reentry_count[token] = config.reentry_count.get(token, 0) + 1
            print(f'[REENTRY] {symbol} re-entry #{config.reentry_count[token]} '
                  f'of max {config.MAX_REENTRIES_PER_LEG}')


def can_reenter(token):
    """Configurable re-entry gate: not disabled, under the max-reentries cap,
    and cooldown elapsed since the last SL exit (the re-entry itself is only
    ever called from a fresh CV>CP trigger, see handle_trigger)."""
    if config.risk_disabled[token]:
        return False
    if config.reentry_count.get(token, 0) >= config.MAX_REENTRIES_PER_LEG:
        print(f'[REENTRY] token {token} blocked - max re-entries '
              f'({config.MAX_REENTRIES_PER_LEG}) reached')
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
    if reason.startswith('SL_LEVEL'):
        config.risk_level_index[token] = config.risk_level_index.get(token, 0) + 1
        if config.risk_level_index[token] >= len(config.RISK_LOSS_LEVELS):
            config.risk_disabled[token] = True
            print(f'[RISK] {symbol} DISABLED for the day (cumulative_loss={config.cum_loss[token]:.2f})')

    config.in_position[token] = False
    config.entry_price[token] = 0
    config.last_exit_time[token] = time.time()

    print(f'[EXIT] {symbol} BOUGHT qty={exit_qty} @ {exit_price} pnl={pnl} reason={reason}')
    monitor.report('RUNNING')


def check_leg_risk(symbol, token, qty):
    """Checked on every loop iteration (not just candle close) so the ₹650/
    1300/2000 ladder reacts to *actual* live P&L, per leg, per lot."""
    if not config.in_position.get(token):
        return
    ltp = config.last_ltp.get(token)
    if ltp is None:
        return
    entry_price = config.entry_price.get(token, 0)
    loss = max(0.0, (ltp - entry_price) * qty)   # short option: loss when price rises
    level_index = config.risk_level_index.get(token, 0)
    if level_index >= len(config.RISK_LOSS_LEVELS):
        return
    threshold = config.RISK_LOSS_LEVELS[level_index] * config.num_lots
    if loss >= threshold:
        reason = f'SL_LEVEL_{level_index + 1}'
        print(f'[RISK] {symbol} breached {reason} (loss={loss:.2f} >= {threshold:.2f}) -> exiting leg only')
        exit_leg(symbol, token, qty, reason=reason)


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
