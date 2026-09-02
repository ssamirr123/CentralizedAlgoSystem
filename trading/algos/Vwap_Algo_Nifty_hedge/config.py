# ============================ CREDENTIALS ============================
# Never commit real broker credentials -- fill these in directly on the
# target EC2 instance (not in git). See DoubleStraddelAlgo/config.py for
# the same convention.


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

# --- Telegram log forwarding ---
# Create a bot via @BotFather to get the token, and get your chat id
# (e.g. message the bot then visit https://api.telegram.org/bot<TOKEN>/getUpdates).
telegram_enabled = True
telegram_bot_token = ''   # e.g. '123456789:ABCdefGhIJKlmNoPQRstuVWxyz'
telegram_chat_id = ''     # e.g. '123456789' or '-1001234567890' for a group

# --- Central Strategy Monitoring System ---
# These settings are ONLY used by the heartbeat agent. They must never affect
# how the strategy trades. Set monitoring_enabled = False to disable entirely.
monitoring_enabled = True
strategy_name = "Mod_Vwap_Algo_Nifty_hedge"
server_name = "algo-server-1"
api_base_url = "https://centralized-algo-system-b52h.vercel.app"
agent = None   # populated at startup with the StrategyHeartbeatAgent instance

objconn = ''
sws = ''
slhit = {}
qty = '195'
orderbook = []   # kept as an (empty) list so `for i in orderbook` is always safe

tlv_data = {}
ohlc_data = {}
