"""
Request/response schemas for AI Research Backtesting. Reuses ai_research's
own schema pieces (AnalystKey, ResearchDepth, ResearchDisabledOut,
SUPPORTED_LLM_PROVIDERS symbol pattern) rather than duplicating them --
Section 5: "avoid duplicating the entire normal research service."

Structural validation (shape, per-field bounds, an absolute sanity
ceiling) lives here, in Pydantic. The actual CONFIGURABLE cost/workload
limits (AI_BACKTEST_MAX_SYMBOLS/DATES/RUNS -- Section 7) are enforced in
service.py at submission time, not here, because they are read from
runtime settings, not static schema constants -- and because a limit
violation must be reported with the ACTUAL configured ceiling in the
error message, which only the service layer has loaded.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading.ai_research.market.instruments import Market
from trading.ai_research.schemas import (
    AnalystKey,
    ResearchDepth,
    SUPPORTED_LLM_PROVIDERS,
    _SYMBOL_PATTERN,
)
from trading.ai_research.trading_agents_adapter import (
    BACKTEST_FREQUENCY_DAYS,
    RETRY_COUNT_MAX,
    RETRY_COUNT_MIN,
    SUPPORTED_DATA_VENDOR_CATEGORIES,
)

# Absolute structural ceiling on the request SHAPE itself (distinct from
# the configurable AI_BACKTEST_MAX_SYMBOLS business limit enforced in
# service.py) -- large enough to never bind ahead of any sane configured
# limit, small enough that a malformed/adversarial payload can't build an
# enormous in-memory list before validation even reaches the real limit.
_STRUCTURAL_MAX_SYMBOLS = 50


class BacktestFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class BacktestJobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BacktestCellStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BacktestRequestIn(BaseModel):
    """POST /api/ai-research/backtests body. Research-only, same as
    ResearchRequestIn: extra="forbid" rejects any broker/order/strategy
    -shaped field at the validation layer, before any handler code runs."""

    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=_STRUCTURAL_MAX_SYMBOLS)
    start_date: date
    end_date: date
    frequency: BacktestFrequency = BacktestFrequency.WEEKLY
    # Phase 7 (Section 20): defaults to US so every existing backtest
    # request keeps its exact prior grid-generation behavior.
    market: Market = Market.US

    selected_analysts: list[AnalystKey] | None = Field(default=None, max_length=4)
    research_depth: ResearchDepth = ResearchDepth.STANDARD
    llm_provider: str | None = Field(default=None, max_length=32)
    deep_think_llm: str | None = Field(default=None, max_length=64)
    quick_think_llm: str | None = Field(default=None, max_length=64)
    temperature: float | None = Field(default=None, ge=0, le=2)
    debate_rounds: int | None = Field(default=None, ge=1, le=5)
    risk_debate_rounds: int | None = Field(default=None, ge=1, le=5)
    retry_count: int | None = Field(default=None, ge=RETRY_COUNT_MIN, le=RETRY_COUNT_MAX)
    data_vendors: dict[str, str] | None = Field(default=None, max_length=6)
    # Forward-return evaluation horizon (trading days). None -> the
    # configured AI_BACKTEST_HOLDING_DAYS default (service.py).
    holding_days: int | None = Field(default=None, ge=1, le=60)

    @field_validator("symbols")
    @classmethod
    def _valid_symbols(cls, value: list[str]) -> list[str]:
        import re

        pattern = re.compile(_SYMBOL_PATTERN)
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in value:
            sym = raw.strip().upper()
            if not pattern.match(sym):
                raise ValueError(f"invalid symbol {raw!r}")
            if sym in seen:
                raise ValueError(f"duplicate symbol {raw!r} in request")
            seen.add(sym)
            normalized.append(sym)
        return normalized

    @field_validator("end_date")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("end_date cannot be in the future")
        return value

    @field_validator("start_date")
    @classmethod
    def _start_not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("start_date cannot be in the future")
        return value

    @field_validator("data_vendors")
    @classmethod
    def _known_vendors(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        for category, vendor in value.items():
            allowed = SUPPORTED_DATA_VENDOR_CATEGORIES.get(category)
            if allowed is None:
                raise ValueError(f"unsupported data vendor category {category!r}")
            if vendor not in allowed:
                raise ValueError(f"unsupported vendor {vendor!r} for category {category!r}; supported: {allowed}")
        return value

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, value: str | None) -> str | None:
        if value is not None and value.lower() not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"unsupported llm_provider {value!r}; supported: {SUPPORTED_LLM_PROVIDERS}")
        return value.lower() if value else value

    @field_validator("frequency")
    @classmethod
    def _known_frequency(cls, value: BacktestFrequency) -> BacktestFrequency:
        if value.value not in BACKTEST_FREQUENCY_DAYS:
            raise ValueError(f"unsupported frequency {value!r}")
        return value

    def model_post_init(self, __context) -> None:  # noqa: D401 -- pydantic hook
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")


class BacktestEstimateOut(BaseModel):
    """Response to POST /api/ai-research/backtests/estimate -- Section 24's
    "Estimated research runs: N" figure, computed from the SAME real grid
    function the actual submission uses (never a separate, potentially
    inconsistent estimate). A RUN-COUNT estimate only, never a monetary
    cost estimate (Section 24's own instruction)."""

    total_runs: int
    symbol_count: int
    date_count: int
    dates: list[date]
    exceeds_limit: bool
    limit_message: str | None = None
    limits: "BacktestLimitsOut"


class BacktestLimitsOut(BaseModel):
    max_symbols: int
    max_dates: int
    max_runs: int
    max_concurrent: int
    default_holding_days: int


BacktestEstimateOut.model_rebuild()


class BacktestSubmittedOut(BaseModel):
    backtest_id: str
    status: BacktestJobStatus
    total_runs: int
    created_at: datetime


class BacktestStatusOut(BaseModel):
    """GET /api/ai-research/backtests/{id}/status -- Section 9: progress
    derived ONLY from real completed/failed run counts, never an
    agent-level fabricated percentage."""

    backtest_id: str
    status: BacktestJobStatus
    total_runs: int
    completed_runs: int
    failed_runs: int
    remaining_runs: int
    progress: float  # (completed_runs + failed_runs) / total_runs, honest
    current_symbol: str | None = None
    current_date: date | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class BacktestCellOut(BaseModel):
    symbol: str
    cell_date: date
    status: BacktestCellStatus
    research_id: str | None = None
    raw_decision: str | None = None
    normalized_decision: str | None = None
    evaluation_status: str | None = None
    raw_return: float | None = None
    alpha_return: float | None = None
    benchmark: str | None = None
    holding_days: int | None = None
    resolution_date: date | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class BacktestRunsPageOut(BaseModel):
    items: list[BacktestCellOut]
    total: int
    page: int
    page_size: int


class DirectionalAccuracyOut(BaseModel):
    count: int
    hit_rate: float | None = None
    mean_alpha: float


class BacktestMetricsOut(BaseModel):
    """Section 15/16: "AI Research Decision Evaluation", explicitly never
    labeled or shaped as a trading-strategy/portfolio-P&L result. Directly
    mirrors upstream tradingagents.backtest.BacktestSummary's own scoring
    concept (direction-called hit rate + mean alpha per rating), computed
    over this backtest's own persisted cells."""

    label: str = "AI Research Decision Evaluation"
    total_decisions: int
    counts_by_decision: dict[str, int]
    resolved: int
    pending_evaluation: int
    unscored: int
    by_decision: dict[str, DirectionalAccuracyOut]
    overall_mean_alpha: float | None = None
    overall_median_alpha: float | None = None
    positive_decision_rate: float | None = None
    holding_days: int


class BacktestDetailOut(BaseModel):
    backtest_id: str
    status: BacktestJobStatus
    symbols: list[str]
    start_date: date
    end_date: date
    frequency: BacktestFrequency
    market: str = "US"
    research_depth: ResearchDepth
    llm_provider: str | None = None
    holding_days: int
    total_runs: int
    completed_runs: int
    failed_runs: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    metrics: BacktestMetricsOut | None = None


class BacktestHistoryEntryOut(BaseModel):
    backtest_id: str
    symbols: list[str]
    start_date: date
    end_date: date
    frequency: BacktestFrequency
    market: str = "US"
    status: BacktestJobStatus
    total_runs: int
    completed_runs: int
    failed_runs: int
    created_at: datetime
    completed_at: datetime | None = None


class BacktestHistoryPageOut(BaseModel):
    items: list[BacktestHistoryEntryOut]
    total: int
    page: int
    page_size: int


class BacktestResumedOut(BaseModel):
    backtest_id: str
    status: BacktestJobStatus
    resumed_pending_cells: int


class BacktestCancelledOut(BaseModel):
    backtest_id: str
    status: BacktestJobStatus


class DataQualitySourceOut(BaseModel):
    source: str
    safe: str  # "YES" | "NO" | "PARTIAL" | "WITHHELD"
    detail: str


class DataQualityMatrixOut(BaseModel):
    """Section 28: the bias/data-quality disclosure -- static per pinned
    TradingAgents version (see trading_agents_adapter.POINT_IN_TIME_SAFETY,
    grounded in direct upstream source reading), not computed per-backtest.
    Always shown alongside results; never hidden behind aggregate metrics."""

    sources: list[DataQualitySourceOut]
    summary: str
