"""
Phase 10 (Section 62/63) -- schema-constrained LLM agent outputs.

Every agent returns one of these Pydantic models via the LLM provider's
structured-output mode, never free-form text alone. Any field that could
carry a "fact" the LLM might invent (a strike/price/level) is a typed
numeric field, sanitized against the frozen ``OptionsResearchContext`` by
``sanitize_key_levels``/``validate_candidate_index`` in this module
before the value is trusted anywhere downstream (Section 61: the
hallucination guard).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from trading.ai_options_research.context import OptionsResearchContext


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"
    UNCERTAIN = "UNCERTAIN"


class VolatilityRegime(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class MarketStructureClass(str, Enum):
    TRENDING = "TRENDING"
    RANGE_BOUND = "RANGE_BOUND"
    BREAKOUT = "BREAKOUT"
    UNCERTAIN = "UNCERTAIN"


class MarketRegimeOutput(BaseModel):
    trend: Trend
    volatility_regime: VolatilityRegime
    market_structure: MarketStructureClass
    evidence: str = Field(description="What deterministic facts from the context support this classification")
    confidence: Confidence
    limitations: str = Field(default="", description="What evidence is missing/insufficient")


class VolatilityAnalysisOutput(BaseModel):
    interpretation: str
    notable_observations: list[str] = Field(default_factory=list)
    confidence: Confidence


class OIPositioningOutput(BaseModel):
    interpretation: str = Field(description='Must use probabilistic language ("suggests", "consistent with"), never claim OI proves direction')
    notable_observations: list[str] = Field(default_factory=list)
    confidence: Confidence


class CandidateCommentary(BaseModel):
    candidate_index: int
    fits_reasoning: str
    invalidation: str
    market_assumptions: str
    volatility_assumptions: str
    key_levels: list[float] = Field(default_factory=list, description="Only strikes/spot/ATM that actually appear in the supplied context")


class StrategyResearchOutput(BaseModel):
    commentary: list[CandidateCommentary]


class DirectionalCaseOutput(BaseModel):
    thesis: str
    supporting_evidence: list[str] = Field(default_factory=list)
    key_levels: list[float] = Field(default_factory=list)


class CandidateRiskAnalysis(BaseModel):
    candidate_index: int
    directional_exposure: str
    volatility_exposure: str
    theta_exposure: str
    gamma_risk: str
    gap_risk: str
    expiry_risk: str
    liquidity_risk: str
    assignment_considerations: str
    event_risk: str
    tail_risk: str


class RiskAnalysisOutput(BaseModel):
    analyses: list[CandidateRiskAnalysis]


class FinalReportOutput(BaseModel):
    market_overview: str
    technical_context: str
    news_sentiment: str = Field(default="Not available in this phase")
    options_market_structure_summary: str
    invalidation_conditions: list[str] = Field(default_factory=list)
    final_summary: str


# --------------------------------------------------------------------------
# Hallucination guard (Section 61)
# --------------------------------------------------------------------------
_LEVEL_TOLERANCE = 1e-6


def _known_levels(context: OptionsResearchContext) -> set[float]:
    levels = set(context.valid_strikes)
    if context.spot is not None:
        levels.add(round(context.spot, 2))
    if context.atm_strike is not None:
        levels.add(context.atm_strike)
    if context.max_pain_strike is not None:
        levels.add(context.max_pain_strike)
    levels.update(context.oi_support)
    levels.update(context.oi_resistance)
    return levels


def sanitize_key_levels(levels: list[float], context: OptionsResearchContext) -> tuple[list[float], list[float]]:
    """Returns (kept, rejected). A level is kept only if it matches a
    real strike, the spot, the ATM strike, Max Pain, or an OI support/
    resistance level already present in the frozen context -- anything
    else is a hallucinated fact and is dropped, never persisted or
    displayed as authoritative."""
    known = _known_levels(context)
    kept, rejected = [], []
    for level in levels:
        if any(abs(level - k) <= max(_LEVEL_TOLERANCE, k * 1e-6) for k in known):
            kept.append(level)
        else:
            rejected.append(level)
    return kept, rejected


def validate_candidate_index(index: int, candidate_count: int) -> bool:
    return 0 <= index < candidate_count


def sanitize_candidate_commentary(output: StrategyResearchOutput, context: OptionsResearchContext, candidate_count: int) -> tuple[StrategyResearchOutput, list[str]]:
    warnings: list[str] = []
    cleaned: list[CandidateCommentary] = []
    for item in output.commentary:
        if not validate_candidate_index(item.candidate_index, candidate_count):
            warnings.append(f"dropped commentary referencing out-of-range candidate_index={item.candidate_index}")
            continue
        kept, rejected = sanitize_key_levels(item.key_levels, context)
        if rejected:
            warnings.append(f"candidate {item.candidate_index}: dropped hallucinated levels {rejected}")
        cleaned.append(item.model_copy(update={"key_levels": kept}))
    return StrategyResearchOutput(commentary=cleaned), warnings


def sanitize_directional_case(output: DirectionalCaseOutput, context: OptionsResearchContext) -> tuple[DirectionalCaseOutput, list[str]]:
    kept, rejected = sanitize_key_levels(output.key_levels, context)
    warnings = [f"dropped hallucinated levels {rejected}"] if rejected else []
    return output.model_copy(update={"key_levels": kept}), warnings


def sanitize_risk_analysis(output: RiskAnalysisOutput, candidate_count: int) -> tuple[RiskAnalysisOutput, list[str]]:
    warnings: list[str] = []
    cleaned = []
    for item in output.analyses:
        if not validate_candidate_index(item.candidate_index, candidate_count):
            warnings.append(f"dropped risk analysis referencing out-of-range candidate_index={item.candidate_index}")
            continue
        cleaned.append(item)
    return RiskAnalysisOutput(analyses=cleaned), warnings
