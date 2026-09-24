"""
Section 23 -- derivatives metadata FOUNDATION only. This module defines
the shape later phases will need; it does not fetch, compute, or resolve
any real option-chain/contract data.

    NO OPTION CHAIN FETCHING.
    NO OI/IV/GREEKS/PCR/MAX PAIN.
    NO EXPIRY-DATE COMPUTATION (Section 24 -- deferred to whichever later
    phase actually needs it, since real NSE expiry-date determination
    should come from authoritative provider/contract metadata at that
    time, not a hardcoded "every weekly expiry is Thursday" assumption
    verified only in memory here and liable to go stale, per Section 24's
    own explicit warning).

OptionType/DerivativeContract exist so a later phase has a stable shape
to populate -- every field is Optional and nothing here is ever
constructed with real data by this phase.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class OptionType(str, Enum):
    CALL = "CE"
    PUT = "PE"


@dataclass(frozen=True)
class DerivativeContract:
    """Shape only -- Section 23's "Underlying / Expiry / Strike / Option
    type / Lot size / Tick size" list, nothing more. No instance of this
    is created by Phase 7 with real market data."""

    underlying_canonical_id: str
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    lot_size: int | None = None
    tick_size: float | None = None
