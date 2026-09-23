"""
AI Research Engine configuration -- follows the same pattern as
trading/core/config.py's Settings (frozen dataclass, env-var-backed,
fail-safe defaults) but is intentionally its own, separate settings
object: AI Research must never share a config object with, or be able to
mutate, the trading-safety settings in trading/core/config.py or the
per-algo trading/common/config.py::TradingConfig.

AI_RESEARCH_ENABLED defaults to False. When False, callers must not
initialize TradingAgents, make any LLM call, or make any TradingAgents
data-provider call -- see trading_agents_adapter.py, which checks this
flag before anything else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "trading" / "data" / "ai_research"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool_default_true(name: str) -> bool:
    """Like _env_bool, but fails OPEN (default True) for a per-feature
    flag that should behave exactly as it always has (fully enabled)
    when an operator has not opted into the finer-grained Phase 11
    control -- only an explicit false/0/no actually disables it."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in ("0", "false", "no")


@dataclass(frozen=True)
class AIResearchSettings:
    # Master switch. Fails safe: unset/misspelled -> disabled, never enabled.
    enabled: bool = field(default_factory=lambda: _env_bool("AI_RESEARCH_ENABLED"))

    # Phase 11 (Section 5): a SEPARATE global kill switch for AI/LLM
    # workloads specifically -- distinct from AI_RESEARCH_ENABLED (which
    # is the original feature flag) and from TRADING_MODE/is_live (the
    # trading-safety gate in trading/core/config.py, which this must
    # never touch or be touched by). Fails OPEN by design: an operator
    # who has already turned AI_RESEARCH_ENABLED on gets unchanged
    # behavior unless they explicitly set this to false during an
    # incident -- that is the intended "kill switch" action, not a
    # second on/off switch to remember to set for normal operation.
    ai_workloads_enabled: bool = field(default_factory=lambda: _env_bool_default_true("AI_WORKLOADS_ENABLED"))

    # Phase 11 (Section 4): independent per-capability controls, additive
    # to the existing AI_RESEARCH_ENABLED master switch -- both fail OPEN
    # (default True) so existing deployments that only ever set
    # AI_RESEARCH_ENABLED=true see IDENTICAL behavior to before Phase 11;
    # an operator only sets one of these to false to narrow what's on.
    ai_backtest_enabled: bool = field(default_factory=lambda: _env_bool_default_true("AI_BACKTEST_ENABLED"))
    ai_options_research_enabled: bool = field(default_factory=lambda: _env_bool_default_true("AI_OPTIONS_RESEARCH_ENABLED"))

    # Which TradingAgents LLM provider to use (openai | anthropic | google | ...).
    # No default credential is read or validated here -- TradingAgents itself
    # reads its own provider-specific *_API_KEY env var lazily, only when
    # run_research() actually executes.
    llm_provider: str = field(default_factory=lambda: _env("AI_RESEARCH_LLM_PROVIDER", "openai"))

    # Bounds concurrent research runs (each one is an LLM-heavy, multi-minute
    # operation). Enforced in Phase 2 via an asyncio.Semaphore in jobs.py.
    max_concurrent: int = field(default_factory=lambda: _env_int("AI_RESEARCH_MAX_CONCURRENT", 1))

    # Phase 1's smoke test observed a single successful agent-node round
    # trip in low tens of seconds, and TradingAgentsGraph.propagate() runs
    # several such round trips sequentially (4 analysts + bull/bear debate
    # + trader + risk debate, even at max_debate_rounds=1). 300s gives a
    # full "standard"-depth run realistic headroom without leaving a
    # genuinely stuck/hung job RUNNING indefinitely.
    timeout_seconds: int = field(default_factory=lambda: _env_int("AI_RESEARCH_TIMEOUT_SECONDS", 300))

    # Section 15/16: TradingAgents' own default writes cache/results/memory
    # under the OS user's home directory (~/.tradingagents). We never use
    # that default -- everything it would write is redirected under this
    # single, controlled, project-relative directory instead (created if
    # missing, never committed -- see .gitignore). This is a *containment*
    # boundary only: it does not grant TradingAgents any new capability, it
    # just keeps where it's allowed to write predictable and cleanable.
    data_dir: str = field(default_factory=lambda: _env("AI_RESEARCH_DATA_DIR", str(_DEFAULT_DATA_DIR)))

    # Phase 6 -- AI Research Backtesting cost/workload guards (Section 7).
    # Conservative defaults: a backtest is symbols x dates independent
    # TradingAgents runs, each itself several LLM calls -- these bound the
    # blast radius of an accidental huge request. Requests exceeding any
    # of these are REJECTED, never silently truncated.
    ai_backtest_max_symbols: int = field(default_factory=lambda: _env_int("AI_BACKTEST_MAX_SYMBOLS", 3))
    ai_backtest_max_dates: int = field(default_factory=lambda: _env_int("AI_BACKTEST_MAX_DATES", 12))
    ai_backtest_max_runs: int = field(default_factory=lambda: _env_int("AI_BACKTEST_MAX_RUNS", 20))
    ai_backtest_max_concurrent: int = field(default_factory=lambda: _env_int("AI_BACKTEST_MAX_CONCURRENT", 1))
    # Forward-return evaluation horizon (trading days) -- upstream's own
    # config["holding_period_days"], forwarded to settle_pending_decisions().
    ai_backtest_default_holding_days: int = field(default_factory=lambda: _env_int("AI_BACKTEST_HOLDING_DAYS", 5))

    # Phase 10 -- AI Options Research cost/workload guards (Section 56).
    # A multi-agent run (regime + volatility + OI + strategy research +
    # bull/bear debate rounds + risk + coordinator) is several LLM calls
    # per candidate; these bound the blast radius the same way the Phase 6
    # backtest guards do. Requests exceeding any of these are REJECTED.
    ai_options_max_debate_rounds: int = field(default_factory=lambda: _env_int("AI_OPTIONS_MAX_DEBATE_ROUNDS", 3))
    ai_options_max_candidates: int = field(default_factory=lambda: _env_int("AI_OPTIONS_MAX_CANDIDATES", 5))
    ai_options_max_concurrent: int = field(default_factory=lambda: _env_int("AI_OPTIONS_MAX_CONCURRENT", 1))
    ai_options_timeout_seconds: int = field(default_factory=lambda: _env_int("AI_OPTIONS_TIMEOUT_SECONDS", 420))


def load_ai_research_settings() -> AIResearchSettings:
    """Read a fresh AIResearchSettings snapshot from the current environment."""
    return AIResearchSettings()
