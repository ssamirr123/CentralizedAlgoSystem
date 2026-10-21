"""
option_chain.py
Resolve the ATM CE/PE option contract (symbol + token) for the nearest weekly
expiry using Angel One's public instrument master.
"""

import json
import os
import tempfile
import time
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

    # The master is ~tens of MB and the host is slow at times: cache it on disk
    # per day, retry, and let main.py preload it at startup so an entry is
    # never delayed (or lost) by a download at signal time.
    _DOWNLOAD_ATTEMPTS = 4
    _READ_TIMEOUT = 120

    def _cache_path(self):
        return os.path.join(
            tempfile.gettempdir(),
            f"angel_scrip_master_{datetime.now().strftime('%Y%m%d')}.json",
        )

    def _load_instruments(self):
        if self._instruments is not None:
            return self._instruments

        path = self._cache_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._instruments = json.load(fh)
                log.info("Instrument master loaded from cache: %d rows",
                         len(self._instruments))
                return self._instruments
            except (OSError, ValueError) as exc:
                log.warning("Instrument cache unreadable (%s); re-downloading.", exc)

        last_exc = None
        for attempt in range(1, self._DOWNLOAD_ATTEMPTS + 1):
            log.info("Downloading instrument master (attempt %d/%d)...",
                     attempt, self._DOWNLOAD_ATTEMPTS)
            try:
                resp = requests.get(INSTRUMENT_URL, timeout=(10, self._READ_TIMEOUT))
                resp.raise_for_status()
                self._instruments = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                log.warning("Instrument master download failed: %s", exc)
                time.sleep(2 * attempt)
        else:
            raise last_exc

        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._instruments, fh)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not cache instrument master: %s", exc)
        log.info("Instrument master loaded: %d rows", len(self._instruments))
        return self._instruments

    def preload(self):
        """Best-effort warm-up at startup; failures are retried at entry time."""
        try:
            self._load_instruments()
        except Exception as exc:  # noqa: BLE001
            log.error("Instrument master preload failed: %s", exc)

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

    def is_expiry_day(self):
        """True when the nearest weekly expiry is today."""
        return self._nearest_expiry(self._matching_options()) == datetime.now().date()

    def _find_contract(self, strike, option_type):
        option_type = option_type.upper()
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
                ins_strike = float(ins["strike"]) / 100.0
            except (KeyError, ValueError):
                continue
            if int(ins_strike) == strike:
                return {
                    "symbol": ins["symbol"],
                    "token": ins["token"],
                    "strike": strike,
                    "expiry": expiry_str,
                    "lotsize": int(ins.get("lotsize", self.cfg["lot_size"])),
                    "option_type": option_type,
                }

        raise RuntimeError(
            f"Could not find {option_type} for strike {strike} expiry {expiry_str}"
        )

    def get_atm_contract(self, spot, option_type):
        """
        Find the ATM option contract.

        Returns
        -------
        dict with: symbol, token, strike, expiry, lotsize, option_type
        """
        atm_strike = self.round_to_atm(spot)
        contract = self._find_contract(atm_strike, option_type)
        log.info(
            "ATM %s resolved: %s (strike=%s, expiry=%s)",
            contract["option_type"], contract["symbol"], atm_strike, contract["expiry"],
        )
        return contract

    def get_hedge_contract(self, sold_contract):
        """
        Far-OTM long hedge for a short option: same type and expiry, placed
        HEDGE_POINTS_EXPIRY_DAY (500) points away on expiry day, otherwise
        HEDGE_POINTS (1000). Sold PE -> lower strike; sold CE -> higher strike.
        """
        points = (config.HEDGE_POINTS_EXPIRY_DAY if self.is_expiry_day()
                  else config.HEDGE_POINTS)
        sign = -1 if sold_contract["option_type"] == "PE" else 1
        strike = sold_contract["strike"] + sign * points
        contract = self._find_contract(strike, sold_contract["option_type"])
        log.info("Hedge %s resolved: %s (strike=%s, %d pts from %s)",
                 contract["option_type"], contract["symbol"], strike, points,
                 sold_contract["strike"])
        return contract
