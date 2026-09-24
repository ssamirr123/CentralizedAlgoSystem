import os
import io
import time
import zipfile
import requests
import pandas as pd
from datetime import datetime
import config

from pathlib import Path
import config

_BASE_DIR = Path(__file__).resolve().parent
ANGEL_TOKEN_FILE = _BASE_DIR / 'nifty_token.csv'
SHOONYA_TOKEN_FILE = _BASE_DIR / 'shoonya_nifty_token.csv'

ANGEL_SCRIP_MASTER_URL = 'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json'
SHOONYA_NFO_SYMBOLS_URL = 'https://api.shoonya.com/NFO_symbols.txt.zip'

REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
REQUEST_TIMEOUT = (10, 90)


def download_token_angel():
    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(ANGEL_SCRIP_MASTER_URL, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            token = pd.DataFrame(resp.json())
            break
        except Exception as exc:
            last_exc = exc
            print(f"download_token_angel: attempt {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)
    else:
        if ANGEL_TOKEN_FILE.exists():
            print(f"download_token_angel: all attempts failed ({last_exc}); reusing existing {ANGEL_TOKEN_FILE.name}")
            return
        raise RuntimeError(f"download_token_angel: all attempts failed: {last_exc}") from last_exc

    token = token.loc[(token.exch_seg == 'NFO') & (token.name == 'NIFTY') & (token.instrumenttype == 'OPTIDX')]
    token = token.drop(['instrumenttype', 'exch_seg', 'tick_size', 'name'], axis=1)
    token.to_csv(ANGEL_TOKEN_FILE, index=False)
    print(f"[OK] Downloaded and cached {len(token)} Angel One NIFTY options to {ANGEL_TOKEN_FILE.name}")


def download_token_shoonya():
    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(SHOONYA_NFO_SYMBOLS_URL, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                with z.open('NFO_symbols.txt') as f:
                    df = pd.read_csv(f)
            break
        except Exception as exc:
            last_exc = exc
            print(f"download_token_shoonya: attempt {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)
    else:
        if SHOONYA_TOKEN_FILE.exists():
            print(f"download_token_shoonya: all attempts failed ({last_exc}); reusing existing {SHOONYA_TOKEN_FILE.name}")
            return
        raise RuntimeError(f"download_token_shoonya: all attempts failed: {last_exc}") from last_exc

    df = df.loc[(df['Exchange'] == 'NFO') & (df['Symbol'] == 'NIFTY') & (df['Instrument'] == 'OPTIDX')]
    keep_cols = [c for c in ['TradingSymbol', 'Token', 'LotSize', 'Expiry', 'OptionType', 'StrikePrice'] if c in df.columns]
    df = df[keep_cols]
    df.to_csv(SHOONYA_TOKEN_FILE, index=False)
    print(f"[OK] Downloaded and cached {len(df)} Shoonya NIFTY options to {SHOONYA_TOKEN_FILE.name}")


def download_token():
    if config.BROKER == "SHOONYA":
        download_token_shoonya()
    else:
        download_token_angel()


def token_nifty_angel(strike):
    """Returns (symbol, token, lotsize) for the nearest front expiry using Angel One master."""
    if not ANGEL_TOKEN_FILE.exists():
        download_token_angel()
    df = pd.read_csv(ANGEL_TOKEN_FILE)
    expiries = df[['expiry']].drop_duplicates().reset_index(drop=True)
    expiries['expiry_dt'] = [datetime.strptime(e, '%d%b%Y') for e in expiries['expiry']]
    expiries = expiries.sort_values(by='expiry_dt').reset_index(drop=True)
    symbol = 'NIFTY' + (expiries['expiry'][0])[:5] + (expiries['expiry'][0])[5:][2:] + strike
    row = df.loc[df["symbol"] == symbol]
    if row.empty:
        raise ValueError(f"Angel One contract not found for strike {strike} (symbol {symbol})")
    token = row["token"].values[0]
    lotsize = int(row["lotsize"].values[0]) if "lotsize" in df.columns else 65
    return symbol, str(token), lotsize


def token_nifty_shoonya(strike):
    """
    `strike` is a string like '24000CE' / '24000PE'.
    Returns (symbol, token, lotsize) for the nearest front expiry using Shoonya master.
    """
    if not SHOONYA_TOKEN_FILE.exists():
        download_token_shoonya()
    df = pd.read_csv(SHOONYA_TOKEN_FILE)
    opt_type = 'CE' if 'CE' in strike.upper() else 'PE'
    strike_val = float(strike.upper().replace(opt_type, ''))

    df['expiry_dt'] = pd.to_datetime(df['Expiry'], format='%d-%b-%Y')
    today = pd.Timestamp.now().normalize()
    df_active = df[df['expiry_dt'] >= today]
    if df_active.empty:
        df_active = df  # fallback if file has earlier dates

    front_expiry = df_active.sort_values('expiry_dt')['Expiry'].iloc[0]
    match = df_active[(df_active['Expiry'] == front_expiry) &
                      (df_active['OptionType'].str.upper() == opt_type) &
                      ((df_active['StrikePrice'].astype(float) - strike_val).abs() < 0.01)]
    if match.empty:
        raise ValueError(f"Shoonya contract not found for strike {strike} on expiry {front_expiry}")
    row = match.iloc[0]
    symbol = str(row['TradingSymbol'])
    token = str(row['Token'])
    lotsize = int(row['LotSize']) if 'LotSize' in row and pd.notna(row['LotSize']) else 65
    return symbol, token, lotsize


def token_nifty(strike):
    """
    `strike` is a string like '24000CE' / '24000PE'.
    Returns (symbol, token, lotsize) for the nearest (front) expiry.
    """
    if config.BROKER == "SHOONYA":
        return token_nifty_shoonya(strike)
    return token_nifty_angel(strike)


