"""
The ONLY module in this codebase allowed to import the `tradingagents`
package (https://github.com/TauricResearch/TradingAgents, pinned v0.5.0).
Installed from GitHub, never from PyPI -- see requirements-ai-research.txt
for why (the PyPI package of that exact name is an unrelated project).

Import discipline: `import tradingagents` happens LAZILY, inside
run_research(), never at module load time.

Every name below is grounded in the actual upstream v0.5.0 source
(confirmed via direct inspection, not guessed):
  * SUPPORTED_ANALYSTS / real node names -- tradingagents/graph/analyst_execution.py,
    tradingagents/graph/setup.py
  * TradingAgentsGraph(selected_analysts=, debug=, config=, callbacks=),
    .graph (the compiled StateGraph), .create_run_state(), .checkpoint_scope(),
    .checkpoint_input(), .record_decision(), .clear_checkpoint_on_success(),
    .process_signal(), .propagator.get_graph_args() -- all real, public
    methods on TradingAgentsGraph (tradingagents/graph/trading_graph.py)
  * PortfolioContext / Position -- tradingagents/portfolio.py
  * the 5-tier rating scale + "REVIEW" -- tradingagents/agents/utils/rating.py
  * AgentState field names -- tradingagents/agents/utils/agent_states.py
  * TradingMemoryLog.load_entries() -- tradingagents/agents/utils/memory.py
  * data_vendors / llm_max_retries -- tradingagents/default_config.py,
    tradingagents/graph/trading_graph.py's _coerce_max_retries

PHASE 3 STAGE-PROGRESS FIX -- root cause and new mechanism
------------------------------------------------------------
Phase 2 used a generic `BaseCallbackHandler.on_chain_start` LangChain
callback, hoping it would fire once per LangGraph node. Phase 2's own real
end-to-end run proved it never fired at all (stages_seen: []), even
though three analyst nodes genuinely executed. Root cause: LangChain's
generic chain-callback instrumentation does not reliably wrap LangGraph's
own node-dispatch machinery in this version -- it's simply the wrong hook
for this purpose.

The correct, OFFICIAL LangGraph mechanism is the compiled graph's own
`.stream(..., stream_mode=[...])` API (this is exactly what TradingAgents'
own `debug=True` path already uses internally, via
`self.graph.stream(graph_input, **self.propagator.get_graph_args())` with
`stream_mode="values"` -- see trading_graph.py's `_run_graph`). That mode
alone yields only the cumulative state after each step, with no node
name. Passing `stream_mode=["updates", "values"]` instead (confirmed
working against the pinned langgraph==1.2.12 with a trivial, zero-cost
graph before touching any real LLM) yields BOTH:
    ("updates", {"<real node name>": <that node's state delta>})
    ("values",  <cumulative state after that step>)
in one interleaved stream. "updates" gives real node names; the LAST
"values" tuple is the authoritative final state, identical to what
`.invoke()`/`.propagate()` would have returned.

IMPORTANT NUANCE (documented, not glossed over): LangGraph's "updates"
mode reports a node's COMPLETION (its output), not a separate "started"
event -- LangGraph has no built-in "node started" stream mode. So what
this module calls "stage progress" is built from real completion events:
each ("updates", {name: ...}) tuple means `name` just finished. The
"currently RUNNING" node is INFERRED as the next not-yet-completed node
in the deterministic execution plan after the most recently completed
one (jobs.py owns this inference, from the real completion events this
module reports) -- it is not a directly observed "start" event.

This module bypasses ONLY the single `graph.invoke(...)`/
`graph.stream(stream_mode="values")` line inside upstream's own
`TradingAgentsGraph._run_graph()`. Every other side effect
`_run_graph`/`propagate()` performs is preserved by calling the SAME
real, public instance methods upstream itself calls, in the same order
(see `_run_graph_with_progress` below) -- not reimplemented, not guessed:
    create_run_state()             initial state (settles pending memory
                                    entries, injects past_context/instrument
                                    identity, same as upstream)
    checkpoint_scope()/checkpoint_input()   identical checkpoint handling
    record_decision()              writes the decision-memory entry
    clear_checkpoint_on_success()  identical checkpoint cleanup
    process_signal()               identical 5-tier signal extraction
`_log_state` (a private upstream method) is also called for full parity
with `_run_graph`'s own behavior, since it is side-effect-free
(in-memory bookkeeping only).

EXECUTION ISOLATION (structurally enforced -- see
tests/ai_research/test_isolation.py):
  * this module MUST NOT import trading.common.broker, any
    trading.common.brokers.* adapter, or any trading.algos.* strategy
    script;
  * it MUST NOT accept a broker client, order service, strategy-control
    service, or account-routing service as a parameter;
  * it MUST NOT read or write TRADING_MODE / trading.common.config /
    trading.core.config;
  * run_research() returns a ResearchResult (a report + a research
    signal/rating), never anything resembling an OrderResult, and never
    calls a broker method or places/modifies/cancels anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trading.ai_research.config import load_ai_research_settings

# --------------------------------------------------------------------------- #
# Real upstream analyst keys and node names (tradingagents/graph/
# analyst_execution.py, tradingagents/graph/setup.py) -- nothing here is
# invented; if upstream renames a node, only this block needs updating.
# --------------------------------------------------------------------------- #
SUPPORTED_ANALYSTS: tuple[str, ...] = ("market", "social", "news", "fundamentals")
DEFAULT_ANALYSTS: tuple[str, ...] = SUPPORTED_ANALYSTS

# analyst key -> its real graph node name (ANALYST_NODE_SPECS in
# analyst_execution.py). "social" is upstream's own wire key for what the
# CLI/docs call the Sentiment Analyst.
ANALYST_NODE_NAMES: dict[str, str] = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}

# Real, fixed downstream node names (always present regardless of analyst
# selection) -- tradingagents/graph/setup.py's workflow.add_node calls.
# "Research Manager" is the Bull/Bear debate's judge/synthesizer -- there
# is no separate real "debate" node upstream; the three named debate/risk
# participants ARE the debate, so this module never invents a pseudo-node
# for it (Phase 3 Section 5: "Do not invent nodes").
RESEARCH_DEBATE_NODES: tuple[str, ...] = ("Bull Researcher", "Bear Researcher", "Research Manager")
TRADER_NODES: tuple[str, ...] = ("Trader",)
RISK_NODES: tuple[str, ...] = ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst")
PORTFOLIO_NODES: tuple[str, ...] = ("Portfolio Manager",)

_ANALYST_NODE_NAMES = set(ANALYST_NODE_NAMES.values())
_DEBATE_NODE_NAMES = set(RESEARCH_DEBATE_NODES)
_TRADER_NODE_NAMES = set(TRADER_NODES)
_RISK_NODE_NAMES = set(RISK_NODES)
_PORTFOLIO_NODE_NAMES = set(PORTFOLIO_NODES)


def real_node_order(selected_analysts: tuple[str, ...]) -> tuple[str, ...]:
    """The real, deterministic node-completion order for a given analyst
    selection -- analysts run in SUPPORTED_ANALYSTS order (matching
    build_analyst_execution_plan's own iteration order), then the fixed
    debate/trader/risk/portfolio nodes. Debate/risk nodes can each
    complete MORE than once (once per debate_rounds/risk_debate_rounds
    round) -- this is the first-occurrence order only, used to seed the
    initial PENDING checklist; jobs.py handles repeats."""
    order = [ANALYST_NODE_NAMES[a] for a in SUPPORTED_ANALYSTS if a in selected_analysts]
    order.extend(RESEARCH_DEBATE_NODES)
    order.extend(TRADER_NODES)
    order.extend(RISK_NODES)
    order.extend(PORTFOLIO_NODES)
    return tuple(order)


def stage_bucket_for_node(name: str) -> str | None:
    """Coarse bucket for a real node name -- used only for the simple
    /status summary field; /stages exposes the real per-node list."""
    if name in _ANALYST_NODE_NAMES:
        return "ANALYSTS"
    if name in _DEBATE_NODE_NAMES:
        return "RESEARCH_DEBATE"
    if name in _TRADER_NODE_NAMES:
        return "TRADER"
    if name in _RISK_NODE_NAMES:
        return "RISK"
    if name in _PORTFOLIO_NODE_NAMES:
        return "PORTFOLIO_MANAGER"
    return None


# Real upstream data-vendor options (tradingagents/default_config.py's
# data_vendors block) -- only vendors the pinned version actually supports.
SUPPORTED_DATA_VENDOR_CATEGORIES: dict[str, tuple[str, ...]] = {
    "core_stock_apis": ("yfinance", "alpha_vantage"),
    "technical_indicators": ("yfinance", "alpha_vantage"),
    "fundamental_data": ("yfinance", "alpha_vantage"),
    "news_data": ("yfinance", "alpha_vantage"),
    "macro_data": ("fred",),
    "prediction_markets": ("polymarket",),
}

# Real bounds for llm_max_retries (tradingagents/graph/trading_graph.py's
# _coerce_max_retries only rejects negative/non-integer values; upstream's
# own SDK default is "usually 2" per that function's docstring). We cap
# the upper bound ourselves (Section 12: "do not allow arbitrary
# unbounded retries") -- this ceiling is OUR safety choice, not an
# upstream one.
RETRY_COUNT_MIN = 0
RETRY_COUNT_MAX = 10
RETRY_COUNT_DEFAULT = None  # None -> upstream's own per-provider default


class AIResearchDisabledError(RuntimeError):
    """Raised when research is attempted while AI_RESEARCH_ENABLED=false
    (the default). Callers must treat this as "feature off", not an error
    to retry."""


class AIResearchConfigError(RuntimeError):
    """Raised for any failure to actually run research: the tradingagents
    package isn't installed, an unsupported analyst/config value was
    requested, required LLM credentials are missing, or TradingAgents
    itself raised during the run."""

    def __init__(self, message: str, *, provider_error: str | None = None) -> None:
        super().__init__(message)
        # Section 16: classify a real external-provider failure (e.g. a
        # rate limit) distinctly from an application defect, WITHOUT
        # parsing/trusting arbitrary provider text beyond a few known,
        # safe markers. None means "not classified as a provider error".
        self.provider_error = provider_error


def _classify_provider_error(exc: Exception) -> str | None:
    """Best-effort classification from the exception's own type/message,
    for the FAILED job's provider_error field (Section 16) -- never
    trusts or echoes back more than a short category label. Extend this
    as new real failure modes are observed; unrecognized errors stay
    unclassified (None) rather than guessed."""
    text = str(exc).lower()
    type_name = type(exc).__name__
    if "rate_limit" in text or "429" in text or "too many requests" in text:
        return "RATE_LIMIT"
    if "413" in text or "request too large" in text or "tokens per minute" in text:
        return "RATE_LIMIT"
    if "401" in text or "invalid api key" in text or "authentication" in text or "unauthorized" in text:
        return "AUTH_ERROR"
    if "404" in text and "model" in text:
        return "MODEL_NOT_FOUND"
    if "timeout" in text or "timed out" in type_name.lower():
        return "PROVIDER_TIMEOUT"
    return None


@dataclass(frozen=True)
class PortfolioPositionInput:
    ticker: str
    quantity: float
    average_price: float | None = None


@dataclass(frozen=True)
class PortfolioInput:
    """Mirrors tradingagents.portfolio.PortfolioContext/Position exactly --
    optional, explicit research context only. Never sourced from a real
    broker position."""

    cash: float | None = None
    currency: str | None = None
    positions: tuple[PortfolioPositionInput, ...] = ()


@dataclass(frozen=True)
class ResearchReport:
    """Normalized from the real AgentState field names (see module
    docstring). A field is None, never fabricated, when TradingAgents
    itself didn't populate it for this run."""

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
    signal: str | None = None


@dataclass(frozen=True)
class ResearchResult:
    instrument: str
    as_of_date: str
    report: ResearchReport
    raw_state: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryEntry:
    """One tradingagents decision-memory entry, from TradingMemoryLog's
    own load_entries(). memory_id is OUR OWN synthesized, purely logical
    key (trade_date:ticker) -- never a filesystem path, so there is
    nothing here for a path-traversal attempt to reach."""

    memory_id: str
    ticker: str
    trade_date: str
    rating: str | None
    pending: bool
    decision: str | None
    reflection: str | None


def _memory_log_path(data_dir: str) -> Path:
    return Path(data_dir) / "memory" / "trading_memory.md"


def list_memory_entries(*, limit: int = 50) -> list[MemoryEntry]:
    """Read-only: the decision-memory log this adapter itself writes to
    (always under our own controlled AIResearchSettings.data_dir, never
    upstream's ~/.tradingagents default). Returns [] if nothing has been
    written yet -- never raises for a missing file."""
    settings = load_ai_research_settings()
    path = _memory_log_path(settings.data_dir)
    if not path.exists():
        return []
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
    except ImportError as exc:
        raise AIResearchConfigError(f"tradingagents package is not installed or importable: {exc}") from exc

    log = TradingMemoryLog({"memory_log_path": str(path)})
    entries = log.load_entries()
    out = []
    for e in entries[-limit:]:
        ticker = e.get("ticker", "")
        trade_date = e.get("trade_date", "")
        out.append(MemoryEntry(
            memory_id=f"{trade_date}:{ticker}", ticker=ticker, trade_date=trade_date,
            rating=e.get("rating"), pending=bool(e.get("pending")),
            decision=e.get("decision"), reflection=e.get("reflection"),
        ))
    return out


def get_memory_entry(memory_id: str) -> MemoryEntry | None:
    """Looks up ONE entry by its logical memory_id (trade_date:ticker) --
    a plain dict lookup against already-parsed entries, never a
    filesystem path built from client input."""
    for entry in list_memory_entries(limit=10_000):
        if entry.memory_id == memory_id:
            return entry
    return None


# --------------------------------------------------------------------------- #
# Phase 6 -- AI Research Backtesting. Reuses the SAME real upstream
# machinery as run_research(): TradingMemoryLog, TradingAgentsGraph's own
# settle_pending()/_resolve_pending_entries(), and tradingagents.backtest's
# own iter_grid(). Nothing here reimplements decision scoring or forward
# returns -- upstream's _fetch_returns() (yfinance close-vs-benchmark,
# point-in-time-safe: it fetches the window AFTER trade_date, so no future
# information reaches the DECISION, only the already-made decision's own
# outcome) is the sole source of raw_return/alpha_return.
# --------------------------------------------------------------------------- #

# Our own frequency vocabulary -> upstream iter_grid's every_n_days
# (calendar days, matching iter_grid's own simple day-stepping -- not
# trading-day precise; documented, not silently rounded).
BACKTEST_FREQUENCY_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30}


def backtest_date_grid(start_date: str, end_date: str, frequency: str, market: str = "US") -> tuple[str, ...]:
    """Date-grid generation matching tradingagents.backtest.iter_grid's own
    algorithm EXACTLY (pinned v0.5.0 source, read directly -- not guessed):
    start at start_date, step by every_n_days, never past today (a future
    date has no outcome to settle, and the graph itself rejects it).

    Deliberately reimplemented here (not imported from `tradingagents`)
    rather than lazily importing the real package the way run_research()/
    settle_pending_decisions() do: computing a date grid is pure, trivial
    arithmetic with no LLM/network/side effect, so making it depend on the
    (large, optional) tradingagents install being present would be a
    needless hard dependency for something this module can trivially
    reproduce byte-for-byte -- the exact same posture evaluator.py takes
    for upstream's tiny `_DIRECTION` mapping.

    Phase 7 Section 20: for market="INDIA", the stepped calendar-day grid
    is additionally filtered to real NSE trading days (skips weekends AND
    holidays via the verified market.calendar_nse module) -- a stepped
    date landing on a holiday is SKIPPED, never shifted to a neighboring
    date (Section 19's "do not silently change the requested date"
    philosophy applied to grid generation)."""
    from datetime import datetime, timedelta, timezone

    every_n_days = BACKTEST_FREQUENCY_DAYS.get(frequency)
    if every_n_days is None:
        raise AIResearchConfigError(f"unsupported backtest frequency {frequency!r}; supported: {tuple(BACKTEST_FREQUENCY_DAYS)}")

    def _canonical(value: str) -> "datetime":
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise AIResearchConfigError(f"grid dates must be in YYYY-MM-DD format, got {value!r}") from exc
        if parsed.strftime("%Y-%m-%d") != str(value):
            raise AIResearchConfigError(f"grid dates must be in YYYY-MM-DD format, got {value!r}")
        return parsed

    start, end = _canonical(start_date), _canonical(end_date)
    if end < start:
        raise AIResearchConfigError(f"the grid ends before it starts: {end_date} is before {start_date}")

    last = min(end, datetime.strptime(datetime.now(timezone.utc).strftime("%Y-%m-%d"), "%Y-%m-%d"))
    dates: list[str] = []
    cursor = start
    while cursor <= last:
        dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=every_n_days)

    if market == "INDIA":
        from trading.ai_research.market.calendar_nse import CalendarCoverageError, is_trading_day

        try:
            dates = [d for d in dates if is_trading_day(datetime.strptime(d, "%Y-%m-%d").date())]
        except CalendarCoverageError as exc:
            raise AIResearchConfigError(str(exc)) from exc

    return tuple(dates)


def settle_pending_decisions(
    ticker: str,
    *,
    llm_provider: str | None = None,
    quick_think_llm: str | None = None,
    data_vendors: dict[str, str] | None = None,
    holding_days: int | None = None,
) -> None:
    """Flush this ticker's still-pending decision(s) whose holding window
    has now traded, via the REAL upstream `TradingAgentsGraph.settle_pending()`
    (trading_graph.py) -- the same method upstream's own `run_backtest()`
    calls once per ticker after its sweep, so the LAST cell for a ticker
    doesn't stay pending forever (only the NEXT run for that ticker would
    otherwise trigger settlement, via create_run_state()'s own
    _resolve_pending_entries() call). No new evaluation math: this calls
    upstream's actual yfinance-vs-benchmark computation
    (_fetch_returns/_resolve_pending_entries), never reimplemented here.

    Settling costs one quick-LLM reflection call per pending entry
    (upstream's Reflector) -- a real, if modest, provider cost, and a
    provider failure here must not abort the caller's sweep (mirrors
    upstream's own try/except around settle_pending in run_backtest:
    "reflection calls an LLM; one failure is not the sweep's").
    """
    settings = load_ai_research_settings()
    if not settings.enabled:
        raise AIResearchDisabledError("AI_RESEARCH_ENABLED is false -- research will not run.")

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as exc:
        raise AIResearchConfigError(f"tradingagents package is not installed or importable: {exc}") from exc

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = llm_provider or settings.llm_provider
    if quick_think_llm:
        config["quick_think_llm"] = quick_think_llm
    if data_vendors:
        config["data_vendors"] = {**config.get("data_vendors", {}), **data_vendors}
    if holding_days is not None:
        config["holding_period_days"] = holding_days

    data_dir = Path(settings.data_dir)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    config["data_cache_dir"] = str(data_dir / "cache")
    config["results_dir"] = str(data_dir / "logs")
    config["memory_log_path"] = str(_memory_log_path(settings.data_dir))

    try:
        # Analyst selection is irrelevant to settlement (it only touches
        # memory_log/reflector/yfinance internals) -- minimal graph shape.
        graph = TradingAgentsGraph(selected_analysts=("market",), debug=False, config=config)
        graph.settle_pending(ticker)
    except Exception as exc:  # noqa: BLE001 -- provider/network failure must not abort the caller's sweep
        raise AIResearchConfigError(
            f"settling pending decisions for {ticker!r} failed: {exc}", provider_error=_classify_provider_error(exc),
        ) from exc


# --------------------------------------------------------------------------- #
# Section 10/28 -- Point-in-time safety matrix. Grounded in direct reading
# of the pinned v0.5.0 source (tradingagents/dataflows/date_window.py,
# y_finance.py, yfinance_news.py, fred.py, polymarket.py,
# alpha_vantage_fundamentals.py, reddit.py, stocktwits.py) -- not
# assumed. Every value here cites the actual upstream mechanism enforcing
# (or not enforcing) the cutoff.
# --------------------------------------------------------------------------- #
POINT_IN_TIME_SAFETY: dict[str, dict] = {
    "market_data": {
        "safe": True,
        "detail": "OHLCV/technical indicators are fetched with an explicit "
                  "[start,end] window bounded by the analysis date "
                  "(y_finance.py get_YFin_data_online / get_stock_stats_indicators_window); "
                  "Alpha Vantage's get_stock() is equivalently filtered "
                  "client-side to the same window.",
    },
    "news": {
        "safe": True,
        "detail": "Per-ticker and global news (Yahoo) are filtered through "
                  "date_window.in_window() against each article's published "
                  "timestamp, a half-open window that excludes anything "
                  "published after the analysis date (#992/#1007/#1126).",
    },
    "sentiment": {
        "safe": True,
        "detail": "Reddit and StockTwits (the 'social'/sentiment sources) use "
                  "the same date_window.in_window() filter as news, keyed on "
                  "each post's own timestamp (#1220).",
    },
    "fundamentals_company_profile": {
        "safe": "WITHHELD",
        "detail": "yfinance Ticker.info / Alpha Vantage OVERVIEW carry no "
                  "historical vintage (market cap, name, sector all reflect "
                  "TODAY). date_window.withhold_live_profile() refuses to "
                  "serve this for any historical analysis date rather than "
                  "risk leaking it (#1300) -- safe by refusal, not by cutoff.",
    },
    "fundamentals_financial_statements": {
        "safe": "PARTIAL",
        "detail": "Balance sheet/income/cashflow rows are filtered to "
                  "periods ending on or before the analysis date "
                  "(filter_financials_by_date), but neither vendor reports a "
                  "FILING date -- a statement can be served on the day its "
                  "period ends even though the company had not yet actually "
                  "published it (a filing lag of days to weeks). Upstream "
                  "documents this explicitly in the vendor's own output "
                  "header rather than claiming a stricter guarantee.",
    },
    "insider_transactions": {
        "safe": "PARTIAL",
        "detail": "Filtered to transactions on or before the analysis date, "
                  "but dated by TRANSACTION date, not FILING date -- a Form "
                  "4 is filed up to two business days later, so the newest "
                  "rows served on a given date may not yet have been public "
                  "on that date.",
    },
    "macro_data": {
        "safe": True,
        "detail": "FRED is queried with its own realtime_start/vintage "
                  "parameters pinned to the analysis date (fred.py "
                  "get_macro_data, #1275) -- a historical run sees the value "
                  "as it stood on that date, not a later revision (material "
                  "for revision-prone series like CPI/GDP).",
    },
    "prediction_markets": {
        "safe": "WITHHELD",
        "detail": "Polymarket has no historical-odds API -- it only serves "
                  "LIVE odds on currently-open markets. get_prediction_markets() "
                  "withholds entirely for any analysis date before today "
                  "rather than serve today's odds as if they were historical.",
    },
    "decision_memory_past_context": {
        "safe": True,
        "detail": "Past-decision lessons injected into the prompt "
                  "(TradingMemoryLog.get_past_context) are filtered by "
                  "as_of=trade_date for any historical run -- only lessons "
                  "already RESOLVED by that date are shown (#1251).",
    },
}


def _run_graph_with_progress(graph, instrument: str, as_of_date: str, asset_type: str,
                              portfolio_arg, on_node_complete: Callable[[str], None] | None,
                              market_data_context: str | None = None):
    """Equivalent to TradingAgentsGraph._run_graph(), reusing every one of
    its real side-effecting method calls (see module docstring), with
    ONLY the actual execution line replaced by
    `graph.graph.stream(..., stream_mode=["updates", "values"])` so real
    node-completion events are observable. Returns (final_state, signal)
    -- identical shape to .propagate().

    Phase 8 (Section 23): `market_data_context`, when given, is appended
    to the initial state's real `instrument_context` field -- upstream's
    OWN AgentState key (verified against propagation.py's
    `Propagator.create_initial_state`: "the deterministic ticker-identity
    string... agents fall back to ticker-only context via
    get_instrument_context_from_state" when empty, i.e. downstream nodes
    genuinely read this field). This is the smallest reliable
    integration point that needs no monkey-patching and no rewriting of
    TradingAgents' own tools: we are only post-processing a plain dict
    `create_run_state()` (a real, public method) already handed back to
    us, before it is ever streamed through the graph. News/sentiment/
    fundamentals tool calls are completely untouched (Section 24)."""
    init_state = graph.create_run_state(instrument, as_of_date, asset_type, portfolio_arg)
    if market_data_context and "instrument_context" in init_state:
        existing = init_state["instrument_context"] or ""
        init_state["instrument_context"] = f"{existing}\n\n{market_data_context}".strip()

    with graph.checkpoint_scope(instrument, as_of_date, asset_type, portfolio_arg) as thread_id_value:
        args = graph.propagator.get_graph_args()
        if thread_id_value is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = thread_id_value
        # Override upstream's own default ("values" only) -- this is the
        # one substitution this function makes.
        args["stream_mode"] = ["updates", "values"]

        graph_input = graph.checkpoint_input(init_state)

        final_state: dict = {}
        for mode, chunk in graph.graph.stream(graph_input, **args):
            if mode == "updates":
                for node_name in chunk:
                    if on_node_complete is not None:
                        on_node_complete(node_name)
            elif mode == "values":
                final_state = chunk

    graph.curr_state = final_state
    graph._log_state(as_of_date, final_state)  # noqa: SLF001 -- real upstream method, in-memory only
    graph.record_decision(instrument, as_of_date, final_state)
    graph.clear_checkpoint_on_success(instrument, as_of_date, asset_type, portfolio_arg)
    signal = graph.process_signal(final_state.get("final_trade_decision"))
    return final_state, signal


def run_research(
    instrument: str,
    as_of_date: str,
    *,
    selected_analysts: tuple[str, ...] | None = None,
    llm_provider: str | None = None,
    deep_think_llm: str | None = None,
    quick_think_llm: str | None = None,
    temperature: float | None = None,
    debate_rounds: int | None = None,
    risk_debate_rounds: int | None = None,
    retry_count: int | None = None,
    data_vendors: dict[str, str] | None = None,
    checkpoint_enabled: bool = False,
    portfolio: PortfolioInput | None = None,
    on_node_complete: Callable[[str], None] | None = None,
    market_data_context: str | None = None,
) -> ResearchResult:
    """Run one TradingAgents research pass and return the normalized
    report. Read-only: never places, modifies, or cancels a broker order;
    never starts/stops a strategy; never changes TRADING_MODE. Accepts no
    broker/account/strategy parameter of any kind.

    `on_node_complete`, when given, is called synchronously (from
    whatever thread this function runs on) with the REAL graph node name
    each time LangGraph reports it as complete (see module docstring for
    why this is a completion event, not a "started" event, and why this
    is now a directly-observed LangGraph mechanism, not a best-effort
    LangChain callback guess).

    `market_data_context` (Phase 8, Section 23) -- an optional pre-built
    text block (from HistoricalMarketDataService, our own
    Breeze/TimescaleDB-backed data for market=INDIA) appended to the
    graph's real `instrument_context` field. TradingAgents itself is
    never handed Breeze credentials and never calls Breeze directly --
    this function only ever receives an already-fetched, already-
    normalized string.
    """
    settings = load_ai_research_settings()
    if not settings.enabled:
        raise AIResearchDisabledError(
            "AI_RESEARCH_ENABLED is false -- research will not run. "
            "This is the default; set AI_RESEARCH_ENABLED=true to opt in."
        )

    analysts = tuple(selected_analysts) if selected_analysts else DEFAULT_ANALYSTS
    unknown = set(analysts) - set(SUPPORTED_ANALYSTS)
    if unknown:
        raise AIResearchConfigError(
            f"unsupported analyst(s) {sorted(unknown)}; supported: {SUPPORTED_ANALYSTS}"
        )
    if not analysts:
        raise AIResearchConfigError("selected_analysts must not be empty")

    if retry_count is not None and not (RETRY_COUNT_MIN <= retry_count <= RETRY_COUNT_MAX):
        raise AIResearchConfigError(
            f"retry_count must be between {RETRY_COUNT_MIN} and {RETRY_COUNT_MAX}, got {retry_count}"
        )

    if data_vendors:
        for category, vendor in data_vendors.items():
            allowed = SUPPORTED_DATA_VENDOR_CATEGORIES.get(category)
            if allowed is None:
                raise AIResearchConfigError(f"unsupported data vendor category {category!r}")
            if vendor not in allowed:
                raise AIResearchConfigError(
                    f"unsupported vendor {vendor!r} for category {category!r}; supported: {allowed}"
                )

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.portfolio import PortfolioContext as TAPortfolioContext
        from tradingagents.portfolio import Position as TAPosition
    except ImportError as exc:
        raise AIResearchConfigError(
            f"tradingagents package is not installed or importable: {exc}"
        ) from exc

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = llm_provider or settings.llm_provider
    if deep_think_llm:
        config["deep_think_llm"] = deep_think_llm
    if quick_think_llm:
        config["quick_think_llm"] = quick_think_llm
    if temperature is not None:
        config["temperature"] = temperature
    if debate_rounds is not None:
        config["max_debate_rounds"] = debate_rounds
    if risk_debate_rounds is not None:
        config["max_risk_discuss_rounds"] = risk_debate_rounds
    if retry_count is not None:
        config["llm_max_retries"] = retry_count
    if data_vendors:
        config["data_vendors"] = {**config.get("data_vendors", {}), **data_vendors}
    config["checkpoint_enabled"] = bool(checkpoint_enabled)

    # Never TradingAgents' own ~/.tradingagents default -- everything it
    # writes is contained under our own controlled, git-ignored directory.
    data_dir = Path(settings.data_dir)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    config["data_cache_dir"] = str(data_dir / "cache")
    config["results_dir"] = str(data_dir / "logs")
    config["memory_log_path"] = str(_memory_log_path(settings.data_dir))

    portfolio_arg = None
    if portfolio is not None:
        portfolio_arg = TAPortfolioContext(
            cash=portfolio.cash,
            currency=portfolio.currency,
            positions=[
                TAPosition(ticker=p.ticker, quantity=p.quantity, average_price=p.average_price)
                for p in portfolio.positions
            ],
        )

    try:
        graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config)
        state, signal = _run_graph_with_progress(
            graph, instrument, as_of_date, "stock", portfolio_arg, on_node_complete,
            market_data_context=market_data_context,
        )
    except Exception as exc:  # noqa: BLE001 -- any TradingAgents/LLM failure
        # must surface as a typed, catchable error, never crash the caller
        # or leak raw provider exceptions uncontrolled.
        raise AIResearchConfigError(
            f"TradingAgents research failed: {exc}", provider_error=_classify_provider_error(exc),
        ) from exc

    state = state if isinstance(state, dict) else {}
    invest_debate = state.get("investment_debate_state") or {}
    risk_debate = state.get("risk_debate_state") or {}

    report = ResearchReport(
        market_analysis=state.get("market_report") or None,
        news_analysis=state.get("news_report") or None,
        sentiment_analysis=state.get("sentiment_report") or None,
        fundamentals_analysis=state.get("fundamentals_report") or None,
        bull_case=invest_debate.get("bull_history") or None,
        bear_case=invest_debate.get("bear_history") or None,
        research_manager_decision=state.get("investment_plan") or invest_debate.get("judge_decision") or None,
        trader_plan=state.get("trader_investment_plan") or None,
        risk_aggressive=risk_debate.get("aggressive_history") or None,
        risk_conservative=risk_debate.get("conservative_history") or None,
        risk_neutral=risk_debate.get("neutral_history") or None,
        risk_judge_decision=risk_debate.get("judge_decision") or None,
        final_trade_decision=state.get("final_trade_decision") or None,
        signal=str(signal) if signal is not None else None,
    )

    return ResearchResult(instrument=instrument, as_of_date=as_of_date, report=report, raw_state=state)


def create_options_research_llm(provider: str, model: str, *, temperature: float | None = None) -> Any:
    """Phase 10 -- the ONE sanctioned way any other module in this
    codebase obtains an LLM client, keeping the `tradingagents` import
    confined to this file (see module docstring). Returns a plain
    LangChain ``BaseChatModel`` (call ``.invoke(...)`` or
    ``.with_structured_output(SomeModel)`` on it) built via
    ``tradingagents.llm_clients.create_llm_client`` -- the same public
    factory ``TradingAgentsGraph`` itself uses internally
    (tradingagents/graph/trading_graph.py). This function never touches
    a broker credential and never returns anything beyond a chat-model
    object; it has no knowledge of options/strikes/orders."""
    try:
        from tradingagents.llm_clients import create_llm_client
    except ImportError as exc:
        raise AIResearchConfigError(f"tradingagents package is not installed or importable: {exc}") from exc

    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        client = create_llm_client(provider=provider, model=model, **kwargs)
        return client.get_llm()
    except Exception as exc:  # noqa: BLE001 -- normalize provider/config failures the same way run_research does
        provider_error = _classify_provider_error(exc)
        raise AIResearchConfigError(f"failed to create LLM client ({type(exc).__name__}): {exc}", provider_error=provider_error) from exc
