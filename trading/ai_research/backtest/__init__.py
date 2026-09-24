"""
Phase 6 -- AI Research Backtesting.

Answers "what decisions would TradingAgents have produced across historical
research dates, and how would those decisions have scored?" -- NOT strategy
backtesting, NOT broker backtesting, NOT order simulation. See
trading_agents_adapter.py's EXECUTION ISOLATION docstring section: nothing
in this package (or its parent) imports trading.common.broker,
trading.common.brokers.*, or trading.algos.*, and nothing here can start,
stop, or route an order/strategy/account.

Architecture (never inverted):
    Historical Dates -> TradingAgents -> Historical AI Decisions
                      -> Evaluation -> Metrics/Report

Reuses, rather than duplicates:
    * trading_agents_adapter.run_research() -- the SAME function backing a
      normal single research job -- for every individual (symbol, date) cell.
    * trading_agents_adapter.settle_pending_decisions() -- upstream's own
      TradingAgentsGraph.settle_pending(), for real yfinance-vs-benchmark
      forward-return evaluation (never reimplemented here).
    * trading.ai_research.repository -- each backtest cell is ALSO a normal
      persisted AIResearchRun row (Section 18: traceability/drill-down),
      not a second copy of the report.
"""
