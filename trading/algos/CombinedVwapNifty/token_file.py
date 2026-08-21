import os
import time
import requests
import pandas as pd
from datetime import datetime

SCRIP_MASTER_URL = 'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json'
# curl succeeds against this URL where a plain requests.get times out -- the
# CDN appears to throttle/serve slower to the default python-requests UA,
# and the ~37MB body legitimately needs more than a 30s read window. A
# browser-like UA + a generous (connect, read) timeout matches curl's
# observed behavior.
REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
REQUEST_TIMEOUT = (10, 90)


def download_token():
    # The scrip master is a ~37MB file -- Angel Broking's CDN occasionally
    # truncates the response mid-stream (IncompleteRead) or stalls long
    # enough to trip a read timeout. Retry a few times before falling back
    # to whatever nifty_token.csv is already on disk from a prior
    # successful run, so a transient network blip doesn't take the whole
    # algo down.
    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(SCRIP_MASTER_URL, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            token = pd.DataFrame(resp.json())
            break
        except Exception as exc:
            last_exc = exc
            print(f"download_token: attempt {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)
    else:
        if os.path.exists('nifty_token.csv'):
            print(f"download_token: all attempts failed ({last_exc}); reusing existing nifty_token.csv")
            return
        raise RuntimeError(f"download_token: all attempts failed: {last_exc}") from last_exc

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
