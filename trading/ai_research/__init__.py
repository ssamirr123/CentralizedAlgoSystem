"""
AI Research Engine -- an independent, read-only research module.

Phase 0/1 scope only: configuration + an isolated TradingAgents adapter.
No router, no background jobs, no UI yet (see docs referenced in the
Phase 0/1 reports).

STRUCTURAL ISOLATION (enforced by tests/ai_research/test_isolation.py):
nothing under trading/ai_research/ may import trading.common.broker,
trading.common.brokers.*, or trading.algos.* -- this package can never
place, modify, or cancel an order, start/stop a strategy, or change
TRADING_MODE, no matter what a future caller does with its output.
"""
