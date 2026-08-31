"""
Entry point: python trading/algos/example_strategy/main.py

Wires together, in order: config -> logger -> PID file -> broker connect
(with reconnect/backoff) -> strategy init -> heartbeat agent -> main loop
-> graceful shutdown.

This file is the template. Copy trading/algos/example_strategy/ to
trading/algos/<your_algo>/ and only strategy.py should need real changes
for a new strategy — main.py's plumbing stays the same.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[3]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from trading.algos.example_strategy.config import load_strategy_config
from trading.algos.example_strategy.strategy import ExampleStrategy
from trading.common.broker import BrokerConfigError, BrokerConnectionError, create_broker
from trading.common.config import load_config
from trading.common.heartbeat import ControlCenterHeartbeatAgent
from trading.common.log_shipper import LogShipper
from trading.common.logger import attach_shipper, get_logger, log_event
from trading.common.reporting import report_daily_pnl
from trading.common.utils import (
    GracefulShutdown,
    clear_stop_flag,
    get_hostname,
    get_pid,
    remove_pid_file,
    write_pid_file,
)

ALGO_NAME = "example_strategy"


def connect_with_retry(broker, config, logger: logging.Logger) -> None:
    """Connect to the broker, retrying with exponential backoff on failure.

    BrokerConfigError (missing/invalid credentials) is deliberately NOT
    retried — it's a permanent misconfiguration, not a transient blip, so
    retrying it just delays an inevitable failure by ~90s for nothing.
    """
    attempt = 0
    delay = config.broker_reconnect_backoff_seconds
    while True:
        attempt += 1
        try:
            broker.connect()
            return
        except BrokerConfigError:
            raise
        except BrokerConnectionError as exc:
            log_event(
                logger,
                logging.WARNING,
                "BROKER_CONNECT_FAILED",
                attempt=attempt,
                max_attempts=config.broker_reconnect_max_attempts,
                error=str(exc),
            )
            if attempt >= config.broker_reconnect_max_attempts:
                raise
            time.sleep(delay)
            delay *= 2


def main() -> int:
    config = load_config()
    strategy_config = load_strategy_config()

    server_name = config.server_name or get_hostname()
    logger = get_logger(component=ALGO_NAME, server=server_name, algo=ALGO_NAME)

    log_event(
        logger, logging.INFO, "ALGO_STARTED",
        pid=get_pid(), trading_mode=config.trading_mode, broker=config.broker_name,
    )
    write_pid_file(ALGO_NAME)
    clear_stop_flag(ALGO_NAME)  # defensive: drop any stale flag from a prior run

    # Created early, before any WARNING/ERROR-level event can fire (e.g. a
    # broker connect failure) -- SHIPPABLE_EVENTS ships WARNING+ regardless
    # of event name, so this needs to be attached before those can happen,
    # not just before the main loop. Explicitly stopped (drains the queue)
    # on every early-return path below, since a daemon thread's queued-but-
    # unsent items don't survive an abrupt process exit.
    shipper: LogShipper | None = None
    if config.control_api_key:
        shipper = LogShipper(
            algo_name=config.strategy_name, server_name=server_name,
            api_base_url=config.api_base_url, api_key=config.control_api_key,
        )
        shipper.start()
        attach_shipper(logger, shipper)

    shutdown = GracefulShutdown(algo_name=ALGO_NAME)

    broker = create_broker(config)
    try:
        connect_with_retry(broker, config, logger)
    except BrokerConfigError as exc:
        log_event(logger, logging.ERROR, "BROKER_CONFIG_ERROR", error=str(exc))
        if shipper:
            shipper.stop()
        remove_pid_file(ALGO_NAME)
        return 1
    except BrokerConnectionError as exc:
        log_event(logger, logging.ERROR, "BROKER_CONNECT_FATAL", error=str(exc))
        if shipper:
            shipper.stop()
        remove_pid_file(ALGO_NAME)
        return 1
    except NotImplementedError as exc:
        # Expected for the real broker stubs (zerodha/angelone/icici_breeze)
        # until their connect() methods are actually implemented.
        log_event(logger, logging.ERROR, "BROKER_NOT_IMPLEMENTED", error=str(exc))
        if shipper:
            shipper.stop()
        remove_pid_file(ALGO_NAME)
        return 1

    log_event(logger, logging.INFO, "BROKER_CONNECTED", broker=config.broker_name)

    try:
        quote = broker.get_quote(strategy_config.symbol)
        log_event(logger, logging.INFO, "MARKET_DATA_CONNECTED", symbol=quote.symbol, last_price=quote.last_price)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, logging.ERROR, "MARKET_DATA_FAILED", error=str(exc))
        if shipper:
            shipper.stop()
        broker.disconnect()
        remove_pid_file(ALGO_NAME)
        return 1

    strategy = ExampleStrategy(broker=broker, config=strategy_config, logger=logger)
    strategy.on_start()
    log_event(logger, logging.INFO, "STRATEGY_INITIALIZED")
    log_event(logger, logging.INFO, "START")  # shippable per the project's own event list

    # Heartbeat sender feeding the control-center schema (heartbeats table
    # via POST /api/heartbeat). Runs only if CONTROL_API_KEY is configured
    # -- the strategy still runs without it, it just won't report to the
    # dashboard.
    control_center_agent: ControlCenterHeartbeatAgent | None = None
    if config.control_api_key:
        control_center_agent = ControlCenterHeartbeatAgent(
            algo_name=config.strategy_name,
            server_name=server_name,
            api_base_url=config.api_base_url,
            api_key=config.control_api_key,
            interval_seconds=config.control_heartbeat_interval_seconds,
        )
        control_center_agent.start()
        log_event(
            logger, logging.INFO, "CONTROL_CENTER_HEARTBEAT_RUNNING",
            interval_seconds=config.control_heartbeat_interval_seconds,
        )
    else:
        log_event(logger, logging.INFO, "CONTROL_CENTER_HEARTBEAT_SKIPPED", reason="CONTROL_API_KEY not set")

    log_event(logger, logging.INFO, "ALGO_RUNNING")

    # Throttled to roughly the same cadence as the control-center heartbeat
    # rather than every tick -- reuses that interval instead of adding a
    # third background thread just for this.
    last_pnl_report_monotonic = 0.0

    exit_code = 0
    try:
        while not shutdown.is_set():
            try:
                status, _mtm, day_pnl, trade_count = strategy.on_tick()
                if control_center_agent is not None:
                    control_center_agent.update_metrics(status=status, pnl=day_pnl)

                    now = time.monotonic()
                    if now - last_pnl_report_monotonic >= config.control_heartbeat_interval_seconds:
                        report_daily_pnl(
                            config.api_base_url, config.control_api_key, config.strategy_name,
                            server_name, pnl=day_pnl, trade_count=trade_count,
                        )
                        last_pnl_report_monotonic = now
            except Exception as exc:  # noqa: BLE001
                # A single bad tick should not crash the whole process —
                # log it, report ERROR status via heartbeat, keep looping.
                log_event(logger, logging.ERROR, "TICK_ERROR", error=str(exc))
                if control_center_agent is not None:
                    control_center_agent.update_metrics(status="ERROR", pnl=strategy.day_pnl)

            shutdown.wait(strategy_config.loop_interval_seconds)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, logging.CRITICAL, "FATAL_ERROR", error=str(exc))
        exit_code = 1
    finally:
        log_event(logger, logging.INFO, "SHUTDOWN_INITIATED")
        try:
            strategy.on_stop()
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "STRATEGY_STOP_ERROR", error=str(exc))

        if control_center_agent is not None:
            control_center_agent.update_metrics(status="STOPPED", pnl=strategy.day_pnl)
            control_center_agent.stop()
            report_daily_pnl(
                config.api_base_url, config.control_api_key, config.strategy_name,
                server_name, pnl=strategy.day_pnl, trade_count=strategy.trade_count,
            )

        log_event(logger, logging.INFO, "STOP")  # shippable per the project's own event list

        if shipper:
            shipper.stop()  # drains the queue (including the STOP event above) before exit

        try:
            broker.disconnect()
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "BROKER_DISCONNECT_ERROR", error=str(exc))

        remove_pid_file(ALGO_NAME)
        clear_stop_flag(ALGO_NAME)
        log_event(logger, logging.INFO, "ALGO_STOPPED_SAFELY")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
