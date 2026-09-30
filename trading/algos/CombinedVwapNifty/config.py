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


# ============================ BROKER SELECTION ============================
# Supported brokers: 'SHOONYA' or 'ANGELONE' (defaults to SHOONYA)
BROKER = os.environ.get("BROKER", "ANGELONE").strip().upper()


def _read_env_file():
    from pathlib import Path as _P
    _envf = _P(__file__).resolve().parents[3] / "trading" / ".env"
    if _envf.is_file():
        for _raw in _envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            _raw = _raw.strip()
            if _raw and not _raw.startswith("#") and "=" in _raw:
                _k, _, _v = _raw.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))


_read_env_file()


def _angel_creds() -> dict:
    """AngelOne credentials from the environment."""
    def _g(*names):
        for _n in names:
            _val = os.environ.get(_n, "").strip()
            if _val:
                return _val
        return ""

    return {
        "clientid": _g("ANGELONE_CLIENT_ID"),
        "apikey": _g("ANGELONE_API_KEY"),
        "mpin": _g("ANGELONE_MPIN", "ANGELONE_PASSWORD"),
        "token": _g("ANGELONE_TOTP_SECRET"),
    }


def _shoonya_creds() -> dict:
    """Shoonya (Finvasia) credentials from the environment."""
    def _g(*names, default=""):
        for _n in names:
            _val = os.environ.get(_n, "").strip()
            if _val:
                return _val
        return default

    user_id = _g("SHOONYA_USER_ID", "SHOONYA_CLIENT_ID", "FINVASIA_USER_ID")
    return {
        "user_id": user_id,
        "password": _g("SHOONYA_PASSWORD", "FINVASIA_PASSWORD"),
        "totp_secret": _g("SHOONYA_TOTP_SECRET", "SHOONYA_TOKEN", "FINVASIA_TOTP_SECRET"),
        "vendor_code": _g("SHOONYA_VENDOR_CODE", "FINVASIA_VENDOR_CODE", default=f"{user_id}_U" if user_id else ""),
        "api_key": _g("SHOONYA_API_KEY", "SHOONYA_API_SECRET", "FINVASIA_API_KEY"),
        "imei": _g("SHOONYA_IMEI", default="abc1234"),
    }


# ============================ CREDENTIALS ============================
_ANGEL = _angel_creds()
clientid = _ANGEL["clientid"]
apikey = _ANGEL["apikey"]
mpin = _ANGEL["mpin"]
token = _ANGEL["token"]

_SHOONYA = _shoonya_creds()
shoonya_user_id = _SHOONYA["user_id"]
shoonya_password = _SHOONYA["password"]
shoonya_totp_secret = _SHOONYA["totp_secret"]
shoonya_vendor_code = _SHOONYA["vendor_code"]
shoonya_api_key = _SHOONYA["api_key"]
shoonya_imei = _SHOONYA["imei"]

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
                             # risk exits are driven by actual P&L - see combined_risk_level_index/cum_loss)
qty = '65'                    # exchange quantity = lot_size * num_lots (set at startup)
lot_size = 65
num_lots = 1
orderbook = []               # kept as an (empty) list so `for i in orderbook` is always safe

tlv_data = {}
ohlc_data = {}
last_ltp = {}                 # token -> latest traded price (updated on every tick)
last_volume = {}              # token -> latest traded volume
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
EOD_SQUARE_OFF_TIME = '15:25:00'    # unconditional square-off of any open leg

# --- Risk ladder: COMBINED CE+PE premium loss (Rupees, PER LOT), not each
# leg's own loss in isolation. Rule 1 (650) and Rule 2 (1300) exit only
# whichever leg is losing more; Rule 3 (2000) exits both and ends the day.
RISK_LOSS_LEVELS = [650.0, 1300.0, 2000.0]
REENTRY_COOLDOWN_SECONDS = 60

# Shared (not per-leg) combined-loss ladder state, reset at the start of
# each day in manager.py.
combined_risk_level_index = 0   # 0 -> watching for 650, 1 -> 1300, 2 -> 2000
day_stopped = False             # True once Rule 3 fires: no more entries today

# Per-leg risk/position state, keyed by token (populated in manager.py at startup)
in_position = {}              # token -> bool
entry_price = {}              # token -> float (avg fill price of current open leg)
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
