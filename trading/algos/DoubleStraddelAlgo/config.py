import os


def _env_flag(*names, default="false"):
    """Return True if any of the given env vars is set to a truthy value.

    Accepts several aliases (handy for tolerating typos like BOT_DY_RUN) and
    falls back to `default` when none are present.
    """
    for name in names:
        val = os.getenv(name)
        if val is not None:
            return str(val).strip().lower() in ("1", "true", "yes", "on", "y")
    return str(default).strip().lower() in ("1", "true", "yes", "on", "y")


# ============================ RUN MODE ============================
# Set True to run without placing real orders (paper trading).
# Enable via env var BOT_DRY_RUN=true (BOT_DY_RUN kept as a typo-tolerant alias).
DRY_RUN = _env_flag("BOT_DRY_RUN", "BOT_DY_RUN", default="true")

# ============================ CREDENTIALS ============================
#Samir


def _angel_creds() -> dict:
    """AngelOne credentials from the environment (Stage 20). Falls back to a
    git-ignored trading/.env at the repo root so a strategy box that does not
    inject them via systemd still works. Real values live there or in AWS
    Secrets Manager -> env -- NEVER in this tracked file."""
    import os
    from pathlib import Path as _P

    _envf = _P(__file__).resolve().parents[3] / "trading" / ".env"
    if _envf.is_file():
        for _raw in _envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            _raw = _raw.strip()
            if _raw and not _raw.startswith("#") and "=" in _raw:
                _k, _, _v = _raw.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

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


# ============================ CREDENTIALS ============================
# Stage 20: sourced from the environment. NEVER hard-code real values here.
_ANGEL = _angel_creds()
clientid = _ANGEL["clientid"]
apikey = _ANGEL["apikey"]
mpin = _ANGEL["mpin"]
token = _ANGEL["token"]

# ============================ INSTRUMENT ============================
INDEX_NAME   = 'NIFTY'
INDEX_TOKEN  = '99926000'      # NIFTY spot token (NSE)
INDEX_EXCH   = 'NSE'
OPT_EXCH     = 'NFO'
STRIKE_STEP  = 50
LOT_QTY      = '65'            # 1 lot NIFTY = 65 (current lot size)

# ============================ TIMINGS (IST, 24h) ============================
HEDGE_ENTRY   = (10, 20)
MORNING_ENTRY = (10, 25)
MORNING_EXIT  = (14, 14)
AFT_ENTRY     = (14, 16)
FINAL_EXIT    = (15, 25)

# ============================ SL / TARGET (points) ============================
SL_POINTS     = 25
TARGET_POINTS = 50

# ============================ HEDGE DISTANCE (points) ============================
HEDGE_GAP_EXPIRY     = 500
HEDGE_GAP_NONEXPIRY  = 1000

# ============================ ORDER / RETRY ============================
ORDER_MAX_RETRIES = 5
ORDER_RETRY_DELAY = 2          # seconds between retries
LIMIT_SLIPPAGE    = 0.50       # rupees to walk the limit toward market on each retry
PENDING_TIMEOUT   = 5          # seconds a limit order may stay pending before action
PENDING_ACTION    = 'MODIFY'   # 'MODIFY' | 'CANCEL' | 'MARKET' once pending timeout hit
                               # 'MODIFY' keeps entries LIMIT-only (re-prices toward market)
ALLOW_MARKET_EMERGENCY = True  # market orders permitted ONLY in emergency square-off

# ============================ RISK ============================
DAILY_MAX_LOSS      = 20000  # absolute rupees; if day MTM <= -this -> kill switch
RECONCILE_INTERVAL  = 120      # seconds between position/order reconciliation

# ============================ EXPIRY CALENDAR (configurable) ============================
# Explicit NIFTY weekly expiry dates (YYYY-MM-DD). Populate from the NSE calendar.
EXPIRY_DATES = [
    # '2026-07-30', '2026-08-06', '2026-08-13', '2026-08-20', '2026-08-27',
]
# Fallback used only when EXPIRY_DATES is empty: weekday number (Mon=0, Tue=1 ... Sun=6).
# NIFTY weekly expiry is now Tuesday.
EXPIRY_WEEKDAY = 1
# NSE trading holidays (YYYY-MM-DD). If the expiry weekday (Tuesday) is a holiday,
# expiry shifts BACK to the previous trading day (e.g. Monday). Keep this updated.
HOLIDAYS = [
    # '2026-08-15',  # Independence Day (example)
    # '2026-10-20',  # Diwali (example)
]

# ============================ STATE / REPORT ============================
STATE_FILE   = 'state.json'
CSV_PREFIX   = 'trades'

# ============================ MONITOR (reused heartbeat agent) ============================
# Set False to fully disable the strategy_agent heartbeat (no agent, no thread,
# all monitor.report()/monitor.stop() calls become no-ops).
monitoring_enabled = True
strategy_name = "Nifty_Hedged_Double_Straddle"
server_name   = "algo-server-1"
api_base_url  = "http://127.0.0.1:8000"
agent = None

# --- Telegram log forwarding ---
telegram_enabled   = True
telegram_bot_token = ''
telegram_chat_id   = ''

# ============================ SHARED RUNTIME STATE ============================
objconn     = None
sws         = None
ltp         = {}      # {token(str): last_traded_price(float)}
orderbook   = []      # cached order book (list of dicts)
positions   = []      # cached position book (list of dicts)
kill_switch = False   # set True by risk guard / emergency square-off
state       = None    # live strategy state dict (state.store) - set by engine.run()

