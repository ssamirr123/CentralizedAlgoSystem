import pyotp
import time
import json
import os
from datetime import datetime
import config


def makeconnection_angel():
    """Log in to Angel One SmartAPI via TOTP. Retries a few times, returns None on failure."""
    from SmartApi import SmartConnect
    for attempt in range(0, 3):
        try:
            apikey = config.apikey
            clientid = config.clientid
            mpin = config.mpin
            token = config.token
            tk = pyotp.TOTP(token).now()
            obj = SmartConnect(api_key=apikey)
            obj.generateSession(clientid, mpin, tk)
            print('[LOGIN] Angel One Connection Established')
            return obj
        except Exception as e:
            print(f'[ERROR] makeconnection_angel attempt {attempt + 1}/3: {type(e).__name__}')
            time.sleep(1)
    print('[FAILED] makeconnection_angel - all attempts exhausted, returning None')
    return None


def makeconnection_shoonya():
    """Log in to Shoonya (Finvasia) NorenApi via TOTP. Retries a few times, returns None on failure."""
    from NorenRestApiPy.NorenApi import NorenApi
    for attempt in range(0, 3):
        try:
            api = NorenApi(host='https://api.shoonya.com/NorenWSTP/', websocket='wss://api.shoonya.com/NorenWSTP/')
            user = config.shoonya_user_id
            pwd = config.shoonya_password
            token = config.shoonya_totp_secret
            tk = pyotp.TOTP(token).now() if token else ""
            vc = config.shoonya_vendor_code
            api_key = config.shoonya_api_key
            imei = config.shoonya_imei or "abc1234"

            ret = api.login(userid=user, password=pwd, twoFA=tk, vendor_code=vc, api_secret=api_key, imei=imei)
            if ret and ret.get('stat') == 'Ok':
                print(f'[LOGIN] Shoonya Connection Established for user {user}')
                return api
            else:
                msg = ret.get('emsg', 'Unknown login error') if isinstance(ret, dict) else str(ret)
                print(f'[ERROR] Shoonya login failed (attempt {attempt + 1}/3): {msg}')
        except Exception as e:
            print(f'[ERROR] makeconnection_shoonya attempt {attempt + 1}/3: {type(e).__name__} - {e}')
        time.sleep(1)
    print('[FAILED] makeconnection_shoonya - all attempts exhausted, returning None')
    return None


def makeconnection():
    """Log in to the configured broker."""
    if config.BROKER == "SHOONYA":
        return makeconnection_shoonya()
    return makeconnection_angel()

