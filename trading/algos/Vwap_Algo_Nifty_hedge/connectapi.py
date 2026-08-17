from SmartApi import SmartConnect
import pyotp
import time
import config


def makeconnection():

    for attempt in range(0,3):
        try:
            apikey = config.apikey
            clientid = config.clientid
            mpin = config.mpin
            token = config.token
            tk = pyotp.TOTP(token).now()
            obj = SmartConnect(api_key=apikey)
            data = obj.generateSession(clientid,mpin,tk)
            print('Connection Established')
            return obj
        except Exception as e:
            print(f'[ERROR] makeconnection attempt {attempt+1}/3: {e}')
            time.sleep(1)
    print('[FAILED] makeconnection – all attempts exhausted, returning None')
    return None
