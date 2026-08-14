"""
End-of-day CSV trade report (deliverable 13).
"""
import csv
from datetime import date
import config
from risk.guard import day_mtm

FIELDS = [
    'date', 'leg_type', 'session', 'symbol', 'strike', 'option_type', 'side', 'qty',
    'entry_time', 'entry_price', 'exit_time', 'exit_price', 'exit_reason',
    'pnl_points', 'pnl_amount',
]


def _pts(exit_p, entry_p, side):
    if exit_p is None or entry_p is None:
        return ''
    return round((entry_p - exit_p) if side == 'SELL' else (exit_p - entry_p), 2)


def _amount(points, qty):
    try:
        return round(float(points) * int(qty), 2)
    except Exception:
        return ''


def _rows(state):
    d = date.today().strftime('%Y-%m-%d')
    rows = []

    # Hedge legs (BUY).
    h = state.get('hedge', {})
    for k, otype in (('ce', 'CE'), ('pe', 'PE')):
        if k in h:
            pts = _pts(h[k].get('exit'), h[k].get('entry'), 'BUY')
            rows.append({
                'date': d, 'leg_type': 'HEDGE', 'session': 'ALL', 'symbol': h[k]['sym'],
                'strike': h[k].get('strike'), 'option_type': otype, 'side': 'BUY',
                'qty': config.LOT_QTY,
                'entry_time': h[k].get('entry_time'), 'entry_price': h[k].get('entry'),
                'exit_time': h[k].get('exit_time'), 'exit_price': h[k].get('exit'),
                'exit_reason': 'Day Close',
                'pnl_points': pts, 'pnl_amount': _amount(pts, config.LOT_QTY),
            })

    # Short straddle legs (SELL).
    for s in ('morning', 'afternoon'):
        sess = state.get(s, {})
        for k, otype in (('ce', 'CE'), ('pe', 'PE')):
            if k in sess:
                leg = sess[k]
                pts = _pts(leg.get('exit'), leg.get('entry'), 'SELL')
                rows.append({
                    'date': d, 'leg_type': 'SHORT', 'session': s, 'symbol': leg['sym'],
                    'strike': sess.get('atm'), 'option_type': otype, 'side': 'SELL',
                    'qty': config.LOT_QTY,
                    'entry_time': leg.get('entry_time'), 'entry_price': leg.get('entry'),
                    'exit_time': leg.get('exit_time'), 'exit_price': leg.get('exit'),
                    'exit_reason': leg.get('exit_reason'),
                    'pnl_points': pts, 'pnl_amount': _amount(pts, config.LOT_QTY),
                })
    return rows


def write_report(state):
    fn = f'{config.CSV_PREFIX}_{date.today():%Y-%m-%d}.csv'
    try:
        rows = _rows(state)
        with open(fn, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, '') for k in FIELDS})
        total = sum(r['pnl_amount'] for r in rows if isinstance(r['pnl_amount'], (int, float)))
        # Per-session short-straddle P&L (e.g. the morning trade).
        for s in ('morning', 'afternoon'):
            sub = sum(r['pnl_amount'] for r in rows
                      if r['session'] == s and isinstance(r['pnl_amount'], (int, float)))
            print(f'[REPORT] {s} P&L={round(sub, 2)}')
        print(f'[REPORT] written {fn} | est P&L={round(total, 2)} | broker day MTM={day_mtm()}')
    except Exception as e:
        print(f'[REPORT] write failed: {e}')

