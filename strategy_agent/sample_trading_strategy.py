from __future__ import annotations

import random
import time
import sys
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from strategy_agent.agent import StrategyHeartbeatAgent


class SampleMeanReversionStrategy:
    """Example strategy showing how to embed the heartbeat agent."""

    def __init__(self) -> None:
        self.name = "sample_mean_reversion"
        self.server = "ec2-ap-south-1a-i-01"
        self.mtm = 0.0
        self.day_pnl = 0.0
        self.trade_count = 0
        self.status = "RUNNING"

        self.heartbeat_agent = StrategyHeartbeatAgent(
            strategy_name=self.name,
            server_name=self.server,
            api_base_url="http://127.0.0.1:8000",
            heartbeat_interval_seconds=30,
            request_timeout_seconds=5,
            max_retries=3,
        )

    def run(self) -> None:
        self.heartbeat_agent.start()
        try:
            while True:
                # Replace this block with real strategy logic.
                self._simulate_trade_step()
                self.heartbeat_agent.update_metrics(
                    mtm=self.mtm,
                    pnl=self.day_pnl,
                    trade_count=self.trade_count,
                    status=self.status,
                )
                time.sleep(2)
        except KeyboardInterrupt:
            self.status = "STOPPED"
            self.heartbeat_agent.update_metrics(
                mtm=self.mtm,
                pnl=self.day_pnl,
                trade_count=self.trade_count,
                status=self.status,
            )
            self.heartbeat_agent.stop()

    def _simulate_trade_step(self) -> None:
        pnl_change = random.uniform(-150.0, 200.0)
        self.day_pnl += pnl_change
        self.mtm += pnl_change
        if random.random() > 0.7:
            self.trade_count += 1


if __name__ == "__main__":
    SampleMeanReversionStrategy().run()

