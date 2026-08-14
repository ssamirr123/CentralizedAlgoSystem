import pandas as pd
from datetime import datetime
import config


def download_token():
    """Download the NFO scrip master for the index and cache the option chain to CSV."""
    df = pd.read_json('https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json')
    df = df.loc[(df.exch_seg == 'NFO') & (df.name == config.INDEX_NAME) & (df.instrumenttype == 'OPTIDX')]
    df = df.drop(['strike', 'lotsize', 'instrumenttype', 'exch_seg', 'tick_size', 'name'], axis=1)
    df.to_csv('nifty_token.csv', index=False)
    print(f'[TOKEN] scrip master saved ({len(df)} option rows)')


def _nearest_expiry_prefix(df):
    exp = df[['expiry']].drop_duplicates().reset_index(drop=True)
    exp['dt'] = [datetime.strptime(e, '%d%b%Y') for e in exp['expiry']]
    exp = exp.sort_values('dt').reset_index(drop=True)
    e = exp['expiry'][0]                       # e.g. 30JUL2026
    return e[:5] + e[5:][2:]                    # -> 30JUL26


def nearest_expiry_date():
    """
    Return the nearest (current-week) NIFTY expiry as a date, straight from the
    exchange scrip master. This is holiday-accurate by construction because NSE
    publishes the actual expiry date in the master (no weekday guessing).
    """
    df = pd.read_csv('nifty_token.csv')
    today = datetime.today().date()
    dates = sorted({datetime.strptime(e, '%d%b%Y').date() for e in df['expiry'].unique()})
    future = [d for d in dates if d >= today]
    return future[0] if future else dates[-1]



def token_nifty(strike):
    """
    Resolve a full option strike suffix (e.g. '25000CE' / '24000PE') for the nearest
    expiry to its (tradingsymbol, symboltoken).
    """
    df = pd.read_csv('nifty_token.csv')
    prefix = _nearest_expiry_prefix(df)
    symbol = config.INDEX_NAME + prefix + str(strike)
    token = df.loc[df['symbol'] == symbol, 'token'].values[0]
    return symbol, str(token)

