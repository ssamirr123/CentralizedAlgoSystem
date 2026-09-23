"""
Phase 10 (Section 7-10, 18, 21-24, 31) -- the LLM agent layer.

Every agent function here:
  * receives the SAME frozen ``OptionsResearchContext`` (Section 5) plus
    already-computed ``StrategyCandidate`` objects (Section 63: never
    invents legs/strikes);
  * calls the LLM via ``create_options_research_llm`` (the one sanctioned
    tradingagents-importing module, Section 57: no separate API-key
    system) using structured output (Section 62) so its response is a
    typed Pydantic object, not free text to parse;
  * has its numeric "key_levels"/candidate-index fields sanitized against
    the frozen context immediately after the call returns (Section 61 --
    see ``agent_schemas.sanitize_*``), before the result is used by any
    later agent or persisted.

No agent function here can place, modify, or cancel an order, start/stop
a strategy, or touch TRADING_MODE/account routing -- none of that surface
is even importable from this module.
"""
from __future__ import annotations

from trading.ai_options_research.agent_schemas import (
    DirectionalCaseOutput,
    FinalReportOutput,
    MarketRegimeOutput,
    OIPositioningOutput,
    RiskAnalysisOutput,
    StrategyResearchOutput,
    VolatilityAnalysisOutput,
    sanitize_candidate_commentary,
    sanitize_directional_case,
    sanitize_risk_analysis,
)
from trading.ai_options_research.candidates import StrategyCandidate
from trading.ai_options_research.context import OptionsResearchContext
from trading.ai_research.trading_agents_adapter import create_options_research_llm

_STRUCTURED_OUTPUT_RETRIES = 2  # bounded -- never an unbounded retry loop


def _invoke_structured(llm, schema, prompt: str):
    """Some OpenAI-compatible providers intermittently fail strict
    tool-calling structured output ("Tool choice is required, but model
    did not call a tool") even when the same call type succeeds on a
    different invocation -- an external reliability issue, not an
    application defect. One bounded retry absorbs that transient failure
    without ever looping unboundedly; if it still fails, the real
    exception propagates and is classified/reported honestly (Section 55)."""
    structured = llm.with_structured_output(schema)
    for attempt in range(_STRUCTURED_OUTPUT_RETRIES):
        try:
            return structured.invoke(prompt)
        except Exception:  # noqa: BLE001 -- retried once, then re-raised for real classification
            if attempt == _STRUCTURED_OUTPUT_RETRIES - 1:
                raise
    raise AssertionError("unreachable")


def build_context_facts_text(context: OptionsResearchContext) -> str:
    """Section 3: the ONLY numbers an agent ever sees are these
    already-computed Phase 9 facts -- it is never asked to derive them."""
    lines = [
        f"Underlying: {context.underlying}",
        f"Research timestamp: {context.research_timestamp} (as_of={'LIVE' if context.as_of is None else context.as_of})",
        f"Spot: {context.spot}",
        f"Expiry: {context.expiry}  DTE calendar={context.dte_calendar}  DTE trading={context.dte_trading}",
        f"ATM strike: {context.atm_strike}",
        f"Total Call OI: {context.total_call_oi}  Total Put OI: {context.total_put_oi}",
        f"OI PCR: {context.oi_pcr}  Volume PCR: {context.volume_pcr}",
        f"Max Pain: {context.max_pain_strike}",
        f"OI-derived support: {list(context.oi_support)}  OI-derived resistance: {list(context.oi_resistance)}",
        f"ATM IV: {context.atm_iv} (source={context.atm_iv_source})",
        f"IV Rank: {context.iv_rank} IV Percentile: {context.iv_percentile} (status={context.iv_rank_status})",
        f"Expected Move: {context.expected_move} (method={context.expected_move_method})",
        f"Data quality: {context.data_quality.value} -- reasons: {list(context.data_quality_reasons)}",
    ]
    return "\n".join(lines)


def _candidates_text(candidates: list[StrategyCandidate]) -> str:
    blocks = []
    for i, c in enumerate(candidates):
        if not c.supported:
            blocks.append(f"[{i}] {c.strategy.value}: UNSUPPORTED ({c.unsupported_reason})")
            continue
        legs = "; ".join(f"{l.side.value} {l.option_type.value} {l.strike} @ {l.price} ({l.price_source})" for l in c.legs)
        payoff = c.payoff
        payoff_text = (
            f"net_premium={payoff.net_premium} max_profit={'UNBOUNDED' if payoff.max_profit_unbounded else payoff.max_profit} "
            f"max_loss={'UNBOUNDED/UNDEFINED' if payoff.max_loss_unbounded else payoff.max_loss} breakevens={list(payoff.breakevens)}"
            if payoff and payoff.priced else "not priced (missing quotes)"
        )
        blocks.append(f"[{i}] {c.strategy.value}: legs=[{legs}] payoff=({payoff_text}) liquidity_ok={c.liquidity_ok}")
    return "\n".join(blocks) if blocks else "(no candidates generated)"


_SYSTEM_PREAMBLE = (
    "You are a specialized options-market research analyst. You ONLY interpret facts already "
    "given to you -- you never calculate PCR, Max Pain, IV, Greeks, ATM, DTE, expected move, or OI "
    "totals yourself; those are provided as authoritative, already-computed values. You never "
    "recommend placing, modifying, or cancelling an order, and you never claim a structure will "
    "definitely be profitable. Use probabilistic language (\"suggests\", \"consistent with\", "
    "\"indicates positioning\"), never deterministic claims about future price direction. If "
    "evidence is insufficient for a confident classification, say so explicitly rather than forcing one."
)


def run_market_regime_agent(context: OptionsResearchContext, llm) -> MarketRegimeOutput:
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nClassify the market regime from these facts:\n{build_context_facts_text(context)}\n\n"
        "Classify trend (BULLISH/BEARISH/SIDEWAYS/UNCERTAIN), volatility_regime (LOW/NORMAL/HIGH/UNKNOWN -- "
        "base this on ATM IV / IV Rank if available, UNKNOWN if insufficient), and market_structure "
        "(TRENDING/RANGE_BOUND/BREAKOUT/UNCERTAIN). Do not force a classification the facts do not support."
    )
    return _invoke_structured(llm, MarketRegimeOutput, prompt)


def run_volatility_agent(context: OptionsResearchContext, llm) -> VolatilityAnalysisOutput:
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nAnalyze volatility from these facts:\n{build_context_facts_text(context)}\n\n"
        "Discuss ATM IV level, expected move, and IV Rank/Percentile if AVAILABLE (if NOT_AVAILABLE, say so "
        "explicitly rather than guessing a rank). Clearly separate the OBSERVED deterministic values above "
        "from your own interpretation of what they suggest about current option pricing."
    )
    return _invoke_structured(llm, VolatilityAnalysisOutput, prompt)


def run_oi_positioning_agent(context: OptionsResearchContext, llm) -> OIPositioningOutput:
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nInterpret OI positioning from these facts:\n{build_context_facts_text(context)}\n\n"
        "Discuss Call/Put OI, OI PCR, Volume PCR, Max Pain, and OI-derived support/resistance. "
        "Do NOT state that OI proves future direction -- use probabilistic/positioning language only."
    )
    return _invoke_structured(llm, OIPositioningOutput, prompt)


def run_strategy_research_agent(
    context: OptionsResearchContext, candidates: list[StrategyCandidate], regime: MarketRegimeOutput,
    volatility: VolatilityAnalysisOutput, oi: OIPositioningOutput, llm,
) -> tuple[StrategyResearchOutput, list[str]]:
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nFacts:\n{build_context_facts_text(context)}\n\n"
        f"Market Regime: {regime.model_dump()}\nVolatility: {volatility.model_dump()}\nOI Positioning: {oi.model_dump()}\n\n"
        f"Candidate structures (index in brackets; use ONLY these exact candidate_index values, "
        f"and ONLY strikes/levels that literally appear in the facts above):\n{_candidates_text(candidates)}\n\n"
        "For EACH candidate index, provide: why it fits the observed conditions, what would invalidate "
        "the thesis, the market assumptions it relies on, the volatility assumptions it relies on, and "
        "any key price levels (must be real strikes/spot/ATM/Max Pain/OI levels from the facts, nothing invented)."
    )
    result = _invoke_structured(llm, StrategyResearchOutput, prompt)
    return sanitize_candidate_commentary(result, context, len(candidates))


def run_bull_researcher(context: OptionsResearchContext, candidates: list[StrategyCandidate], counter_argument: str | None, llm) -> tuple[DirectionalCaseOutput, list[str]]:
    counter = f"\n\nThe Bear researcher's latest argument to address:\n{counter_argument}" if counter_argument else ""
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nYou are the BULL researcher. Build the strongest EVIDENCE-SUPPORTED "
        f"bullish interpretation using ONLY these facts (never invent data):\n{build_context_facts_text(context)}\n\n"
        f"Candidates for context:\n{_candidates_text(candidates)}{counter}"
    )
    result = _invoke_structured(llm, DirectionalCaseOutput, prompt)
    return sanitize_directional_case(result, context)


def run_bear_researcher(context: OptionsResearchContext, candidates: list[StrategyCandidate], counter_argument: str | None, llm) -> tuple[DirectionalCaseOutput, list[str]]:
    counter = f"\n\nThe Bull researcher's latest argument to address:\n{counter_argument}" if counter_argument else ""
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nYou are the BEAR researcher. Build the strongest EVIDENCE-SUPPORTED "
        f"bearish interpretation using ONLY these facts (never invent data):\n{build_context_facts_text(context)}\n\n"
        f"Candidates for context:\n{_candidates_text(candidates)}{counter}"
    )
    result = _invoke_structured(llm, DirectionalCaseOutput, prompt)
    return sanitize_directional_case(result, context)


def run_debate(
    context: OptionsResearchContext, candidates: list[StrategyCandidate], llm, *, rounds: int,
) -> tuple[DirectionalCaseOutput, DirectionalCaseOutput, list[str]]:
    """Section 23: bounded bull/bear debate. ``rounds`` is validated
    upstream (config cost guard) to a small positive integer -- this
    function itself has no unbounded loop regardless of the input."""
    rounds = max(1, min(int(rounds), 5))
    warnings: list[str] = []
    bull: DirectionalCaseOutput | None = None
    bear: DirectionalCaseOutput | None = None
    for _ in range(rounds):
        bull, w1 = run_bull_researcher(context, candidates, bear.thesis if bear else None, llm)
        bear, w2 = run_bear_researcher(context, candidates, bull.thesis if bull else None, llm)
        warnings.extend(w1)
        warnings.extend(w2)
    return bull, bear, warnings


def run_risk_agent(context: OptionsResearchContext, candidates: list[StrategyCandidate], llm) -> tuple[RiskAnalysisOutput, list[str]]:
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nFacts:\n{build_context_facts_text(context)}\n\n"
        f"Candidates (deterministic max_profit/max_loss/breakevens are already computed -- interpret them, "
        f"do not recompute):\n{_candidates_text(candidates)}\n\n"
        "For EACH candidate index, analyze: directional exposure, volatility exposure, theta exposure, "
        "gamma risk, gap risk, expiry risk, liquidity risk, assignment/exercise considerations, event risk, "
        "and tail risk. Where a candidate's max_loss is UNBOUNDED/UNDEFINED, explicitly say so as HIGH TAIL RISK."
    )
    result = _invoke_structured(llm, RiskAnalysisOutput, prompt)
    return sanitize_risk_analysis(result, len(candidates))


def run_coordinator(
    context: OptionsResearchContext, candidates: list[StrategyCandidate], regime: MarketRegimeOutput,
    volatility: VolatilityAnalysisOutput, oi: OIPositioningOutput, bull: DirectionalCaseOutput,
    bear: DirectionalCaseOutput, risk: RiskAnalysisOutput, llm,
) -> FinalReportOutput:
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\nYou are the Research Coordinator. Combine the following into one coherent "
        f"AI OPTIONS RESEARCH report (never call it a trade signal; never suggest executing anything):\n\n"
        f"Facts:\n{build_context_facts_text(context)}\n\n"
        f"Market Regime: {regime.model_dump()}\nVolatility: {volatility.model_dump()}\nOI Positioning: {oi.model_dump()}\n"
        f"Bull Case: {bull.model_dump()}\nBear Case: {bear.model_dump()}\nRisk Analysis: {risk.model_dump()}\n"
        f"Candidates:\n{_candidates_text(candidates)}\n\n"
        "Produce: market_overview, technical_context (note if none is available), news_sentiment (say "
        "'Not available in this phase' if none was supplied), options_market_structure_summary, "
        "invalidation_conditions (a list), and final_summary. This is AI OPTIONS RESEARCH for research "
        "purposes only -- never phrase anything as an instruction to place, modify, or cancel an order."
    )
    return _invoke_structured(llm, FinalReportOutput, prompt)


def get_llm(provider: str, model: str, *, temperature: float | None = None):
    return create_options_research_llm(provider, model, temperature=temperature)
