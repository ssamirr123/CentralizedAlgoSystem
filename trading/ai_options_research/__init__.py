"""
Phase 10 -- Multi-Agent AI Options Research Engine.

Architecture (Section 1's mandatory boundary):

    Market Data -> Options Intelligence (Phase 9) -> AI Options Research
        -> Candidate Structures -> Risk Analysis -> Research Report -> STOP

This package NEVER places, modifies, or cancels a broker order; never
starts/stops a strategy; never touches TRADING_MODE or account routing.
No object produced here is automatically convertible into an
``OrderIntent`` or any existing strategy configuration -- research output
is read-only, displayed and persisted only (same boundary as
``trading.ai_research``).

All deterministic option math (strategy templates, payoff, candidate
generation) lives in plain, LLM-free Python modules; only interpretation/
comparison/narrative is delegated to LLM agents, which never see or
influence a raw strike/price value directly -- they consume the frozen,
already-computed ``OptionsResearchContext`` and ``StrategyCandidate``
objects and their output is validated back against that same frozen
context (the "hallucination guard", Section 61) before being persisted or
displayed as authoritative.
"""
