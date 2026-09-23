"""Phase 10 -- API/persistence request & response schemas (Pydantic).
Mirrors ``trading/ai_research/schemas.py``'s conventions exactly."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trading.ai_options_research.strategy_templates import StrategyType

# Mirrors trading/ai_research/schemas.py's SUPPORTED_LLM_PROVIDERS exactly.
SUPPORTED_LLM_PROVIDERS: tuple[str, ...] = (
    "openai", "google", "anthropic", "xai", "deepseek", "qwen", "glm", "minimax",
    "openrouter", "mistral", "moonshot", "groq", "nvidia", "ollama", "openai_compatible",
    "bedrock", "azure",
)

ALL_STRATEGIES: tuple[StrategyType, ...] = tuple(StrategyType)
DEFINED_RISK_STRATEGIES: tuple[StrategyType, ...] = tuple(
    s for s in StrategyType
    if s not in (StrategyType.SHORT_STRADDLE, StrategyType.SHORT_STRANGLE, StrategyType.LONG_CALL)
)


class StrategyUniversePreset(str, Enum):
    ALL_SUPPORTED = "ALL_SUPPORTED"
    DEFINED_RISK_ONLY = "DEFINED_RISK_ONLY"
    CUSTOM = "CUSTOM"


class ResearchJobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OptionsResearchRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    underlying: str = Field(min_length=1, max_length=32)
    expiry: str = Field(default="nearest", max_length=16, description='"nearest" | "next" | ISO date')
    as_of: datetime | None = None

    strategy_universe_preset: StrategyUniversePreset = StrategyUniversePreset.DEFINED_RISK_ONLY
    strategy_universe: list[StrategyType] | None = Field(default=None, max_length=len(ALL_STRATEGIES))

    candidate_limit: int = Field(default=3, ge=1, le=10)
    debate_rounds: int = Field(default=1, ge=1, le=5)

    llm_provider: str | None = Field(default=None, max_length=32)
    quick_think_llm: str | None = Field(default=None, max_length=64)
    deep_think_llm: str | None = Field(default=None, max_length=64)
    temperature: float | None = Field(default=None, ge=0, le=2)

    strike_window: int = Field(default=10, ge=1, le=50)
    min_delta: float = Field(default=0.15, ge=0.01, le=0.5)
    max_delta: float = Field(default=0.30, ge=0.01, le=0.5)
    minimum_volume: int = Field(default=0, ge=0)
    minimum_oi: int = Field(default=0, ge=0)
    maximum_bid_ask_spread: float | None = Field(default=None, ge=0)
    wing_width: float = Field(default=100.0, gt=0)
    hedge_wing_width: float = Field(default=300.0, gt=0)

    @field_validator("llm_provider")
    @classmethod
    def _validate_provider(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(f"unsupported llm_provider {v!r}; supported: {SUPPORTED_LLM_PROVIDERS}")
        return v

    @field_validator("max_delta")
    @classmethod
    def _max_delta_above_min(cls, v: float, info) -> float:
        min_delta = info.data.get("min_delta")
        if min_delta is not None and v < min_delta:
            raise ValueError("max_delta must be >= min_delta")
        return v

    def resolved_strategy_universe(self) -> list[StrategyType]:
        """Section 43: explicit default is DEFINED_RISK_ONLY -- unsupported/
        undefined-risk strategies are never silently included."""
        if self.strategy_universe_preset == StrategyUniversePreset.CUSTOM:
            return list(self.strategy_universe or [])
        if self.strategy_universe_preset == StrategyUniversePreset.ALL_SUPPORTED:
            return list(ALL_STRATEGIES)
        return list(DEFINED_RISK_STRATEGIES)


class LegOut(BaseModel):
    side: str
    option_type: str
    strike: float
    price: float | None
    price_source: str
    delta: float | None
    iv: float | None
    volume: int | None
    open_interest: int | None
    bid: float | None
    ask: float | None
    liquidity_ok: bool
    liquidity_reasons: list[str]


class PayoffOut(BaseModel):
    net_premium: float | None
    max_profit: float | None
    max_profit_unbounded: bool
    max_loss: float | None
    max_loss_unbounded: bool
    breakevens: list[float]
    pricing_method: str
    priced: bool
    payoff_curve: list[dict]


class CandidateOut(BaseModel):
    strategy: str
    supported: bool
    legs: list[LegOut]
    payoff: PayoffOut | None
    liquidity_ok: bool
    generation_notes: list[str]
    unsupported_reason: str | None
    commentary: dict | None = None
    risk_analysis: dict | None = None


class SnapshotPreviewOut(BaseModel):
    """Section 46 -- shown before an expensive AI run is started."""
    underlying: str
    spot: float | None
    expiry: date | None
    atm_strike: float | None
    dte_calendar: int | None
    dte_trading: int | None
    oi_pcr: float | None
    max_pain_strike: float | None
    atm_iv: float | None
    expected_move: float | None
    oi_support: list[float]
    oi_resistance: list[float]
    data_quality: str
    data_quality_reasons: list[str]
    evidence_quality: str
    evidence_quality_reason: str


class ResearchReportOut(BaseModel):
    market_overview: str
    market_regime: dict
    technical_context: str
    news_sentiment: str
    options_market_structure: dict
    volatility_analysis: dict
    oi_positioning_analysis: dict
    bull_case: dict
    bear_case: dict
    candidates: list[CandidateOut]
    invalidation_conditions: list[str]
    final_summary: str
    sanitizer_warnings: list[str] = Field(default_factory=list)


class ResearchStatusOut(BaseModel):
    research_id: str
    underlying: str
    status: str
    current_stage: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None = None
    provider_error: str | None = None
    research_snapshot_id: str | None = None
    evidence_quality: str | None = None


class ResearchResultOut(ResearchStatusOut):
    report: ResearchReportOut | None = None


class StageOut(BaseModel):
    name: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None


class StagesOut(BaseModel):
    stages: list[StageOut]
    current_stage: str | None


class HistoryItemOut(BaseModel):
    research_id: str
    underlying: str
    expiry: str | None
    status: str
    market_regime_trend: str | None
    candidate_count: int
    created_at: datetime


class HistoryOut(BaseModel):
    items: list[HistoryItemOut]
    total: int
    page: int
    page_size: int


class ResearchDisabledOut(BaseModel):
    disabled: bool = True
    reason: str = "AI Options Research is disabled (set AI_RESEARCH_ENABLED=true)"


def is_disabled(payload: object) -> bool:
    return isinstance(payload, ResearchDisabledOut) or (isinstance(payload, dict) and payload.get("disabled") is True)
