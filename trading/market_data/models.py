"""
Phase 9 -- market-data persistence schema (canonical ``Base``).

    market_candles    -- 1-minute OHLC(V/OI) for the four indices
    option_contracts  -- resolved option contract metadata (from the master)
    option_candles    -- 1-minute OHLC(V/OI) for subscribed option contracts

IMPORTANT (rule 16): ticks are NEVER stored here. The 1-minute aggregator
(``aggregator.py``) writes exactly one row per (symbol/contract, minute).

All timestamps are timezone-aware UTC; the minute-start is the candle key.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading.database.connection import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketCandle(Base):
    """1-minute candle for an index (NIFTY / BANKNIFTY / INDIA_VIX / SENSEX)."""

    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "exchange", "interval", "timestamp",
                         name="uq_market_candle_symbol_exch_interval_ts"),
        Index("ix_market_candle_symbol_ts", "symbol", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)   # internal symbol
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)             # NSE / BSE
    interval: Mapped[str] = mapped_column(String(12), nullable=False, default="1minute")
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class OptionContract(Base):
    """One resolved option contract. Populated by the instrument master;
    referenced by option_candles. provider_token is never hardcoded."""

    __tablename__ = "option_contracts"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_option_contract_symbol"),
        UniqueConstraint("provider", "provider_token", name="uq_option_contract_provider_token"),
        UniqueConstraint("underlying", "exchange", "expiry", "strike", "option_type",
                         name="uq_option_contract_natural_key"),
        Index("ix_option_contract_underlying_expiry", "underlying", "expiry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    underlying: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, default="NFO")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="icici_breeze")
    provider_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(80), nullable=False)  # internal option symbol
    expiry: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)  # CE / PE
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    candles: Mapped[list["OptionCandle"]] = relationship(back_populates="contract")


class OptionCandle(Base):
    """1-minute candle for one subscribed option contract."""

    __tablename__ = "option_candles"
    __table_args__ = (
        UniqueConstraint("contract_id", "timestamp", name="uq_option_candle_contract_ts"),
        Index("ix_option_candle_contract_ts", "contract_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("option_contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    contract: Mapped["OptionContract"] = relationship(back_populates="candles")
