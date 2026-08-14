"""
Expiry-day logic and hedge-strike calculation (deliverable 11).

Expiry detection priority (most reliable first):
  1. config.EXPIRY_DATES     - explicit override list (YYYY-MM-DD).
  2. Exchange scrip master    - the actual expiry date NSE embeds in the option
                                symbol (holiday-accurate by construction).
  3. Holiday-aware weekday    - fallback only if the master can't be read.

Because the exchange bakes the true expiry date into every trading symbol, we can
also decide "is this option expiring today?" straight from the symbol itself
(is_expiry_from_symbol / get_hedge_strike).
"""
from datetime import datetime, date, timedelta
import config


def _is_holiday(d):
    """True if d is a weekend or listed in config.HOLIDAYS."""
    if d.weekday() >= 5:                       # Sat/Sun
        return True
    return d.strftime('%Y-%m-%d') in getattr(config, 'HOLIDAYS', [])


def _actual_expiry_for_week(d):
    """
    Holiday-aware weekday fallback: start at the configured expiry weekday and
    walk BACK over holidays/weekends to the previous trading day.
    """
    exp = d + timedelta(days=(config.EXPIRY_WEEKDAY - d.weekday()))
    while _is_holiday(exp):
        exp -= timedelta(days=1)
    return exp


def is_expiry_day(current_date=None) -> bool:
    """Return True if `current_date` (default: today) is a NIFTY expiry day."""
    d = current_date or date.today()
    if isinstance(d, datetime):
        d = d.date()

    # 1) Explicit override list wins.
    if config.EXPIRY_DATES:
        return d.strftime('%Y-%m-%d') in config.EXPIRY_DATES

    # 2) Exchange scrip master (holiday-accurate). Only meaningful for "today".
    if current_date is None:
        try:
            import token_file
            return token_file.nearest_expiry_date() == d
        except Exception as e:
            print(f'[EXPIRY] scrip-master check failed ({e}); using weekday fallback')

    # 3) Holiday-aware weekday fallback.
    if _is_holiday(d):
        return False
    return d == _actual_expiry_for_week(d)


def is_expiry_from_symbol(symbol) -> bool:
    """
    Decide expiry straight from the exchange trading symbol, e.g.
    'NIFTY19MAY2623700CE' -> expiry 19-May-2026. True if that date is today.
    """
    name = config.INDEX_NAME
    expiry_str = symbol[len(name):len(name) + 7]        # e.g. 19MAY26
    expiry_date = datetime.strptime(expiry_str, '%d%b%y').date()
    return expiry_date == date.today()


def get_hedge_strike(symbol) -> str:
    """
    Given a full option symbol (e.g. 'NIFTY19MAY2623700CE'), return the hedge
    option suffix on the same expiry: +500/-500 on expiry day, +1000/-1000 else.
    e.g. non-expiry -> '24700CE' (CE) or '22700PE' (PE).
    """
    name = config.INDEX_NAME
    option_type = symbol[-2:]                           # CE / PE
    strike = int(symbol[len(name) + 7:-2])              # digits between expiry & CE/PE
    gap = config.HEDGE_GAP_EXPIRY if is_expiry_from_symbol(symbol) else config.HEDGE_GAP_NONEXPIRY
    hedge_strike = strike + gap if option_type == 'CE' else strike - gap
    return f'{hedge_strike}{option_type}'



def get_hedge_strikes(atm_strike, expiry):
    """
    Return (ce_hedge_strike, pe_hedge_strike).

    Expiry day  -> +/- 500 points.
    Non-expiry  -> +/- 1000 points.

    e.g. ATM 25000, expiry     -> (25500, 24500)
         ATM 25000, non-expiry -> (26000, 24000)
    """
    gap = config.HEDGE_GAP_EXPIRY if expiry else config.HEDGE_GAP_NONEXPIRY
    ce = int(atm_strike) + gap
    pe = int(atm_strike) - gap
    return ce, pe

