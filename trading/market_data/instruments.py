"""
Phase 5 -- instrument master.

Holds the currently-listed option contract universe (resolved dynamically
from the provider, never hardcoded) and answers:

    list_expiries(underlying)              -> [date, ...]  (ascending)
    list_strikes(underlying, expiry)       -> [float, ...] (ascending)
    resolve(underlying, expiry, strike, ot)-> Instrument | None
    get(internal_symbol)                   -> Instrument | None

``needs_refresh(today)`` is True until it has been refreshed on ``today``
(Phase 15's daily-startup step 6).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Sequence
from datetime import date

from trading.market_data.symbols import Instrument, make_option_symbol

logger = logging.getLogger("trading.market_data.instruments")


class InstrumentMaster:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_symbol: dict[str, Instrument] = {}
        # underlying -> expiry -> strike -> {"CE": Instrument, "PE": Instrument}
        self._tree: dict[str, dict[date, dict[float, dict[str, Instrument]]]] = {}
        self.refreshed_on: date | None = None
        self.last_source: str | None = None

    # --- population ------------------------------------------------
    def load(self, instruments: Iterable[Instrument], *, as_of: date | None = None,
             source: str | None = None) -> int:
        """Replace the universe with ``instruments`` (option instruments only)."""
        by_symbol: dict[str, Instrument] = {}
        tree: dict[str, dict[date, dict[float, dict[str, Instrument]]]] = {}
        count = 0
        for inst in instruments:
            if not inst.is_option or inst.underlying is None or inst.expiry is None \
               or inst.strike is None or inst.option_type not in ("CE", "PE"):
                continue
            by_symbol[inst.internal_symbol] = inst
            (tree.setdefault(inst.underlying, {})
                 .setdefault(inst.expiry, {})
                 .setdefault(float(inst.strike), {})[inst.option_type]) = inst
            count += 1
        with self._lock:
            self._by_symbol = by_symbol
            self._tree = tree
            if as_of is not None:
                self.refreshed_on = as_of
            if source is not None:
                self.last_source = source
        logger.info("market_data.instrument_master loaded contracts=%d underlyings=%d",
                    count, len(tree))
        return count

    def refresh(self, provider, underlyings: Sequence[str] = ("NIFTY",), *,
                as_of: date | None = None) -> int:
        """Pull ``get_option_instruments`` for each underlying and load()."""
        collected: list[Instrument] = []
        for u in underlyings:
            collected.extend(provider.get_option_instruments(u))
        return self.load(collected, as_of=as_of, source=getattr(provider, "name", "provider"))

    # --- queries -------------------------------------------------
    def needs_refresh(self, today: date) -> bool:
        with self._lock:
            return self.refreshed_on != today

    def is_empty(self) -> bool:
        with self._lock:
            return not self._by_symbol

    def count(self, underlying: str | None = None) -> int:
        with self._lock:
            if underlying is None:
                return len(self._by_symbol)
            expiries = self._tree.get(underlying.strip().upper(), {})
            return sum(len(by_ot) for strikes in expiries.values() for by_ot in strikes.values())

    def list_expiries(self, underlying: str) -> list[date]:
        with self._lock:
            return sorted(self._tree.get(underlying.strip().upper(), {}))

    def list_strikes(self, underlying: str, expiry: date) -> list[float]:
        with self._lock:
            return sorted(self._tree.get(underlying.strip().upper(), {}).get(expiry, {}))

    def resolve(self, underlying: str, expiry: date, strike: float,
                option_type: str) -> Instrument | None:
        with self._lock:
            return (
                self._tree.get(underlying.strip().upper(), {})
                .get(expiry, {})
                .get(float(strike), {})
                .get(option_type.strip().upper())
            )

    def get(self, internal_symbol: str) -> Instrument | None:
        with self._lock:
            return self._by_symbol.get(internal_symbol)

    def resolve_symbol(self, underlying: str, expiry: date, strike: float, option_type: str) -> str:
        return make_option_symbol(underlying, expiry, strike, option_type)


# --- process singleton -----------------------------------------------------
_master: InstrumentMaster | None = None
_master_lock = threading.Lock()


def get_instrument_master() -> InstrumentMaster:
    global _master
    if _master is None:
        with _master_lock:
            if _master is None:
                _master = InstrumentMaster()
    return _master


def set_instrument_master(m: InstrumentMaster | None) -> None:
    """Test hook."""
    global _master
    with _master_lock:
        _master = m
