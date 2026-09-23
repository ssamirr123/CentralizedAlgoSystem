"""
AI Research Backtesting -- durable persistence schema. Registers on the
SAME canonical Base as trading/ai_research/models.py (Phase 5's own
precedent: one SQLAlchemy Base, one Alembic history, tables separated only
by naming convention -- ai_backtest_* here, ai_research_* for single-run
research, trading tables elsewhere).

Two tables, mirroring Phase 5's own "two tables, not three" reasoning:
  * AIBacktestJob   -- one row per backtest request/lifecycle/aggregate metrics.
  * AIBacktestRun   -- one row per (symbol, date) grid cell. Links to the
    underlying AIResearchRun.research_id (Section 18) rather than storing
    a second copy of the full report.

No secret is ever stored here (Section 27, same discipline as Phase 5):
only symbol/date/provider/model NAMES and evaluation NUMBERS.
"""
from __future__ import annotations

from datetime import date as date_, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading.database.connection import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AIBacktestJob(Base):
    """One row per backtest request. total_runs/completed_runs/failed_runs
    are the ONLY basis for progress (Section 9) -- never an agent-level
    fabricated percentage."""

    __tablename__ = "ai_backtest_jobs"

    backtest_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    start_date: Mapped[date_] = mapped_column(Date, nullable=False)
    end_date: Mapped[date_] = mapped_column(Date, nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    # Phase 7 -- NULLABLE for the same backward-compat reason as
    # AIResearchRun.market (Section 34/35): a pre-Phase-7 backtest row has
    # no value here, interpreted as "US".
    market: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Same configuration shape as a single AIResearchRun (Section 5: reuse,
    # not a parallel config schema) -- provider/model NAMES only.
    selected_analysts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    research_depth: Mapped[str] = mapped_column(String(16), nullable=False)
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    quick_think_llm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deep_think_llm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    debate_rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_debate_rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_vendors: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    holding_days: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED", index=True)
    total_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_date: Mapped[date_ | None] = mapped_column(Date, nullable=True)
    # Cooperative cancellation (Section 22): checked between cells, never
    # kills mid-flight work -- see service.py's _run_backtest loop.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Aggregate "AI Research Decision Evaluation" metrics (Section 15/16),
    # computed once at completion by evaluator.compute_metrics() -- never
    # a portfolio P&L/Sharpe/CAGR/drawdown figure (Section 16: no true
    # capital-allocation simulation exists here).
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cells: Mapped[list["AIBacktestRun"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="AIBacktestRun.symbol, AIBacktestRun.cell_date",
    )


class AIBacktestRun(Base):
    """One row per (symbol, date) grid cell. `research_id` links to the
    full, first-class AIResearchRun this cell created (Section 18) --
    drill-down reads that row directly rather than a duplicated report
    stored here a second time."""

    __tablename__ = "ai_backtest_runs"
    __table_args__ = (UniqueConstraint("backtest_id", "symbol", "cell_date", name="uq_ai_backtest_run_cell"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    backtest_id: Mapped[str] = mapped_column(ForeignKey("ai_backtest_jobs.backtest_id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cell_date: Mapped[date_] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    # Nullable, no hard FK enforcement needed beyond documentation intent --
    # a cell that FAILED before run_research() returned never gets one.
    research_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # Decision normalization (Section 12) -- raw_decision is the trimmed
    # final_trade_decision text; normalized_decision is upstream's OWN
    # parse_rating() output (Buy/Overweight/Hold/Underweight/Sell/REVIEW),
    # read back from TradingMemoryLog -- never re-derived/guessed here.
    raw_decision: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    normalized_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Evaluation (Section 13) -- populated only once upstream's OWN
    # settle_pending()/_resolve_pending_entries() has resolved this cell's
    # holding-window return via yfinance. PENDING until then -- never
    # fabricated with today's price.
    evaluation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # PENDING | RESOLVED
    raw_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    alpha_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark: Mapped[str | None] = mapped_column(String(16), nullable=True)
    holding_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_date: Mapped[date_ | None] = mapped_column(Date, nullable=True)

    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[AIBacktestJob] = relationship(back_populates="cells")
