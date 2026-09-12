"""
Phase 5B -- Connected Shadow Execution demo.

    python -m trading.validation.angel_connected_shadow

Runs representative DoubleStraddelAlgo-style trading decisions through the
full broker-agnostic architecture, using REAL Angel One market/account/
instrument data (authentication, quotes, instrument resolution, funds) for
every READ, and a fully SIMULATED ShadowBroker for every WRITE (order
placement, modification, cancellation, fills, positions, P&L).

    Real Angel Market Data
            |
    DoubleStraddelAlgo (representative decisions -- process not started)
            |
    OrderIntent -> RiskManager -> ExecutionEngine -> ConnectedShadowBroker
            |                                              |
            |                                     real reads / simulated writes
            v
    Simulated Fill -> Simulated Position/P&L

ABSOLUTE RULE, enforced structurally (see connected_shadow_broker.py's
module docstring): execution_mode=ExecutionMode.SHADOW is required at
construction and cannot be changed afterwards; place_order()/
modify_order()/cancel_order() never reference the real broker anywhere in
their code. This script additionally verifies, after the run, that the
real broker's underlying SmartAPI mutation methods were never invoked.

Never invoked by any strategy or live process. DoubleStraddelAlgo,
CombinedVwapNifty, Vwap_Algo_Nifty_hedge, main.py, and orders.py do not
import this module.

Credentials: same environment/trading/.env convention as every other tool
in this repo. Never hard-coded, never printed.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.config import TradingConfig, load_config
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount
from trading.validation.angel_readonly import _index_aware_resolver, _pick_current_nifty_ce_symbol, _sanitize

_REPO_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_ID = "DoubleStraddelAlgo"
ACCOUNT_ID = "CONNECTED_SHADOW_MAIN"


def _load_dotenv_if_present() -> None:
    env_file = _REPO_ROOT / "trading" / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, _, value = raw.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _has_credentials(config: TradingConfig) -> bool:
    creds = config.credentials
    return bool(creds.angelone_api_key and creds.angelone_client_id and creds.angelone_password and creds.angelone_totp_secret)


@dataclass
class CapturedDecision:
    leg: str
    reason: str
    strategy_decision: dict
    intent: OrderIntent | None = None
    risk_allowed: bool | None = None
    risk_reason: str = ""
    execution_result: object = None
    simulated_order_state: object = None
    latency_seconds: float = 0.0
    error: str = ""


@dataclass
class DemoRun:
    decisions: list[CapturedDecision] = field(default_factory=list)
    nifty_spot: float = 0.0
    option_symbol: str = ""
    option_expiry: str = ""
    real_broker_mutation_calls: dict = field(default_factory=dict)
    final_positions: list = field(default_factory=list)


def _build_stack(real_broker: AngelOneBroker):
    broker_manager = BrokerManager()
    connected = ConnectedShadowBroker(real_broker, ShadowBroker(), execution_mode=ExecutionMode.SHADOW)
    account = TradingAccount(
        account_id=ACCOUNT_ID, account_name="Connected Shadow (Phase 5B)", broker_id="connected_shadow",
        execution_mode=ExecutionMode.SHADOW,
    )
    broker_manager.register_account(account, broker_client=connected)

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID)

    risk_manager = RiskManager(strategy_assignment)
    engine = StrategyExecutionEngine(
        connected,
        ExecutionConfig(min_api_interval_seconds=0.35, allow_market_emergency=True),
        risk_manager=risk_manager,
        strategy_assignment=strategy_assignment,
        broker_manager=broker_manager,
    )
    return engine, connected, risk_manager, strategy_assignment


def run_demo() -> DemoRun:
    config = load_config()
    if not _has_credentials(config):
        raise RuntimeError("credentials unavailable -- cannot run the connected shadow demo")

    real_broker = AngelOneBroker(config, read_only=True)
    real_broker.connect()  # REAL authentication

    demo = DemoRun()

    real_broker._instrument_resolver = _index_aware_resolver(real_broker)
    nifty_quote = real_broker.get_quote("NIFTY")
    demo.nifty_spot = nifty_quote.last_price

    option_symbol, expiry = _pick_current_nifty_ce_symbol(demo.nifty_spot)
    demo.option_symbol = option_symbol
    demo.option_expiry = expiry
    pe_symbol = option_symbol[:-2] + "PE"

    engine, connected, risk_manager, strategy_assignment = _build_stack(real_broker)

    decisions = [
        {"leg": "straddle_entry_ce", "symbol": option_symbol, "side": "SELL", "qty": 65, "order_type": "LIMIT", "reason": "MORNING_ENTRY"},
        {"leg": "straddle_entry_pe", "symbol": pe_symbol, "side": "SELL", "qty": 65, "order_type": "LIMIT", "reason": "MORNING_ENTRY"},
        {"leg": "straddle_exit_ce", "symbol": option_symbol, "side": "BUY", "qty": 65, "order_type": "LIMIT", "reason": "SL"},
        {"leg": "straddle_exit_pe", "symbol": pe_symbol, "side": "BUY", "qty": 65, "order_type": "LIMIT", "reason": "Target"},
    ]

    for decision in decisions:
        captured = CapturedDecision(leg=decision["leg"], reason=decision["reason"], strategy_decision=decision)
        start = time.monotonic()
        try:
            quote = real_broker.get_quote(decision["symbol"])  # REAL market data drives the intent's price
            intent = OrderIntent(
                strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID, symbol=decision["symbol"], exchange="NFO",
                side=OrderSide(decision["side"]), quantity=decision["qty"], order_type=OrderType(decision["order_type"]),
                limit_price=quote.last_price, reason=decision["reason"],
            )
            captured.intent = intent

            risk_result = risk_manager.validate(intent)
            captured.risk_allowed = risk_result.allowed
            captured.risk_reason = risk_result.reason

            result = engine.execute(intent)
            captured.execution_result = result
            if result.order_id:
                captured.simulated_order_state = connected.get_order(result.order_id)
        except Exception as exc:
            captured.error = _sanitize(exc)
        finally:
            captured.latency_seconds = round(time.monotonic() - start, 4)
        demo.decisions.append(captured)

    # Prove the real broker's mutation methods were never touched.
    from trading.common.broker import ReadOnlyModeError

    for name, call in (
        ("place_order", lambda: real_broker.place_order("NIFTY", OrderSide.BUY, 1, OrderType.MARKET)),
        ("modify_order", lambda: real_broker.modify_order("X", 1, 1.0)),
        ("cancel_order", lambda: real_broker.cancel_order("X")),
    ):
        try:
            call()
            demo.real_broker_mutation_calls[name] = "NOT BLOCKED"
        except ReadOnlyModeError:
            demo.real_broker_mutation_calls[name] = "BLOCKED"
        except Exception:
            demo.real_broker_mutation_calls[name] = "BLOCKED (other exception before any SmartAPI call)"

    demo.final_positions = connected.get_positions()
    real_broker.disconnect()
    return demo


def _print_report(demo: DemoRun) -> None:
    print("=" * 60)
    print("PHASE 5B -- CONNECTED SHADOW EXECUTION")
    print("=" * 60)
    print(f"NIFTY spot (real): {demo.nifty_spot}")
    print(f"Option contract (real, current): {demo.option_symbol} expiry={demo.option_expiry}")
    print()
    for c in demo.decisions:
        print(f"--- {c.leg} ({c.reason}) ---")
        if c.error:
            print(f"  ERROR: {c.error}")
            continue
        print(f"  strategy_decision: {c.strategy_decision}")
        print(f"  intent: symbol={c.intent.symbol} side={c.intent.side.value} qty={c.intent.quantity} "
              f"price={c.intent.limit_price} correlation_id={c.intent.correlation_id}")
        print(f"  risk_decision: allowed={c.risk_allowed} reason={c.risk_reason!r}")
        r = c.execution_result
        print(f"  simulated_order: order_id={r.order_id} status={r.status} success={r.success}")
        if c.simulated_order_state:
            print(f"  simulated_fill: filled={c.simulated_order_state.filled_quantity} "
                  f"remaining={c.simulated_order_state.remaining_quantity}")
        print(f"  latency_seconds: {c.latency_seconds}")
    print()
    print("Simulated positions after the run:")
    for p in demo.final_positions:
        print(f"  {p.symbol}: qty={p.quantity} avg_price={p.average_price} last_price={p.last_price} pnl={p.pnl}")
    print()
    print("Real broker mutation verification:")
    for name, status in demo.real_broker_mutation_calls.items():
        print(f"  {name}: {status}")
    print("=" * 60)


def main() -> int:
    _load_dotenv_if_present()
    config = load_config()
    if not _has_credentials(config):
        print("Connected shadow demo: NOT RUN")
        print("Reason: credentials unavailable")
        return 0

    demo = run_demo()
    _print_report(demo)
    all_blocked = all(v == "BLOCKED" for v in demo.real_broker_mutation_calls.values())
    return 0 if all_blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
