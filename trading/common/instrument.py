"""
Broker-independent instrument identity.

A strategy reasons about an underlying/expiry/strike/option_type (or a
plain equity/index symbol) -- never about a broker's own instrument-token
scheme. Resolving an Instrument down to whatever a specific broker needs
(Angel's symboltoken, a future Dhan/ICICI/Shoonya security id, ...) is the
broker adapter's job (see e.g. AngelOneBroker.resolve_instrument()), not
something this object or OrderIntent ever needs to know how to do.

This is deliberately a thin identity record, not a full instrument-master
model (no lot size, tick size, margin rules, ...). Known limitation
(consistent with the rest of this migration): there is still no shared
cross-broker instrument-master/resolution service -- each adapter owns its
own resolution today. Instrument exists so a strategy/OrderIntent can
carry a structured reference instead of a bare symbol string when one is
available, without staking out that larger unsolved problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Instrument:
    """Broker-independent identity for one tradable instrument.

    `symbol`/`exchange` are the only two fields every instrument has (an
    equity, an index, an option, ...). The option-specific fields default
    to "empty" and are simply unused for non-derivatives. `instrument_id`
    is an OPTIONAL broker-agnostic identifier (e.g. ISIN) when one is
    known -- it is NOT a broker-specific token (no symboltoken, no
    Angel/Dhan/ICICI/Shoonya-specific id belongs here or anywhere in this
    object).
    """

    symbol: str
    exchange: str
    instrument_id: str = ""
    underlying: str = ""
    expiry: str = ""  # ISO date string ("YYYY-MM-DD"), "" if not applicable
    strike: float | None = None
    option_type: str = ""  # "CE" | "PE" | "" (not applicable)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_option(self) -> bool:
        return self.option_type in ("CE", "PE")
