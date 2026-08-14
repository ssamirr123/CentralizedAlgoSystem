"""
Scheduler / orchestration engine.

Runs a 1-second loop that fires each timed action exactly once:
    10:15 hedge entry
    10:25 morning straddle entry
    14:14 morning time exit (hedge stays active)
    14:16 afternoon straddle entry
    15:25 final exit (square off shorts + hedge, cancel all, stop)
The risk guard runs in parallel and can trip an emergency square-off any time.
"""
import config
import time
from datetime import datetime
from strategy import hedge, straddle
from risk import guard
from state.store import load
from broker import orders
from report.csv_report import write_report
import monitor


def _hit(now, hhmm):
    return now.hour == hhmm[0] and now.minute == hhmm[1]


def run():
    state = load()                 # crash recovery
    config.state = state           # expose to monitor.py for PNL/MTM computation
    guard.start_guard(state)       # risk daemon (max loss + reconciliation)
    done = set()

    # Skip any already-completed steps after a restart (duplicate protection),
    # and resume monitor threads for legs that were open when we crashed.
    if state.get('hedge_active'):
        done.add('hedge')
    if state.get('morning', {}).get('entered'):
        done.add('morn')
        straddle.resume_monitors(state, 'morning')
    if state.get('afternoon', {}).get('entered'):
        done.add('aft')
        straddle.resume_monitors(state, 'afternoon')

    while True:
        if config.kill_switch:
            print('[ENGINE] kill switch active - ending run')
            break
        now = datetime.now()
        try:
            if _hit(now, config.HEDGE_ENTRY) and 'hedge' not in done:
                hedge.enter_hedge(state)
                done.add('hedge')
                monitor.report('RUNNING')

            if _hit(now, config.MORNING_ENTRY) and 'morn' not in done:
                if state.get('hedge_active'):
                    straddle.enter_straddle(state, 'morning')
                else:
                    print('[ENGINE] hedge not active - skipping morning straddle entry (unhedged risk)')
                done.add('morn')
                monitor.report('RUNNING')

            if _hit(now, config.MORNING_EXIT) and 'morn_exit' not in done:
                print('[ENGINE] 14:14 morning time exit (hedge stays active)')
                straddle.time_exit_straddle(state, 'morning')
                straddle.print_session_pnl(state, 'morning')
                done.add('morn_exit')
                monitor.report('RUNNING')

            if _hit(now, config.AFT_ENTRY) and 'aft' not in done:
                if state.get('hedge_active'):
                    straddle.enter_straddle(state, 'afternoon')
                else:
                    print('[ENGINE] hedge not active - skipping afternoon straddle entry (unhedged risk)')
                done.add('aft')
                monitor.report('RUNNING')

            if _hit(now, config.FINAL_EXIT) and 'final' not in done:
                print('[ENGINE] 15:25 final exit - flatten shorts + hedge, cancel all')
                straddle.time_exit_straddle(state, 'afternoon')
                straddle.print_session_pnl(state, 'afternoon')
                hedge.exit_hedge(state)
                orders.cancel_all_pending()
                done.add('final')
                write_report(state)
                monitor.report('STOPPED')
                break
        except Exception as e:
            print(f'[ENGINE] error (continuing): {e}')
        time.sleep(1)

    write_report(state)

