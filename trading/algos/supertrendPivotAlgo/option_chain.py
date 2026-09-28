"""
option_chain.py
Resolve the ATM CE/PE option contract (symbol + token) for the nearest weekly
expiry using Angel One's public instrument master.
"""

import json
from datetime import datetime

import requests

import config
from logger import get_logger

log = get_logger()

INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


class OptionChain:
    def __init__(self):
        self.cfg = config.get_index_config()
        self._instruments = None

    def _load_instruments(self):
        if self._instruments is not None:
            return self._instruments
        log.info("Downloading instrument master...")
        resp = requests.get(INSTRUMENT_URL, timeout=30)
        resp.raise_for_status()
        self._instruments = resp.json()
        log.info("Instrument master loaded: %d rows", len(self._instruments))
        return self._instruments

    def round_to_atm(self, spot):
        """Round the spot price to the nearest valid strike."""
        step = self.cfg["strike_step"]
        return int(round(spot / step) * step)

    def _matching_options(self):
        name = self.cfg["spot_symbol"]
        exch = self.cfg["exchange"]
        out = []
        for ins in self._load_instruments():
            if ins.get("exch_seg") != exch:
                continue
            if ins.get("name") != name:
                continue
            if ins.get("instrumenttype") not in ("OPTIDX",):
                continue
            out.append(ins)
        return out

    def _nearest_expiry(self, options):
        today = datetime.now().date()
        expiries = set()
        for ins in options:
            exp = ins.get("expiry")
            if not exp:
                continue
            try:
                d = datetime.strptime(exp, "%d%b%Y").date()
            except ValueError:
                continue
            if d >= today:
                expiries.add(d)
        if not expiries:
            raise RuntimeError("No future expiries found in instrument master")
        return min(expiries)

    def get_atm_contract(self, spot, option_type):
        """
        Find the ATM option contract.

        Parameters
        ----------
        spot : float        -> current index spot price
        option_type : str   -> "CE" or "PE"

        Returns
        -------
        dict with: symbol, token, strike, expiry, lotsize
        """
        option_type = option_type.upper()
        atm_strike = self.round_to_atm(spot)
        options = self._matching_options()
        target_expiry = self._nearest_expiry(options)
        expiry_str = target_expiry.strftime("%d%b%Y").upper()

        # Strikes in the master are usually in paise (value * 100).
        for ins in options:
            if not ins.get("symbol", "").endswith(option_type):
                continue
            if ins.get("expiry", "").upper() != expiry_str:
                continue
            try:
                strike = float(ins["strike"]) / 100.0
            except (KeyError, ValueError):
                continue
            if int(strike) == atm_strike:
                contract = {
                    "symbol": ins["symbol"],
                    "token": ins["token"],
                    "strike": atm_strike,
                    "expiry": expiry_str,
                    "lotsize": int(ins.get("lotsize", self.cfg["lot_size"])),
                    "option_type": option_type,
                }
                log.info(
                    "ATM %s resolved: %s (strike=%s, expiry=%s)",
                    option_type, contract["symbol"], atm_strike, expiry_str,
                )
                return contract

        raise RuntimeError(
            f"Could not find ATM {option_type} for strike {atm_strike} "
            f"expiry {expiry_str}"
        )

