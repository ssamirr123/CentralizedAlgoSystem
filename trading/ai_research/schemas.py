"""
Request/response schemas for the AI Research Engine API.

Every request schema uses `model_config = ConfigDict(extra="forbid")` so an
unexpected field (broker, quantity, side, ...) is a hard 422 validation
error, not a silently-ignored extra key -- see
tests/ai_research/test_isolation.py's execution-field-rejection tests.

Config field names/semantics are grounded in the real upstream v0.5.0
source (confirmed during Phase 2 -- see trading_agents_adapter.py's
module docstring for exact file references), not invented:
    selected_analysts    -> TradingAgentsGraph(selected_analysts=...)
    llm_provider/models  -> config["llm_provider"/"deep_think_llm"/"quick_think_llm"]
    temperature          -> config["temperature"]
    debate_rounds        -> config["max_debate_rounds"]
    risk_debate_rounds   -> config["max_risk_discuss_rounds"]
    checkpoint_enabled   -> config["checkpoint_enabled"]
    portfolio            -> tradingagents.portfolio.PortfolioContext/Position

research_depth is OUR OWN convenience layer on top of the real knobs above
(Section 8): it sets debate_rounds/risk_debate_rounds defaults when the
caller doesn't specify them explicitly, it does not replace them --
mapping:
    quick     -> debate_rounds=1, risk_debate_rounds=1 (upstream's own default)
    standard  -> debate_rounds=1, risk_debate_rounds=1 (same -- upstream's
                 own default is already the lean end of the scale; see
                 Phase 1 finding)
    deep      -> debate_rounds=2, risk_debate_rounds=2
An explicit debate_rounds/risk_debate_rounds in the request always wins
over the research_depth default.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading.ai_research.market.instruments import Market
from trading.ai_research.trading_agents_adapter import (
    RETRY_COUNT_MAX,
    RETRY_COUNT_MIN,
    SUPPORTED_DATA_VENDOR_CATEGORIES,
)

# Real upstream analyst keys (tradingagents/graph/analyst_execution.py).
SUPPORTED_ANALYSTS = ("market", "social", "news", "fundamentals")

# Providers TradingAgents v0.5.0 actually supports (confirmed from its
# README's "Required APIs" section during Phase 1/2 -- not hardcoded to
# OpenAI). "openai_compatible"/"ollama"/"bedrock"/"azure" need additional
# config (backend_url / AWS creds / enterprise .env) beyond what this API
# accepts in Phase 2; they are listed for discoverability
# (GET /api/ai-research/config/options) but using them requires backend
# environment configuration outside this request schema.
SUPPORTED_LLM_PROVIDERS = (
    "openai", "google", "anthropic", "xai", "deepseek", "qwen", "glm",
    "minimax", "openrouter", "mistral", "moonshot", "groq", "nvidia",
    "ollama", "openai_compatible", "bedrock", "azure",
)

_DEPTH_DEBATE_ROUNDS = {"quick": 1, "standard": 1, "deep": 2}
_DEPTH_RISK_ROUNDS = {"quick": 1, "standard": 1, "deep": 2}


class ResearchDepth(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class ResearchJobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ResearchStage(str, Enum):
    """Coarse progress buckets grouping TradingAgents' real graph node
    names -- see trading_agents_adapter.py's stage_bucket_for_node() for
    the exact node-name mapping this is derived from. Kept as a simple
    /status summary field; /stages exposes the real per-node list this is
    computed from (see NodeStageOut below)."""

    QUEUED = "QUEUED"
    ANALYSTS = "ANALYSTS"
    RESEARCH_DEBATE = "RESEARCH_DEBATE"
    TRADER = "TRADER"
    RISK = "RISK"
    PORTFOLIO_MANAGER = "PORTFOLIO_MANAGER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class NodeStageStatus(str, Enum):
    """Per-real-node status (Section 6 of Phase 3). RUNNING is INFERRED
    (the next not-yet-completed node in deterministic order after the
    most recently completed one) -- LangGraph's "updates" stream reports
    completions, not a separate "started" event; see
    trading_agents_adapter.py's module docstring for why."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class NodeStageOut(BaseModel):
    """One real TradingAgents graph node's progress. `name` is the
    ACTUAL upstream node name (e.g. "Market Analyst", "Bull Researcher",
    "Portfolio Manager") -- never an invented label."""

    name: str
    status: NodeStageStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None


class AnalystKey(str, Enum):
    MARKET = "market"
    SOCIAL = "social"  # upstream's wire key for the Sentiment Analyst
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"


# A ticker/symbol as TradingAgents (Yahoo Finance) expects it -- letters,
# digits, dot, caret, hyphen (covers "AAPL", "RELIANCE.NS", "^NSEI",
# "BTC-USD"). "&" added in Phase 7 for real NSE symbols like "M&M"
# (Mahindra & Mahindra). Deliberately narrow: this is a research
# instrument identifier, not a free-text field.
_SYMBOL_PATTERN = r"^[A-Za-z0-9.\^\-&]{1,32}$"


class PortfolioPositionIn(BaseModel):
    """Mirrors tradingagents.portfolio.Position exactly."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=32, pattern=_SYMBOL_PATTERN)
    quantity: float
    average_price: float | None = Field(default=None, ge=0)


class PortfolioContextIn(BaseModel):
    """Optional, explicit research context only (Section 14) -- mirrors
    tradingagents.portfolio.PortfolioContext exactly. Never populated from
    a real broker position in this phase; a caller must supply it (or a
    safe mock) themselves."""

    model_config = ConfigDict(extra="forbid")

    cash: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    positions: list[PortfolioPositionIn] = Field(default_factory=list, max_length=50)


class ResearchRequestIn(BaseModel):
    """POST /api/ai-research body. Research-only -- see the module
    docstring: any execution-shaped field (broker, quantity, side, ...) is
    REJECTED by extra="forbid", not silently dropped."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32, pattern=_SYMBOL_PATTERN)
    research_date: date
    research_depth: ResearchDepth = ResearchDepth.STANDARD

    # Phase 7 (Section 16): defaults to US so every existing caller/test
    # that never sends this field keeps its exact prior behavior
    # (Section 30/35 -- additive, backward compatible).
    market: Market = Market.US

    selected_analysts: list[AnalystKey] | None = Field(default=None, max_length=4)
    llm_provider: str | None = Field(default=None, max_length=32)
    deep_think_llm: str | None = Field(default=None, max_length=64)
    quick_think_llm: str | None = Field(default=None, max_length=64)
    temperature: float | None = Field(default=None, ge=0, le=2)
    debate_rounds: int | None = Field(default=None, ge=1, le=5)
    risk_debate_rounds: int | None = Field(default=None, ge=1, le=5)
    retry_count: int | None = Field(default=None, ge=RETRY_COUNT_MIN, le=RETRY_COUNT_MAX)
    data_vendors: dict[str, str] | None = Field(default=None, max_length=6)
    checkpoint_enabled: bool = False
    portfolio: PortfolioContextIn | None = None

    @field_validator("data_vendors")
    @classmethod
    def _known_vendors(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        for category, vendor in value.items():
            allowed = SUPPORTED_DATA_VENDOR_CATEGORIES.get(category)
            if allowed is None:
                raise ValueError(f"unsupported data vendor category {category!r}; supported: {tuple(SUPPORTED_DATA_VENDOR_CATEGORIES)}")
            if vendor not in allowed:
                raise ValueError(f"unsupported vendor {vendor!r} for category {category!r}; supported: {allowed}")
        return value

    @field_validator("research_date")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        # Mirrors TradingAgents' own _validate_trade_date (Phase 0/1
        # finding): a run dated in the future has no market data to
        # research.
        if value > date.today():
            raise ValueError("research_date cannot be in the future")
        return value

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, value: str | None) -> str | None:
        if value is not None and value.lower() not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"unsupported llm_provider {value!r}; supported: {SUPPORTED_LLM_PROVIDERS}")
        return value.lower() if value else value

    def resolved_debate_rounds(self) -> int:
        return self.debate_rounds if self.debate_rounds is not None else _DEPTH_DEBATE_ROUNDS[self.research_depth.value]

    def resolved_risk_debate_rounds(self) -> int:
        return self.risk_debate_rounds if self.risk_debate_rounds is not None else _DEPTH_RISK_ROUNDS[self.research_depth.value]


class ResearchSubmittedOut(BaseModel):
    """Immediate response to POST -- the request is queued, not executed
    inline. See jobs.py for the QUEUED -> RUNNING -> ... ->
    COMPLETED/FAILED lifecycle this research_id tracks."""

    research_id: str
    status: ResearchJobStatus
    created_at: datetime


class ResearchStatusOut(BaseModel):
    research_id: str
    symbol: str
    research_date: date
    research_depth: ResearchDepth
    status: ResearchJobStatus
    stage: ResearchStage
    current_stage: str | None = None  # real node name currently RUNNING/last COMPLETED, or None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    provider_error: str | None = None  # e.g. "RATE_LIMIT" -- see Section 16
    # Phase 7 (Section 29): result-header market context.
    market: str = "US"
    exchange: str = "GENERIC"
    currency: str = "USD"
    market_data_provenance: dict | None = None


class ResearchReportOut(BaseModel):
    """Structured TradingAgents output, normalized from the real
    AgentState field names (see trading_agents_adapter.py's module
    docstring for exact references). A field is None, never fabricated,
    when TradingAgents itself didn't populate it for this run (e.g. an
    analyst was excluded via selected_analysts)."""

    market_analysis: str | None = None
    news_analysis: str | None = None
    sentiment_analysis: str | None = None
    fundamentals_analysis: str | None = None
    bull_case: str | None = None
    bear_case: str | None = None
    research_manager_decision: str | None = None
    trader_plan: str | None = None
    risk_aggressive: str | None = None
    risk_conservative: str | None = None
    risk_neutral: str | None = None
    risk_judge_decision: str | None = None
    final_trade_decision: str | None = None
    # One of Buy/Overweight/Hold/Underweight/Sell, or "REVIEW" -- research
    # output only, never executed. See trading_agents_adapter.py's
    # EXECUTION ISOLATION docstring section.
    signal: str | None = None


class ResearchResultOut(BaseModel):
    research_id: str
    symbol: str
    research_date: date
    research_depth: ResearchDepth
    status: ResearchJobStatus
    stage: ResearchStage
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    report: ResearchReportOut | None = None
    error: str | None = None
    provider_error: str | None = None
    market: str = "US"
    exchange: str = "GENERIC"
    currency: str = "USD"
    market_data_provenance: dict | None = None


class ResearchHistoryEntryOut(BaseModel):
    research_id: str
    symbol: str
    research_date: date
    research_depth: ResearchDepth
    status: ResearchJobStatus
    llm_provider: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    market: str = "US"
    currency: str = "USD"


class ResearchHistoryPageOut(BaseModel):
    """Phase 5: database-backed, paginated history (Section 16) --
    replaces Phase 2-4's unbounded in-memory list. `total` is the full
    filtered count, independent of page_size, so a client can compute
    total pages."""

    items: list[ResearchHistoryEntryOut]
    total: int
    page: int
    page_size: int


class ResearchDisabledOut(BaseModel):
    """Returned (HTTP 200, not an error) by every AI Research endpoint
    when AI_RESEARCH_ENABLED=false -- the default. Distinguishes
    "feature intentionally off" from a real failure."""

    enabled: bool = False
    message: str = "AI Research is disabled (AI_RESEARCH_ENABLED=false)"


class ResearchConfigOptionsOut(BaseModel):
    """GET /api/ai-research/config/options -- lets a future frontend
    discover valid choices without hardcoding them. enabled=false means
    every other field reflects what WOULD be available if enabled; it
    never implies research can run right now."""

    enabled: bool
    analysts: tuple[str, ...] = SUPPORTED_ANALYSTS
    llm_providers: tuple[str, ...] = SUPPORTED_LLM_PROVIDERS
    research_depths: tuple[str, ...] = tuple(d.value for d in ResearchDepth)
    debate_rounds_range: tuple[int, int] = (1, 5)
    risk_debate_rounds_range: tuple[int, int] = (1, 5)
    retry_count_range: tuple[int, int] = (RETRY_COUNT_MIN, RETRY_COUNT_MAX)
    data_vendors: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(SUPPORTED_DATA_VENDOR_CATEGORIES)
    )
    max_concurrent: int
    timeout_seconds: int


class StagesOut(BaseModel):
    """GET /api/ai-research/{research_id}/stages -- REAL graph progress
    (Phase 3), not the cosmetic Phase 2 placeholder. `stages` is ordered
    per the deterministic execution plan for this job's selected_analysts."""

    research_id: str
    status: ResearchJobStatus
    current_stage: str | None = None
    stages: list[NodeStageOut]


class MemoryEntryOut(BaseModel):
    """One tradingagents decision-memory entry. memory_id is a purely
    logical key (trade_date:ticker), never a filesystem path."""

    memory_id: str
    ticker: str
    trade_date: str
    rating: str | None = None
    pending: bool
    decision: str | None = None
    reflection: str | None = None


class ResumeRequestedOut(BaseModel):
    """Response to POST /api/ai-research/{research_id}/resume -- a NEW
    research_id running with the same parameters as the original job.
    See router.py's docstring for exactly what "resume" means here and
    why (upstream checkpoints are keyed by ticker+date+config-signature,
    not by our own research_id)."""

    original_research_id: str
    new_research_id: str
    status: ResearchJobStatus
    resumed_from_checkpoint: bool  # True only if the original had checkpoint_enabled=True
