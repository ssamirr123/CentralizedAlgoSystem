"""
Phase 16.12 -- the lightweight, deployable strategy WORKER application.

Everything under this package runs OUTSIDE the central TCC process (in
production: on its own EC2 instance/container). It contains ONLY what
Section 10 of the phase brief allows: worker identity, registration,
heartbeat, strategy runtime (the actual Strategy subclass), market-data
acquisition, OrderIntent creation, OrderIntent submission, and runtime/
status reporting -- all over the real HTTP transport in
trading/worker/client.py, calling the central trading/api/worker_routes.py
machine API.

STRUCTURALLY, a worker process:
  - never imports a broker adapter (trading.common.brokers.angelone/dhan/
    icici_breeze) or any BrokerClient implementation at all;
  - never imports trading.common.execution.StrategyExecutionEngine,
    trading.common.risk_manager.RiskManager, or
    trading.common.portfolio_risk.PortfolioRiskManager -- it has no
    central risk/execution authority of any kind;
  - never imports or references LiveAuthorization;
  - holds only a worker-auth secret (trading/common/worker_auth.py) and,
    optionally, READ-ONLY market-data credentials -- never a broker
    trading credential.
See tests/common/test_phase_16_12_worker_structural_safety.py for the
source-scan proof of every guarantee above.
"""
