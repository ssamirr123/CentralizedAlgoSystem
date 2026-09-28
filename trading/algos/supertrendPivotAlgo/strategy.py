"""
strategy.py
Entry and exit decision logic combining pivots + supertrend signals.

Entry:
    close > R1 and supertrend == GREEN and no open position -> Sell ATM PUT
    close < S1 and supertrend == RED   and no open position -> Sell ATM CALL
Exit:
    PE open and supertrend RED   -> buy back put
    CE open and supertrend GREEN -> buy back call
    time >= square-off           -> square off all
"""

import config
from logger import get_logger
from indicators import (
    daily_pivots,
    previous_day_ohlc,
    supertrend,
)

log = get_logger()


class Strategy:
    def __init__(self, market_data, option_chain, order_manager):
        self.md = market_data
        self.oc = option_chain
        self.om = order_manager

    def evaluate(self):
        """
        Evaluate the latest completed 5-minute candle and act on signals.
        Intended to be called once per completed candle.
        """
        df = self.md.get_candles(lookback_days=5)
        if len(df) < config.SUPERTREND_PERIOD + 2:
            log.warning("Not enough candles to evaluate yet.")
            return

        # Pivots from the previous completed day.
        daily = previous_day_ohlc(df)
        if len(daily) < 2:
            log.warning("Not enough daily data for pivots.")
            return
        # Use the day before the current (last) one.
        pivots = daily_pivots(daily.iloc[:-1])
        r1, s1 = pivots["r1"], pivots["s1"]

        # Supertrend on the 5-minute series; use last *completed* candle.
        st_df = supertrend(df)
        last = st_df.iloc[-1]
        close = float(last["close"])
        st_dir = last["supertrend_dir"]

        log.info(
            "Candle %s  close=%.2f  R1=%.2f  S1=%.2f  Supertrend=%s",
            last["datetime"], close, r1, s1, st_dir,
        )

        pos = self.om.current_position

        # -------------------------------------------------- exit logic first
        if pos is not None:
            if pos.option_type == "PE" and st_dir == "RED":
                log.info("Supertrend turned RED -> exit PUT")
                self.om.exit_position(reason="SUPERTREND_RED")
                return
            if pos.option_type == "CE" and st_dir == "GREEN":
                log.info("Supertrend turned GREEN -> exit CALL")
                self.om.exit_position(reason="SUPERTREND_GREEN")
                return
            # Position still valid, nothing to do.
            log.info("Holding open %s position.", pos.option_type)
            return

        # ------------------------------------------------------- entry logic
        ok, reason = self.om.can_open_new_position()
        if not ok:
            log.info("No new entry: %s", reason)
            return

        spot = self.md.get_spot_price()

        if close > r1 and st_dir == "GREEN":
            log.info("R1 breakout + Supertrend GREEN -> Sell ATM PUT")
            contract = self.oc.get_atm_contract(spot, "PE")
            self.om.sell_option(contract)
        elif close < s1 and st_dir == "RED":
            log.info("S1 breakdown + Supertrend RED -> Sell ATM CALL")
            contract = self.oc.get_atm_contract(spot, "CE")
            self.om.sell_option(contract)
        else:
            log.info("No entry signal on this candle.")
