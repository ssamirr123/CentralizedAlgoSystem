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
    text,
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


class ExpiryCycle(Base):
    """One (underlying, expiry_date) cycle -- the primary Straddle Pulse
    boundary (rule: expiry defines the cycle, never a calendar week)."""

    __tablename__ = "expiry_cycles"
    __table_args__ = (
        UniqueConstraint("underlying", "expiry_date", name="uq_expiry_cycle_underlying_expiry"),
        Index("ix_expiry_cycle_underlying_status", "underlying", "status"),
        # Hard DB-level guarantee (not just a service-layer convention):
        # at most one ACTIVE cycle per underlying, ever.
        Index(
            "uq_expiry_cycle_one_active_per_underlying", "underlying", unique=True,
            sqlite_where=text("status = 'ACTIVE'"), postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    underlying: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cycle_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["DailySession"]] = relationship(back_populates="cycle")


class DailySession(Base):
    """One trading day for one underlying, within one expiry cycle.
    ATM is locked exactly once (session_status PENDING -> LOCKED) from the
    completed 09:15-09:16 candle and never recomputed thereafter."""

    __tablename__ = "daily_sessions"
    __table_args__ = (
        UniqueConstraint("underlying", "trading_date", name="uq_daily_session_underlying_date"),
        Index("ix_daily_session_cycle", "cycle_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("expiry_cycles.id", ondelete="CASCADE"), nullable=False)
    underlying: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    spot_0916: Mapped[float | None] = mapped_column(Float, nullable=True)
    atm_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    atm_ce_contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("option_contracts.id", ondelete="SET NULL"), nullable=True
    )
    atm_pe_contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("option_contracts.id", ondelete="SET NULL"), nullable=True
    )
    atm_ce_symbol: Mapped[str | None] = mapped_column(String(80), nullable=True)
    atm_pe_symbol: Mapped[str | None] = mapped_column(String(80), nullable=True)
    session_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    cycle: Mapped["ExpiryCycle"] = relationship(back_populates="sessions")


class OISnapshot(Base):
    """Periodic OI/PCR snapshot for one underlying+expiry+trading_date,
    totalled over the session's subscribed ATM +/- range strike window
    (that window is all the live OI the feed actually subscribes to --
    see MarketDataService._resubscribe_option_universe)."""

    __tablename__ = "oi_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "underlying", "expiry_date", "trading_date", "timestamp",
            name="uq_oi_snapshot_underlying_expiry_date_ts",
        ),
        Index("ix_oi_snapshot_underlying_date", "underlying", "trading_date"),
        Index("ix_oi_snapshot_cycle", "cycle_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("expiry_cycles.id", ondelete="CASCADE"), nullable=False)
    underlying: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    call_oi_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    put_oi_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    call_oi_change: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    put_oi_change: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pcr: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
