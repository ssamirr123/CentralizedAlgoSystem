"""
Phase 7 -- Market Domain layer for the AI Research Engine.

Isolates Indian-market (and generic/US) domain knowledge -- exchanges,
trading calendars, canonical instruments, symbol normalization -- behind
one small package, instead of scattering "if market == INDIA" checks
through trading_agents_adapter.py, schemas.py, service.py, etc.
(Section 1's own architecture principle).

    AI Research Engine
           |
           +-- TradingAgents (trading_agents_adapter.py -- unchanged shape)
           |
           +-- Market Domain (this package)
                  |
                  +-- US / Generic (the existing, default behavior)
                  +-- INDIA
                        +-- NSE exchange
                        +-- NIFTY/BANKNIFTY/FINNIFTY + constituents
                        +-- Asia/Kolkata trading calendar
                        +-- symbol normalization + provider mapping

Nothing here imports trading.common.broker, trading.common.brokers.*, or
trading.algos.* (see tests/ai_research/test_isolation.py's AST scan,
which covers this subpackage too via rglob). This is pure domain data and
lookup logic -- no network I/O at import time, no LLM calls, no order/
strategy/account concept of any kind.
"""
