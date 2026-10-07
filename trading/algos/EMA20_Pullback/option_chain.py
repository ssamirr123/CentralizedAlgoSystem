"""
option_chain.py
Resolve a NIFTY weekly option contract via AngelOne's JSON scrip master
or Dhan's CSV scrip master, selected by the BrokerAdapter kind.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Optional

import requests

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger
from trading.algos.EMA20_Pullback.login import BrokerAdapter

log = get_logger()

ANGEL_INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


class OptionChain:
    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self.cfg = config.get_index_config()
        self._angel_instruments: Optional[list[dict]] = None
        self._dhan_rows: Optional[list[dict]] = None

    def _load_angel(self) -> list[dict]:
        if self._angel_instruments is not None:
            return self._angel_instruments
        log.info("Downloading AngelOne instrument master...")
        resp = requests.get(ANGEL_INSTRUMENT_URL, timeout=30)
        resp.raise_for_status()
        self._angel_instruments = resp.json()
        log.info("AngelOne master loaded: %d rows", len(self._angel_instruments))
        return self._angel_instruments

    def _angel_matching(self) -> list[dict]:
        out = []
        name = self.cfg["spot_symbol"]
        exch = self.cfg["exchange"]
        for ins in self._load_angel():
            if ins.get("exch_seg") != exch:
                continue
            if ins.get("name") != name:
                continue
            if ins.get("instrumenttype") not in ("OPTIDX",):
                continue
            out.append(ins)
        return out

    def _angel_expiries(self) -> list[date]:
        today = date.today()
        exps = set()
        for ins in self._angel_matching():
            raw = ins.get("expiry")
            if not raw:
                continue
            try:
                d = datetime.strptime(raw, "%d%b%Y").date()
            except ValueError:
                continue
            if d >= today:
                exps.add(d)
        return sorted(exps)

    def _angel_resolve(self, strike: int, option_type: str, expiry: date) -> dict:
        exp_str = expiry.strftime("%d%b%Y").upper()
        for ins in self._angel_matching():
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
                    "expiry": expiry,
                    "expiry_str": exp_str,
                    "lotsize": int(ins.get("lotsize", self.cfg["lot_size"])),
                    "option_type": option_type,
                }
        raise LookupError(f"[AngelOne] no {option_type} at strike={strike} expiry={exp_str}")

    def _load_dhan(self) -> list[dict]:
        if self._dhan_rows is not None:
            return self._dhan_rows
        log.info("Downloading Dhan scrip master...")
        resp = requests.get(DHAN_SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        name = self.cfg["spot_symbol"]
        rows = []
        for row in reader:
            if row.get("SEM_INSTRUMENT_NAME") != "OPTIDX":
                continue
            tsym = row.get("SEM_TRADING_SYMBOL", "").upper()
            sm_name = row.get("SM_SYMBOL_NAME", "").upper()
            if sm_name != name and not tsym.startswith(name):
                continue
            rows.append(row)
        self._dhan_rows = rows
        log.info("Dhan master loaded: %d NIFTY OPTIDX rows", len(rows))
        return rows

    @staticmethod
    def _dhan_parse_expiry(raw: str) -> Optional[date]:
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def _dhan_expiries(self) -> list[date]:
        today = date.today()
        exps = set()
        for row in self._load_dhan():
            d = self._dhan_parse_expiry(row.get("SEM_EXPIRY_DATE", ""))
            if d and d >= today:
                exps.add(d)
        return sorted(exps)

    def _dhan_resolve(self, strike: int, option_type: str, expiry: date) -> dict:
        for row in self._load_dhan():
            d = self._dhan_parse_expiry(row.get("SEM_EXPIRY_DATE", ""))
            if d != expiry:
                continue
            if (row.get("SEM_OPTION_TYPE") or "").upper() != option_type:
                continue
            try:
                row_strike = float(row.get("SEM_STRIKE_PRICE") or 0)
            except ValueError:
                continue
            if int(row_strike) != int(strike):
                continue
            return {
                "symbol": row.get("SEM_TRADING_SYMBOL"),
                "token": row.get("SEM_SMST_SECURITY_ID"),
                "strike": int(row_strike),
                "expiry": expiry,
                "expiry_str": expiry.strftime("%d%b%Y").upper(),
                "lotsize": int(row.get("SEM_LOT_UNITS") or self.cfg["lot_size"]),
                "option_type": option_type,
                "exchange_segment": row.get("SEM_EXM_EXCH_ID", "NSE_FNO"),
            }
        raise LookupError(f"[Dhan] no {option_type} at strike={strike} expiry={expiry.isoformat()}")

    def next_weekly_expiry(self) -> date:
        exps = self._angel_expiries() if self.broker.kind == "ANGELONE" else self._dhan_expiries()
        if not exps:
            raise RuntimeError("No future expiries found in instrument master")
        return exps[0]

    def resolve(self, strike: int, option_type: str, expiry: Optional[date] = None) -> dict:
        option_type = option_type.upper()
        exp = expiry or self.next_weekly_expiry()
        if self.broker.kind == "ANGELONE":
            return self._angel_resolve(strike, option_type, exp)
        if self.broker.kind == "DHAN":
            return self._dhan_resolve(strike, option_type, exp)
        raise RuntimeError(f"Unsupported broker kind: {self.broker.kind!r}")
