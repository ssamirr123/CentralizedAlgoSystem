"""
AI Research Engine -- durable persistence schema (registers on the SAME
canonical Base as trading/database/models.py and trading/market_data/models.py
-- one SQLAlchemy Base, one Alembic history, per the project's own
convention; see trading/database/connection.py's own docstring).

Two tables, deliberately not three: the Phase 5 brief's own conceptual
sketch suggested a separate research_outputs table, but the report fields
(market_analysis, bull_case, trader_plan, ...) are never individually
queried/filtered by SQL anywhere in this codebase -- they are always read
and written as one whole normalized object (ResearchReportOut, unchanged
since Phase 2). Storing them as a single JSON column on the run row
follows the exact precedent already in this schema (Command.result,
AuditLog.detail -- see trading/database/models.py) and avoids a join for
every single read, without losing structure (Section 7's actual
requirement is "don't flatten into one unsearchable text field", which a
JSON column already satisfies -- it is not a text blob, every field
survives as its own JSON key).

research_id is used as the primary key directly (a String, our own
already-generated UUID) rather than adding a redundant surrogate integer
id -- every existing caller (router.py, the frontend) already addresses a
run by this string everywhere; adding a second ID system would only add
indirection with no benefit.
"""
from __future__ import annotations

from datetime import date as date_, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading.database.connection import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AIResearchRun(Base):
    """One row per research job -- the durable equivalent of Phase 2-4's
    in-memory ResearchJob. Never stores a secret: only provider/model
    NAMES (strings), never an API key/token/credential (Section 5/27)."""

    __tablename__ = "ai_research_runs"

    research_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    research_date: Mapped[date_] = mapped_column(Date, nullable=False, index=True)
    research_depth: Mapped[str] = mapped_column(String(16), nullable=False)

    # Phase 7 -- market domain metadata (Section 34). NULLABLE: every
    # Phase 5/6 row predates this column and has no value here.
    # repository._row_to_job() interprets NULL as the pre-Phase-7 default
    # (market=US, currency=USD) -- Section 35, never a data migration
    # that rewrites old rows.
    market: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)
    canonical_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Phase 8 (Section 19/29/37): a SMALL provenance summary about the
    # market data used for this run's technical-analysis context (status/
    # source/provider/interval/date-range/cutoff/candle_count) -- never
    # the candles themselves (those stay in market_candles, the market-
    # data DB's own table). NULL for US research and for any row created
    # before this phase.
    market_data_provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
    current_stage: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Configuration -- enough to understand/reproduce a run (Section 5).
    # Provider/model NAMES only; never a key/token.
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    quick_think_llm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deep_think_llm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_analysts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    data_vendors: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    debate_rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_debate_rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Explicit RESEARCH-context portfolio (Section 14, Phase 2/3) -- cash/
    # currency/positions only; never a real broker credential or session.
    portfolio: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Result -- the full normalized ResearchReportOut shape, unchanged
    # field names since Phase 2 (market_analysis, bull_case, trader_plan,
    # risk_*, final_trade_decision, signal, ...). NULL until COMPLETED, or
    # if a run FAILED before producing any report.
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Failure info (Section 9) -- sanitized only, never a stack trace or
    # credential fragment (service.py's existing _sanitize_error still
    # applies before this is ever written).
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stages: Mapped[list["AIResearchStage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AIResearchStage.sequence",
    )


class AIResearchStage(Base):
    """One row per PLANNED real graph node for a run (Section 6) -- the
    durable equivalent of Phase 3/4's in-memory NodeStageOut list. Exactly
    one row per node in the deterministic execution plan (seeded at
    creation from trading_agents_adapter.real_node_order()), updated in
    place as real events arrive -- NOT append-only, even though a
    debate/risk node can complete multiple times across rounds, to
    preserve the exact Phase 3 semantic (a fixed-size checklist, not an
    event log). RUNNING remains inferred, COMPLETED remains an observed
    event -- persistence changes nothing about that (Section 6's own
    instruction)."""

    __tablename__ = "ai_research_stages"
    __table_args__ = (UniqueConstraint("research_id", "sequence", name="uq_ai_research_stage_run_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    research_id: Mapped[str] = mapped_column(ForeignKey("ai_research_runs.research_id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped[AIResearchRun] = relationship(back_populates="stages")
