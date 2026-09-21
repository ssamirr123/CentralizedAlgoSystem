"""
Phase 16.12 -- worker process entrypoint: `python -m trading.worker.main`.

Selects ONE of the three real, already-integrated strategies (Phase 16.7/
16.8, trading rules UNCHANGED) via STRATEGY_ID, builds a WorkerRunner
against the configured TCC_URL, and runs forever. The SAME image/codebase
runs any of the three -- only STRATEGY_ID (and the strategy's own
instrument-configuration env vars) differ between deployments (Section
29: "the same worker image can run CombinedVWAP, DoubleStraddle,
VWAPHedge using different configuration").

No broker adapter, no broker credential, and no central risk/execution
object (RiskManager, PortfolioRiskManager, StrategyExecutionEngine) is
imported anywhere in this file or in trading.worker.* -- see that
package's own docstring and its structural test.
"""
from __future__ import annotations

import logging
import os
import sys

from trading.common.logger import configure_logging
from trading.common.market_data_gateway import FixedMarketDataSource
from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy
from trading.worker.client import TccClient
from trading.worker.config import WorkerConfig
from trading.worker.runner import WorkerRunner

configure_logging()
logger = logging.getLogger("trading.worker.main")

_STRATEGY_BUILDERS = {
    "CombinedVwapNifty": lambda: CombinedVwapNiftyStrategy(
        ce_instrument=os.environ.get("CE_INSTRUMENT", "NIFTY_CE"),
        pe_instrument=os.environ.get("PE_INSTRUMENT", "NIFTY_PE"),
        account_id=os.environ.get("ACCOUNT_ID", ""),
    ),
    "DoubleStraddelAlgo": lambda: DoubleStraddleStrategy(
        hedge_ce_instrument=os.environ.get("HEDGE_CE_INSTRUMENT", "NIFTY_HCE"),
        hedge_pe_instrument=os.environ.get("HEDGE_PE_INSTRUMENT", "NIFTY_HPE"),
        straddle_ce_instrument=os.environ.get("STRADDLE_CE_INSTRUMENT", "NIFTY_SCE"),
        straddle_pe_instrument=os.environ.get("STRADDLE_PE_INSTRUMENT", "NIFTY_SPE"),
        account_id=os.environ.get("ACCOUNT_ID", ""),
    ),
    "Vwap_Algo_Nifty_hedge": lambda: VwapAlgoNiftyHedgeStrategy(
        option_instrument=os.environ.get("OPTION_INSTRUMENT", "NIFTY_OPT"),
        hedge_instrument=os.environ.get("HEDGE_INSTRUMENT", "NIFTY_HEDGE"),
        account_id=os.environ.get("ACCOUNT_ID", ""),
    ),
}


def build_runner(config: WorkerConfig | None = None) -> WorkerRunner:
    config = config or WorkerConfig.from_env()
    if not config.worker_id or not config.auth_secret or not config.strategy_id:
        raise SystemExit("WORKER_ID, WORKER_AUTH_SECRET, and STRATEGY_ID are all required")
    if config.strategy_id not in _STRATEGY_BUILDERS:
        raise SystemExit(f"unknown STRATEGY_ID {config.strategy_id!r} -- must be one of {sorted(_STRATEGY_BUILDERS)}")

    strategy = _STRATEGY_BUILDERS[config.strategy_id]()
    client = TccClient(base_url=config.tcc_url, worker_id=config.worker_id, auth_secret=config.auth_secret, timeout=config.request_timeout_seconds)
    # A real deployment supplies a genuine read-only market-data source
    # (Section 13 -- ProviderMarketDataSource wrapping ICICIBreezeProvider
    # or similar); FixedMarketDataSource() is the safe, structurally-
    # non-networked default when none is configured, matching every prior
    # phase's "no invented external call" discipline.
    market_data_source = FixedMarketDataSource()
    return WorkerRunner(config=config, strategy=strategy, client=client, market_data_source=market_data_source)


def main() -> None:
    runner = build_runner()
    logger.info("starting worker %r for strategy %r against %r", runner._config.worker_id, runner._config.strategy_id, runner._config.tcc_url)
    runner.run_forever()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        raise
