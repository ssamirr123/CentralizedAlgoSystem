"""
Phase 10 -- persistence schema, following ``trading/ai_research/models.py``'s
established pattern exactly: one run row + a fixed per-node stage
checklist, with the full structured report stored as a single JSON
column (Section 38 conceptually suggests a separate
``ai_options_research_candidates`` table; we follow the SAME precedent
Phase 5 already established for ``AIResearchRun.report`` -- candidates
are never individually queried/filtered by SQL, so a normalized child
table would add schema without adding capability. This is documented in
the Phase 10 final report as a deliberate consistency choice, not an
oversight).

Never stores a secret: only provider/model NAMES (same rule as
``AIResearchRun``).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading.database.connection import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AIOptionsResearchRun(Base):
    __tablename__ = "ai_options_research_runs"

    research_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED", index=True)
    current_stage: Mapped[str | None] = mapped_column(String(80), nullable=True)

    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quick_think_llm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deep_think_llm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    debate_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    candidate_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    strategy_universe: Mapped[list | None] = mapped_column(JSON, nullable=True)
    generation_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    research_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_quality: Mapped[str | None] = mapped_column(String(20), nullable=True)

    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stages: Mapped[list["AIOptionsResearchStage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AIOptionsResearchStage.sequence",
    )


class AIOptionsResearchStage(Base):
    __tablename__ = "ai_options_research_stages"
    __table_args__ = (UniqueConstraint("research_id", "sequence", name="uq_ai_options_stage_research_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    research_id: Mapped[str] = mapped_column(ForeignKey("ai_options_research_runs.research_id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped["AIOptionsResearchRun"] = relationship(back_populates="stages")


STAGE_NAMES: tuple[str, ...] = (
    "Snapshot Validation", "Market Regime", "Volatility", "OI Positioning",
    "Candidate Generation", "Strategy Research", "Bull/Bear Research", "Risk", "Final Coordinator",
)
