"""
option_chain.py
Resolve a NIFTY weekly option contract (symbol + token + lotsize) at a
given strike / expiry / option-type via Angel One's instrument master.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

import requests

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger

log = get_logger()

INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


class OptionChain:
    def __init__(self) -> None:
        self.cfg = config.get_index_config()
        self._instruments = None

    def _load_instruments(self) -> list[dict]:
        if self._instruments is not None:
            return self._instruments
        log.info("Downloading instrument master...")
        resp = requests.get(INSTRUMENT_URL, timeout=30)
        resp.raise_for_status()
        self._instruments = resp.json()
        log.info("Instrument master loaded: %d rows", len(self._instruments))
        return self._instruments

    def _matching_options(self) -> list[dict]:
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

    def _parse_expiries(self, options: Iterable[dict]) -> list[date]:
        expiries = set()
        today = date.today()
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
        return sorted(expiries)

    def next_weekly_expiry(self) -> date:
        opts = self._matching_options()
        exps = self._parse_expiries(opts)
        if not exps:
            raise RuntimeError("No future expiries found in instrument master")
        return exps[0]

    def resolve(self, strike: int, option_type: str, expiry: Optional[date] = None) -> dict:
        option_type = option_type.upper()
        exp = expiry or self.next_weekly_expiry()
        exp_str = exp.strftime("%d%b%Y").upper()
        opts = self._matching_options()

        for ins in opts:
            if ins.get("expiry", "").upper() != exp_str:
                continue
            if not ins.get("symbol", "").endswith(option_type):
                continue
            try:
                raw_strike = float(ins["strike"]) / 100.0
            except (KeyError, ValueError):
                continue
            if int(raw_strike) == int(strike):
                return {
                    "symbol": ins["symbol"],
                    "token": ins["token"],
                    "strike": int(raw_strike),
                    "expiry": exp,
                    "expiry_str": exp_str,
                    "lotsize": int(ins.get("lotsize", self.cfg["lot_size"])),
                    "option_type": option_type,
                }
        raise LookupError(
            f"No {option_type} contract found at strike={strike} expiry={exp_str}"
        )
