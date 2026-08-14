"""
Risk management (deliverables: daily max-loss, emergency square-off, reconciliation).

A daemon loop:
    - recomputes day MTM from the broker position book,
    - trips the kill switch + emergency square-off if MTM <= -DAILY_MAX_LOSS,
    - periodically refreshes the order book (order/position reconciliation).
"""
import config
import time
import threading
from broker import orders
from strategy.hedge import exit_hedge
from strategy.straddle import time_exit_straddle


def day_mtm():
    """Current day mark-to-market (realised + unrealised) from the position book."""
    orders.refresh_positions()
    mtm = 0.0
    for p in (config.positions or []):
        try:
            realised = float(p.get('realised') or 0)
            unrealised = float(p.get('unrealised') or 0)
            mtm += (realised + unrealised) if (realised or unrealised) else float(p.get('pnl') or 0)
        except Exception:
            try:
                mtm += float(p.get('pnl') or 0)
            except Exception:
                pass
    return round(mtm, 2)


def emergency_square_off(state, reason='EMERGENCY'):
    """Flatten everything: shorts (market), hedge (market), cancel all pending, stop."""
    print(f'[EMERGENCY] {reason} - squaring off ALL positions')
    config.kill_switch = True
    for s in ('morning', 'afternoon'):
        if state.get(s, {}).get('entered'):
            time_exit_straddle(state, s, emergency=True)
    exit_hedge(state, emergency=True)
    orders.cancel_all_pending()
    print('[EMERGENCY] flat - trading stopped for the day')


def start_guard(state):
    """Start the background risk daemon (max-loss kill switch + reconciliation)."""
    def _loop():
        while not config.kill_switch:
            try:
                mtm = day_mtm()
                if mtm <= -abs(config.DAILY_MAX_LOSS):
                    print(f'[RISK] day MTM {mtm} <= -{config.DAILY_MAX_LOSS} -> KILL SWITCH')
                    emergency_square_off(state, reason='MAX_LOSS')
                    return
                orders.refresh_orderbook()   # order reconciliation
            except Exception as e:
                print(f'[RISK] guard loop error (ignored): {e}')
            time.sleep(config.RECONCILE_INTERVAL)

    threading.Thread(target=_loop, daemon=True).start()
    print('[RISK] guard daemon started')

