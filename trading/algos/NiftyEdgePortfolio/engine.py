"""
Scheduler. A 0.5-second loop that:
  * once per closed 1-min candle (about 2 s after it closes) calls on_minute() on
    every ENABLED strategy - each inside its own try/except, so one strategy's bug
    never stops the others
  * every ~2 s runs the intrabar disaster stops / broker-SL fill detection
  * every RECONCILE_INTERVAL runs the manual-trade guard
  * at the first candle of the day: executes exits deferred from yesterday's close
    and re-places resting broker SLs for overnight legs
  * after SHUTDOWN_AFTER saves state and returns (restarted next morning)
"""
from __future__ import annotations

import time
import traceback
from datetime import date, datetime

import config
from context import minute_index
import monitor
from live_context import LiveContext
from risk import guard
from state import store
from strategies import REGISTRY


def build_strategies():
    out = []
    for name, cfg in config.STRATEGIES.items():
        if not cfg.get("enabled"):
            continue
        params = getattr(config, name.upper())
        out.append(REGISTRY[name](params, lots=cfg["lots"], tag=cfg["tag"], lot_size=config.LOT_SIZE))
    print("[RUN MODE] enabled strategies:", [(s.name, s.lots) for s in out], "| DRY_RUN =", config.DRY_RUN)
    return out


def _expire_stale_legs(ctx):
    """Legs whose contract expired while the algo was not running: exchange-settled."""
    for leg in store.legs(ctx.st):
        if leg.expiry < ctx.today.isoformat():
            print(f"[WARNING] [ENGINE] {leg.strategy} {leg.symbol} expired {leg.expiry} while not managed - "
                  f"marked CLOSED (settled by the exchange). CHECK THE BROKER P&L.")
            leg.status = "CLOSED"
            leg.exit_reason = "EXPIRED_UNMANAGED"
            leg.exit_dt = f"{ctx.today} 09:15:00"
            store.put_leg(ctx.st, leg)


def _print_pnl_report(ctx):
    """Strategy-wise realised P&L for today and this month, written to app.log at the daily shutdown."""
    try:
        from state import journal
        t = ctx.today
        print("[REPORT] " + journal.format_summary(journal.summary(t.isoformat(), t.isoformat()), f"P&L by strategy {t} ({'PAPER' if config.DRY_RUN else 'LIVE'})"))
        print("[REPORT] " + journal.format_summary(journal.summary(t.replace(day=1).isoformat(), t.isoformat()), f"P&L by strategy {t:%B %Y} (month to date)"))
    except Exception as e:
        print(f"[WARNING] [REPORT] strategy P&L report failed: {e}")


def run():
    st = store.load()
    ctx = LiveContext(st)
    ctx.today = date.today()
    monitor.attach(ctx)
    strategies = build_strategies()
    _expire_stale_legs(ctx)
    import feed
    feed.subscribe([l.token for l in store.legs(st) if l.token])
    try:
        ctx.prewarm_chains()
    except Exception as e:
        print(f"[WARNING] [ENGINE] chain pre-subscribe failed (REST quotes will be used): {e}")
    print(f"[ENGINE] resumed {len(store.legs(st))} open legs from {config.STATE_FILE}: "
          f"{[(l.strategy, l.symbol, l.qty, l.entry_price) for l in store.legs(st)]}")
    last_min = None
    day_started = False
    warned_not_live = False
    last_intrabar = last_reconcile = 0.0
    try:
        from trading.common.utils import is_stop_requested
    except Exception:
        def is_stop_requested(_):
            return False

    while not config.kill_switch:
        now = datetime.now()
        if (now.hour, now.minute) >= config.SHUTDOWN_AFTER:
            print("[STARTUP] STRATEGY STOPPED - after close, state saved, stopping for the day")
            _print_pnl_report(ctx)
            break
        if is_stop_requested(config.ALGO_NAME):
            print("[STARTUP] STRATEGY STOPPED - stop requested by the control centre - stopping (open legs are kept and resumed on restart)")
            break

        mi = minute_index(now)
        done = mi - 1                                  # candle that has just closed
        live = True
        if 0 <= done <= config.LAST_MINUTE and now.second >= 2 and done != last_min:
            live = feed.market_live()
            if not live:
                # Holiday guard: the index has not moved today -> exchange closed (or no data).
                # Strategies are not called and NO order is sent; a pending ST10 flip or a
                # deferred exit simply waits until the market is confirmed live.
                if done >= 2 and not warned_not_live:
                    warned_not_live = True
                    print(f"[WARNING] [ENGINE] NIFTY index has not moved today (holiday or no market data?) - "
                          f"NO orders will be sent until it does. Calendar says trading day: {date.today()}")
                last_min = done
                time.sleep(0.5)
                continue
            if warned_not_live:
                warned_not_live = False
                print("[RUN MODE] market confirmed live - strategies resumed")
            if not day_started:
                day_started = True
                ctx.run_deferred_exits()
                ctx.refresh_broker_sls()
            if done % 30 == 0 or done == 0:
                try:
                    ctx.prewarm_chains()            # at the open (real spot) and re-centred if NIFTY moved 500+
                except Exception as e:
                    print(f"[WARNING] [ENGINE] chain pre-subscribe failed: {e}")
            ctx.minute = done
            for s in strategies:
                try:
                    s.on_minute(ctx)
                except Exception as e:
                    print(f"[ERROR] [ENGINE] {s.name} error at minute {done} (other strategies continue): {e}\n{traceback.format_exc()}")
            try:
                guard.after_minute(ctx)
            except Exception as e:
                print(f"[ERROR] [ENGINE] risk check error: {e}")
            ctx.persist_bars()
            last_min = done

        if 0 <= mi <= config.LAST_MINUTE and day_started and time.monotonic() - last_intrabar >= 2:
            last_intrabar = time.monotonic()
            try:
                guard.intrabar(ctx)
            except Exception as e:
                print(f"[ERROR] [ENGINE] intrabar check error: {e}")
        if 0 <= mi <= config.LAST_MINUTE and day_started and time.monotonic() - last_reconcile >= config.RECONCILE_INTERVAL:
            last_reconcile = time.monotonic()
            try:
                guard.reconcile(ctx)
            except Exception as e:
                print(f"[ERROR] [ENGINE] reconcile error: {e}")
        time.sleep(0.5)

    store.save(st)
    return ctx
