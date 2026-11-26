"""
NiftyEdgePortfolio configuration.

One process runs several independent NIFTY option-selling strategies (plug-ins
under strategies/). Each strategy has its own ENABLED flag, lots, order tag,
state section and P&L. Rules are the ones validated in the Nov 2026 backtests
(NIFTY_DTE_EDGE_RESEARCH); see README.md before changing any number here.
"""
import os
from pathlib import Path


def _env_flag(*names, default="false"):
    for name in names:
        val = os.getenv(name)
        if val is not None:
            return str(val).strip().lower() in ("1", "true", "yes", "on", "y")
    return str(default).strip().lower() in ("1", "true", "yes", "on", "y")


# ============================ RUN MODE ============================
# Paper by default. Real orders ONLY when NEP_LIVE=true is set (environment or
# trading/.env). This algo deliberately does NOT read BOT_DRY_RUN, so switching
# another algo (DoubleStraddelAlgo / CombinedVwapNifty) to live can never switch
# this one to live by accident.
ALGO_NAME = "NiftyEdgePortfolio"   # must equal the name registered in the control centre


# ============================ CREDENTIALS ============================
def _angel_creds() -> dict:
    """From the environment, falling back to the git-ignored trading/.env.
    NEVER hard-code real values in this file."""
    envf = Path(__file__).resolve().parents[3] / "trading" / ".env"
    if envf.is_file():
        for raw in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                k, _, v = raw.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    def g(*names):
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return ""

    return {"clientid": g("ANGELONE_CLIENT_ID"), "apikey": g("ANGELONE_API_KEY"),
            "mpin": g("ANGELONE_MPIN", "ANGELONE_PASSWORD"), "token": g("ANGELONE_TOTP_SECRET")}


_ANGEL = _angel_creds()              # also loads trading/.env into os.environ
DRY_RUN = not _env_flag("NEP_LIVE", default="false")
clientid = _ANGEL["clientid"]
apikey = _ANGEL["apikey"]
mpin = _ANGEL["mpin"]
token = _ANGEL["token"]

# ============================ INSTRUMENT ============================
INDEX_NAME = "NIFTY"
INDEX_TOKEN = "99926000"     # NIFTY 50 spot (NSE)
INDEX_EXCH = "NSE"
OPT_EXCH = "NFO"
STRIKE_STEP = 50
LOT_SIZE = 65                # NIFTY lot size (Jan 2026 onwards) - verify before live
FREEZE_QTY = 1800            # exchange freeze quantity per order
CHAIN_HALF_WIDTH = 2000      # subscribe strikes ATM +/- this for chain scans

# ============================ STRATEGIES ============================
# Live plan (Algo Portfolio page, 26 Nov 2026): 1 lot each on Rs12.5L.
# Rollout: strangle60 first, then otm4_1day + btst9, then st10 - flip "enabled" one at a time.
STRATEGIES = {
    "strangle60": {"enabled": True,  "lots": 1, "tag": "NEP_S60",   "margin_per_lot": 125000},
    "otm4_1day":  {"enabled": True, "lots": 1, "tag": "NEP_OTM4",  "margin_per_lot": 200000},
    "btst9":      {"enabled": True, "lots": 1, "tag": "NEP_BTST9", "margin_per_lot": 100000},
    "st10":       {"enabled": True, "lots": 1, "tag": "NEP_ST10",  "margin_per_lot": 150000},
    "stbt":       {"enabled": True, "lots": 1, "tag": "NEP_STBT",  "margin_per_lot": 200000},   # added 26 Nov 2026 (P27); paper first
}

# Minute index m: 0 = 09:15 candle ... 374 = 15:29 candle.
# "Act at minute m" = decided on the CLOSE of candle m; the live order goes out
# ~1 s after that candle closes. All stops are checked on 1-min CLOSES (as tested).

# Rs60 expiry-day strangle (backtest P10, exit 15:15)
STRANGLE60 = {
    "select_minute": 4,       # 09:19 close: ATM from spot, strikes nearest Rs60
    "entry_minute": 5,        # 09:20 close: sell both legs
    "exit_minute": 360,       # 15:15 close: time exit
    "target_premium": 60.0,
    "max_premium_diff": 18.0, # skip the day if a leg's best premium is > Rs18 from 60
    "sl_mult": 2.0,           # SL when a 1-min close >= 2 x entry premium
    "disaster_mult": 3.0,     # intrabar safety stop (live only) + resting broker SL
}

# OTM4 1-day strangle (backtest P14 OTM4_1day)
OTM4_1DAY = {
    "select_minute": 4,       # DTE1 09:19 close: ATM from spot
    "entry_minute": 5,        # 09:20 close: sell CE ATM+200 and PE ATM-200 (legs independent)
    "offset": 200,
    "exit_minute": 360,       # expiry day 15:15 close
    "sl_mult": 1.5,           # SL 50% on 1-min closes, overnight included
    "disaster_mult": 2.25,
}

# BTST 9 (backtest P12/P14 BTST9)
BTST9 = {
    "ref_minute": 5,          # 09:20 close: per side, strike with premium closest to Rs100
    "target_premium": 100.0,
    "fall": 0.5,              # sell when a close <= 50% of the 09:20 premium (one trade per side per day)
    "last_trigger_minute": 373,
    "sl_mult": 1.5,
    "disaster_mult": 2.25,
    "exit_minute": 5,         # next session 09:20 close
}

# ST10 (backtest P12 ST10 / P13 H0): 10-min Supertrend(10,3) on NIFTY spot, naked
ST10 = {
    "bar_minutes": 10,
    "atr_period": 10,
    "multiplier": 3.0,
    "roll_minute": 5,         # expiry day 09:20: roll a position in the expiring weekly
    "history_days": 20,       # live warm-up history for the indicator
    "enter_on_start": False,  # False: wait for the next flip after a fresh start (no open position)
    "disaster_mult": None,    # tested naked; set e.g. 3.0 for a catastrophe stop (changes the strategy)
}

# STBT "Sell Today Buy Tomorrow" (backtest P27, SELL reading): 09:15-11:15 range breakout, sell 200-ITM option against it
STBT = {
    "range_end_minute": 120,  # range = minutes 0..119 (09:15-11:14); scan from the 11:15 candle
    "last_entry_minute": 360, # 15:15 close is the last candle that can trigger
    "min_range_bars": 100,    # skip the day if the morning bars are missing
    "itm_points": 200,        # UP -> SELL PE ATM+200, DOWN -> SELL CE ATM-200
    "sl_pct": 0.005,          # spot 0.5% against the trade (1-min high/low), overnight included
    "exit_minute": 20,        # next session 09:35 close
    "max_late_minutes": 2,    # a breakout found later than this (restart/lag) is skipped
    "disaster_mult": 2.0,     # broker-resting safety stop on the option (spot SL normally fires near ~1.4-1.7x)
}

# ============================ ORDERS ============================
PRODUCT_TYPE = "CARRYFORWARD"  # NRML: no broker auto square-off; needed for overnight legs
ORDER_MAX_RETRIES = 5
ORDER_RETRY_DELAY = 2
LIMIT_SLIPPAGE = 0.50          # Rs the limit walks toward the market on each re-price
FILL_TIMEOUT = 20              # seconds to wait for a fill (re-pricing every PENDING_TIMEOUT)
PENDING_TIMEOUT = 4
ALLOW_MARKET_EMERGENCY = True  # market orders only for emergency square-off
PLACE_BROKER_SL = True         # resting STOPLOSS_LIMIT BUY at the disaster level
SL_LIMIT_BUFFER = 2.0
PAPER_SLIPPAGE = 0.5           # DRY_RUN fill = LTP -/+ this (backtest assumption)

# ============================ RISK ============================
CAPITAL = 1_250_000
DAILY_LOSS_LIMIT = 25_000      # day P&L <= -this -> DAILY_LOSS_ACTION
DAILY_LOSS_ACTION = "BLOCK_NEW"  # "BLOCK_NEW" (keep open legs, no new entries) | "FLATTEN"
PORTFOLIO_DD_STOP = 140_000    # drawdown from equity peak (Monte Carlo 95%) -> flatten + HALT
MARGIN_CHECK = True
MARGIN_BUFFER = 1.10           # need available margin >= estimate x this
RECONCILE_INTERVAL = 60        # seconds between broker reconciliation runs
MANUAL_GUARD = {
    "enabled": True,
    "auto_exit": False,        # True = close positions this algo did not open (never in DRY_RUN).
                               # Turn on ONLY when no other algo trades NIFTY options on this account.
    "grace_seconds": 120,      # a mismatch must persist this long before action
}

# ============================ SCHEDULE ============================
MARKET_OPEN = (9, 15)
LAST_MINUTE = 374              # 15:29 candle
SHUTDOWN_AFTER = (15, 31)      # save state, write report, exit (restarted next morning)

HOLIDAYS = []                  # extra non-trading days (YYYY-MM-DD) on top of trading.common.market_calendar

# ============================ FILES ============================
ALGO_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = ALGO_DIR / "runtime"       # git-ignored
STATE_FILE = RUNTIME_DIR / "state.json"  # multi-day (overnight legs survive restarts)
JOURNAL_FILE = RUNTIME_DIR / "journal.csv"
HALT_FILE = RUNTIME_DIR / "HALTED"       # created by the portfolio stop; delete by hand to resume

# ============================ MONITOR / TELEGRAM ============================
monitoring_enabled = True
telegram_enabled = True
telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

# ============================ SHARED RUNTIME STATE ============================
objconn = None
sws = None
ltp = {}
orderbook = []
positions = []
kill_switch = False
