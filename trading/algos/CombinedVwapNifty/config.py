import os


def _env_flag(*names, default="false"):
    """
    Return a bool for the first environment variable in `names` that is set
    (non-empty), else parse `default`. Truthy values: 1/true/yes/y/on
    (case-insensitive). Accepting multiple `names` lets us tolerate typo
    aliases (e.g. BOT_DRY_RUN and BOT_DY_RUN).
    """
    val = None
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            val = v
            break
    if val is None:
        val = default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


# ============================ CREDENTIALS ============================
# Never commit real broker credentials -- fill these in directly on the
# target EC2 instance (not in git). See DoubleStraddelAlgo/config.py for
# the same convention.
clientid = 'xxx'
apikey = 'xxx'
mpin = 'xxx'
token = 'xxx'

# --- Telegram log forwarding ---
telegram_enabled = True
telegram_bot_token = ''   # e.g. '123456789:ABCdefGhIJKlmNoPQRstuVWxyz'
telegram_chat_id = ''     # e.g. '123456789' or '-1001234567890' for a group

# --- Central Strategy Monitoring System ---
monitoring_enabled = True
strategy_name = "CP_CV_Short_Straddle_Nifty"
server_name = "algo-server-2"
api_base_url = "https://centralized-algo-system-b52h.vercel.app"
agent = None   # populated at startup with the StrategyHeartbeatAgent instance

objconn = ''
sws = ''
slhit = {}                  # kept for parity with the reference project (unused directly;
                             # risk exits are driven by actual P&L - see risk_disabled/cum_loss)
qty = '65'                    # exchange quantity = lot_size * num_lots (set at startup)
lot_size = 65
num_lots = 1
orderbook = []               # kept as an (empty) list so `for i in orderbook` is always safe

tlv_data = {}
ohlc_data = {}
last_ltp = {}                 # token -> latest traded price (updated on every tick)
last_tick_time = {}           # token -> epoch seconds of the last tick received (stale detection)

# --- ATM lock (strike is fixed once at ATM_LOCK_TIME and reused all day) ---
ATM_LOCK_TIME = '09:30:00'
locked_strike = None
ce_symbol = None
ce_token = None
pe_symbol = None
pe_token = None

# --- Session timing ---
NO_NEW_ENTRY_AFTER = '15:00:00'     # no fresh entries / re-entries after this time
EOD_SQUARE_OFF_TIME = '15:15:00'    # unconditional square-off of any open leg

# --- Risk ladder: cumulative loss (Rupees, PER LOT) that exits only the losing leg ---
RISK_LOSS_LEVELS = [650.0, 1300.0, 2000.0]
MAX_REENTRIES_PER_LEG = len(RISK_LOSS_LEVELS)
REENTRY_COOLDOWN_SECONDS = 60

# Per-leg risk/position state, keyed by token (populated in manager.py at startup)
in_position = {}              # token -> bool
entry_price = {}              # token -> float (avg fill price of current open leg)
risk_level_index = {}         # token -> int, how many ladder levels already used today
risk_disabled = {}            # token -> bool, True once all ladder levels are used
cum_loss = {}                 # token -> float, cumulative realized loss today (this leg)
last_exit_time = {}           # token -> epoch seconds of the last SL exit (cooldown gate)
reentry_count = {}            # token -> int, how many RE-entries this leg has had today

# --- Order management ---
ORDER_LIMIT_OFFSET = 0.5              # ticks away from LTP for the initial limit price
ORDER_FILL_TIMEOUT_SECONDS = 8         # wait this long for a fill before repricing
ORDER_MAX_REPRICE = 5                  # max reprice/chase attempts before giving up
ORDER_REPRICE_STEP = 0.5               # price step per reprice attempt
RECONCILE_INTERVAL_SECONDS = 5         # how often saveorderbook() refreshes config.orderbook

# --- Data feed health ---
STALE_DATA_SECONDS = 15                # no tick for this long -> feed considered stale

# ============================ RUN MODE ============================
# Set True to run without placing real orders (paper trading).
# Defaults to paper trading (safe) when no env var is set.
# Set env var BOT_DRY_RUN=false to enable REAL orders (BOT_DY_RUN kept as a typo-tolerant alias).
DRY_RUN = _env_flag("BOT_DRY_RUN", "BOT_DY_RUN", default="true")

# --- Testing convenience (ALWAYS False for live trading) ---
# When True, the ATM lock happens immediately instead of waiting for
# ATM_LOCK_TIME, so the whole flow can be exercised outside market hours.
SKIP_ATM_TIME_GATE = False
