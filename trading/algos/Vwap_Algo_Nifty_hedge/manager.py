import token_file,config,rest_func,time,threading,random,monitor
from datetime import datetime
import pandas as pd

def trademanager(strike):
    try:
        _trademanager(strike)
    except Exception as e:
        # A crash in a trading thread -> flag ERROR on the monitor, then re-raise.
        print(f'[FATAL] trademanager({strike}) crashed: {e}')
        monitor.report('ERROR')
        raise

def _trademanager(strike):

    symbol = token_file.token_nifty(strike)[0]
    token = token_file.token_nifty(strike)[1]
    hedge = rest_func.get_hedge_strike(symbol)
    hedge_symbol = token_file.token_nifty(hedge)[0]
    hedge_token = token_file.token_nifty(hedge)[1]
    print("hedge_symbol:", hedge_symbol)
    print("hedge_token:", hedge_token)
    rest_func.place_market_order(hedge_symbol,hedge_token,config.qty,'BUY')
    monitor.report('RUNNING')   # trade executed -> push metrics

    config.slhit[token] = False

    firstflagentry = True
    istriggered = False
    triggerlow = 0
    triggerhigh = 0
    isintrade = False
    stoploss = 0
    timeup = False
    entryprice = 0
    sl_orderid = 0
    istrailed = False
    cemaxentrylimit = 0
    pemaxentrylimit = 0


    count=0
    while True:
        dt = datetime.now()
        if((dt.minute%2!=0 and dt.second==0) or firstflagentry):
            flagsamecandletrade = True

            # Wait until at least one candle exists (avoids empty-DataFrame / negative-index crash).
            if len(config.ohlc_data[token]['Datetime']) == 0:
                time.sleep(1)
                continue

            df = rest_func.make_vwap(pd.DataFrame(config.ohlc_data[token]))
            df["Vwap"] = df["Vwap"].round(2)
            count = len(df)-1

            ###Timeup
            if(df['Datetime'][count]=='14:30:00'):
                print('No Trades From Now')
                timeup = True

            #Trigger Cancel
            if(df['Close'][count]>df['Vwap'][count] and istriggered and timeup==False):
                print(str(strike)+' Trigger Cancel '+str(dt.strftime('%H:%M'))+str('    '))
                istriggered = False
                triggerlow = 0
                triggerhigh = 0

            #Stoploss
            if(config.slhit[token] and isintrade):
                print(str(strike)+' Stoploss Executed '+str(dt.strftime('%H:%M'))+str('    '))
                isintrade = False
                flagsamecandletrade = False

            #Trailing
            #if(df['Low'][count]<entryprice-25 and isintrade and istrailed==False):
            #   print(str(strike)+' Stoploss Trailed to cost '+str(dt.strftime('%H:%M'))+str('    '))
            #    rest_func.modify_stoploss_order(symbol,token,config.qty,str(entryprice),sl_orderid)
            #    istrailed = True

            #Execution
            if(df['Close'][count]<triggerlow and istriggered and isintrade==False and timeup==False):
                istriggered = False
                stoploss = 0
                if(triggerhigh-df['Close'][count]>20):
                    stoploss = df['Close'][count]+20
                elif(10 <= triggerhigh-df['Close'][count]<=20):
                    stoploss = triggerhigh
                else:
                    stoploss = df['Close'][count]+10


                # CE Entry Count
                if('CE' in str(strike)):
                    if(cemaxentrylimit >= 3):
                        flagsamecandletrade = False
                        isintrade = False
                        print(str(strike)+' CE Max Entry Limit Reached '+str(dt.strftime('%H:%M'))+str('    '))
                        continue
                    else:
                        cemaxentrylimit+=1
                # PE Entry Count
                if('PE' in str(strike)):
                    if(pemaxentrylimit >= 3):
                        flagsamecandletrade = False
                        isintrade = False
                        print(str(strike)+' PE Max Entry Limit Reached '+str(dt.strftime('%H:%M'))+str('    '))
                        continue
                    else:
                        pemaxentrylimit+=1

                print(str(strike)+' Short Executed @'+str(df['Close'][count])+'   '+str(dt.strftime('%H:%M'))+' '+' Stoploss-> '+str(stoploss)+str('    '))
                entryprice = df['Close'][count]
                triggerlow = 0
                triggerhigh = 0
                isintrade = True
                istrailed = False
                config.slhit[token] = False
                rest_func.place_market_order(symbol,token,config.qty,'SELL')
                monitor.report('RUNNING')   # trade executed -> push metrics
                time.sleep(0.5)
                sl_orderid = rest_func.place_stoploss_order(symbol,token,config.qty,stoploss)
                time.sleep(2)
                threading.Thread(target=rest_func.sltracking,args=(sl_orderid,token)).start()


            #Vwap Trigger
            if(df['Close'][count]<df['Vwap'][count] and isintrade==False and timeup==False and flagsamecandletrade):
                print(str(strike)+' Triggered '+str(dt.strftime('%H:%M'))+str('    '))
                istriggered = True
                triggerlow = df['Low'][count]
                triggerhigh = df['High'][count]

            #Booking
            if(df['Datetime'][count]=='15:14:00'):
                if(isintrade):
                    time.sleep(random.randint(1,9)/10)
                    print(str(strike)+' Trade Booked '+str('    '))
                    rest_func.place_market_order(symbol,token,config.qty,'BUY')
                    monitor.report('RUNNING')   # trade executed -> push metrics
                    isintrade = False

                time.sleep(random.randint(1, 9) / 10)
                print(f'{hedge_symbol} Hedge Exited')
                rest_func.place_market_order(hedge_symbol,hedge_token,config.qty,'SELL')
                monitor.report('RUNNING')   # trade executed -> push metrics
                break

            time.sleep(1)
            firstflagentry = False
        time.sleep(0.01)
