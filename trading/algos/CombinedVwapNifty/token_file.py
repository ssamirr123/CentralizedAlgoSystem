import pandas as pd
from datetime import datetime


def download_token():
    token = pd.read_json('https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json')
    token = token.loc[(token.exch_seg == 'NFO') & (token.name == 'NIFTY') & (token.instrumenttype == 'OPTIDX')]
    token = token.drop(['instrumenttype', 'exch_seg', 'tick_size', 'name'], axis=1)
    token.to_csv('nifty_token.csv', index=False)


def token_nifty(strike):
    """
    `strike` is a string like '24000CE' / '24000PE'.
    Returns (symbol, token, lotsize) for the nearest (front) expiry.
    """
    df = pd.read_csv('nifty_token.csv')
    expiries = df[['expiry']]
    expiries = expiries.drop_duplicates().reset_index(drop=True)
    expiries['expiry_dt'] = [datetime.strptime(e, '%d%b%Y') for e in expiries['expiry']]
    expiries = expiries.sort_values(by='expiry_dt').reset_index(drop=True)
    symbol = 'NIFTY' + (expiries['expiry'][0])[:5] + (expiries['expiry'][0])[5:][2:] + strike
    row = df.loc[df["symbol"] == symbol]
    token = row["token"].values[0]
    lotsize = int(row["lotsize"].values[0]) if "lotsize" in df.columns else 65
    return symbol, str(token), lotsize
