# ============================ CREDENTIALS ============================
# Never commit real broker credentials -- fill these in directly on the
# target EC2 instance (not in git). See DoubleStraddelAlgo/config.py for
# the same convention.
clientid = 'xxx'
apikey = 'xxx'
mpin = 'xxx'
token = 'xxx'

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
