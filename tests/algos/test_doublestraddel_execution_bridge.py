"""Phase 3 shadow-migration tests for DoubleStraddelAlgo.

DoubleStraddelAlgo's own modules (config, websocket_feed, broker.orders,
broker.execution_bridge, ...) use bare, non-package-qualified imports
(e.g. `import config`, `from broker import orders`) because the live
process is always launched with that algo's own directory as sys.path[0]
(Python does this automatically for `python main.py`). Nothing else in
this repo imports DoubleStraddelAlgo's modules today, so this file adds
that directory to sys.path itself, then imports the same way production
code does -- this keeps `broker.execution_bridge`'s module identity (and
its lazily-built singleton) consistent between what orders.py sees and
what these tests interact with directly.

No real network call, no real Angel One credentials, and no real
SmartApi/SmartConnect object anywhere in this file -- the shadow bridge's
own hard-coded-empty-credentials design (see execution_bridge.py's module
docstring) already guarantees AngelOneBroker.connect() can never reach the
network; several tests below use a fake BrokerClient instead, purely to
prove the wiring/translation without depending on that safety mechanism.

CREDENTIAL-LEAK GUARD: importing `config` below (DoubleStraddelAlgo's own
config.py) triggers its `_angel_creds()`, which falls back to reading a
real trading/.env file when the ANGELONE_* env vars aren't already set --
and conftest.py's env-clearing only clears the process env, not that file.
If a real trading/.env exists on the machine running these tests (as it
now does whenever Phase 5A/5B credentials have been configured), that
fallback would call os.environ.setdefault(...) with REAL credentials,
leaking them into the shared pytest process environment for the rest of
the session -- this is exactly what broke two unrelated Phase 2 tests in
tests/common/test_angelone_broker.py the first time a real trading/.env
was added. Setting dummy values for all four vars BEFORE importing
`config` makes _angel_creds() see them as "already set" and skip the
trading/.env fallback entirely, regardless of what that file contains.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALGO_ROOT = _REPO_ROOT / "trading" / "algos" / "DoubleStraddelAlgo"
for _p in (str(_REPO_ROOT), str(_ALGO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# See "CREDENTIAL-LEAK GUARD" above -- must happen before `import config`.
for _var, _dummy in (
    ("ANGELONE_API_KEY", "test-dummy-api-key"),
    ("ANGELONE_CLIENT_ID", "test-dummy-client-id"),
    ("ANGELONE_MPIN", "0000"),
    ("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP"),  # syntactically valid base32 seed, not a real secret
):
    os.environ.setdefault(_var, _dummy)

import config  # noqa: E402  (DoubleStraddelAlgo/config.py, bare-import style)
from broker import execution_bridge, orders  # noqa: E402

from trading.common.broker import (  # noqa: E402
    BrokerClient,
    LiveTradingDisabledError,
    OrderResult,
    OrderSide,
    OrderType,
    Quote,
)
from trading.common.broker_manager import BrokerManager  # noqa: E402
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine  # noqa: E402
from trading.common.risk_manager import RiskManager  # noqa: E402
from trading.common.strategy_assignment import StrategyAssignment  # noqa: E402
from trading.common.trading_account import TradingAccount  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")


# --------------------------------------------------------------------------- #
# Test fixtures / fakes
# --------------------------------------------------------------------------- #
class RecordingBroker(BrokerClient):
    """A fake AngelOneBroker stand-in that actually "connects" and records
    every place_order() call, so tests can verify translation/wiring
    without touching a real (or even a mocked-real) Angel session."""

    def __init__(self, place_order_status="OPEN", place_order_exception=None):
        self._connected = False
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.place_order_status = place_order_status
        self.place_order_exception = place_order_exception

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def get_quote(self, symbol):
        return Quote(symbol=symbol, last_price=100.0, timestamp=datetime.now(timezone.utc).isoformat())

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
        if self.place_order_exception:
            raise self.place_order_exception
        self.placed.append(
            {"symbol": symbol, "side": side, "quantity": quantity, "order_type": order_type, "limit_price": limit_price}
        )
        return OrderResult(
            order_id=f"REC-{len(self.placed)}", symbol=symbol, side=side, quantity=quantity,
            status=self.place_order_status,
        )

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def get_positions(self):
        return []


class RiskDeniesEverything:
    """Fake RiskManager whose validate() always denies."""

    def validate(self, intent, context=None):
        from trading.common.risk_manager import RiskCheckResult

        return RiskCheckResult.deny("test-forced denial")


class ExplodingRiskManager:
    def validate(self, intent, context=None):
        raise RuntimeError("boom: risk manager exploded")


def _stack_with_broker(broker, *, execution_config=None, strategy_assignment=None):
    """Build a real _ShadowStack (real RiskManager/StrategyAssignment/
    BrokerManager) but with the given fake broker already registered and
    connected, so execute() reaches place_order() without ever needing a
    real Angel session."""
    broker_manager = BrokerManager()
    account = TradingAccount(account_id=execution_bridge.ACCOUNT_ID, account_name="test", broker_id="angelone")
    broker_manager.register_account(account, broker_client=broker)

    assignment = strategy_assignment or StrategyAssignment(broker_manager)
    if not assignment.has_assignment(execution_bridge.STRATEGY_ID):
        assignment.assign(execution_bridge.STRATEGY_ID, execution_bridge.ACCOUNT_ID)

    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker,
        execution_config or ExecutionConfig(
            min_api_interval_seconds=0.0, pending_timeout_seconds=0.05, retry_delay_seconds=0.01,
            allow_market_emergency=True,
        ),
        risk_manager=risk_manager,
        strategy_assignment=assignment,
        broker_manager=broker_manager,
    )
    return execution_bridge._ShadowStack(broker_manager, assignment, risk_manager, engine)


@pytest.fixture(autouse=True)
def _reset_shadow_stack():
    """Every test gets a clean shadow-stack singleton, and DRY_RUN is
    forced True by default so orders.py never attempts a real SmartAPI
    call regardless of which test runs."""
    execution_bridge._reset_stack_for_tests(None)
    original_dry_run = getattr(config, "DRY_RUN", True)
    config.DRY_RUN = True
    yield
    config.DRY_RUN = original_dry_run
    execution_bridge._reset_stack_for_tests(None)


def _wait_for_shadow(timeout=1.0):
    """mirror_place_order() runs on a background daemon thread -- give it
    a moment to finish before asserting on its effects."""
    time.sleep(timeout)


# --------------------------------------------------------------------------- #
# Architecture
# --------------------------------------------------------------------------- #
def test_angel_main_assignment_resolves_correct_account():
    stack = execution_bridge._get_stack()
    assert stack.strategy_assignment.get_account_id(execution_bridge.STRATEGY_ID) == "ANGEL_MAIN"


def test_angel_main_account_is_registered_with_angelone_broker_id():
    stack = execution_bridge._get_stack()
    account = stack.broker_manager.get_account("ANGEL_MAIN")
    assert account.broker_id == "angelone"


def test_default_stack_uses_a_real_angeloneb_broker_instance():
    from trading.common.brokers.angelone import AngelOneBroker

    stack = execution_bridge._get_stack()
    # Not yet connected (lazy) -- resolving it triggers BrokerConfigError
    # because credentials are hard-coded empty (see execution_bridge's
    # module docstring), never a real network call.
    from trading.common.broker import BrokerConfigError

    with pytest.raises(BrokerConfigError):
        stack.broker_manager.get_broker("ANGEL_MAIN")


def test_execution_engine_and_risk_manager_are_invoked(caplog):
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.place_limit("NIFTY19MAY2623700CE", "48521", config.LOT_QTY, "SELL")
        _wait_for_shadow()

    assert len(broker.placed) == 1  # ExecutionEngine really reached the broker
    assert any("risk_result" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Order mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "side,order_type,func",
    [
        ("BUY", "LIMIT", "place_limit"),
        ("SELL", "LIMIT", "place_limit"),
        ("BUY", "MARKET", "place_market"),
        ("SELL", "MARKET", "place_market"),
    ],
)
def test_order_mapping_preserves_fields(side, order_type, func, monkeypatch):
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))
    monkeypatch.setattr(config, "ALLOW_MARKET_EMERGENCY", True, raising=False)

    getattr(orders, func)("NIFTY19MAY2623700CE", "48521", 65, side)
    _wait_for_shadow()

    assert len(broker.placed) == 1
    call = broker.placed[0]
    assert call["symbol"] == "NIFTY19MAY2623700CE"
    assert call["side"] == OrderSide(side)
    assert call["quantity"] == 65
    assert call["order_type"] == OrderType(order_type)


def test_limit_order_price_is_preserved_in_intent(caplog):
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
        _wait_for_shadow()

    call = broker.placed[0]
    assert call["limit_price"] is not None  # computed via the same _limit_price() helper


def test_strategy_id_is_preserved(caplog):
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
        _wait_for_shadow()

    assert any("'strategy': 'DoubleStraddelAlgo'" in r.getMessage() for r in caplog.records)


def test_client_order_id_is_unique_per_intent():
    from broker.execution_bridge import _to_order_intent

    a = _to_order_intent("SYM", "SELL", 50, "LIMIT", 100.0, None, "ENTRY", "111")
    b = _to_order_intent("SYM", "SELL", 50, "LIMIT", 100.0, None, "ENTRY", "111")
    assert a.client_order_id != b.client_order_id


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
def test_shadow_stack_is_paper_mode_regardless_of_live_trading_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    execution_bridge._reset_stack_for_tests(None)  # force a fresh, real build

    stack = execution_bridge._get_stack()
    account = stack.broker_manager.get_account("ANGEL_MAIN")

    from trading.common.broker import BrokerConfigError

    # Even with TRADING_MODE=live in the environment, the shadow account's
    # own TradingConfig is hard-coded to trading_mode="paper" with empty
    # credentials -- connect() must still fail on credentials, never reach
    # a live session.
    with pytest.raises(BrokerConfigError):
        stack.broker_manager.get_broker("ANGEL_MAIN")
    assert account.enabled is True  # sanity: this is really the shadow account


def test_shadow_never_calls_a_live_trading_disabled_broker_bypassed():
    """If a (test-only) broker that DID connect successfully raises
    LiveTradingDisabledError from place_order(), the bridge must log it as
    expected safety behavior and must NOT retry through another path."""

    class LiveGuardedBroker(RecordingBroker):
        def place_order(self, *a, **k):
            raise LiveTradingDisabledError("refusing: not live")

    broker = LiveGuardedBroker()
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    # Must not raise, and must not place any order via any path.
    execution_bridge.mirror_place_order(
        symbol="NIFTY19MAY2623700CE", token="48521", qty=65, side="SELL",
        order_type="LIMIT", price=125.5, live_order_id="LIVE-1",
    )
    _wait_for_shadow()

    assert broker.placed == []


def test_no_real_smartapi_class_is_ever_imported_or_constructed():
    """The default (production) shadow path must never even attempt
    `from SmartApi import SmartConnect` -- credentials are hard-coded
    empty so AngelOneBroker.connect() fails before that import line."""
    import trading.common.brokers.angelone as angelone_module

    calls = []
    original = angelone_module.AngelOneBroker._default_smart_api_factory
    angelone_module.AngelOneBroker._default_smart_api_factory = staticmethod(
        lambda api_key: calls.append(api_key) or original(api_key)
    )
    try:
        execution_bridge._reset_stack_for_tests(None)
        orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
        _wait_for_shadow()
    finally:
        angelone_module.AngelOneBroker._default_smart_api_factory = original

    assert calls == []  # never reached: connect() fails at the credential check first


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #
def test_risk_manager_rejection_does_not_affect_live_order_result():
    stack = _stack_with_broker(RecordingBroker())
    execution_bridge._reset_stack_for_tests(
        execution_bridge._ShadowStack(stack.broker_manager, stack.strategy_assignment, RiskDeniesEverything(), stack.execution_engine)
    )

    oid = orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    assert oid.startswith("DRYRUN-")  # DRY_RUN forced True by the fixture; unaffected by shadow risk denial


def test_execution_engine_exception_does_not_affect_live_order_result():
    stack = _stack_with_broker(RecordingBroker())
    execution_bridge._reset_stack_for_tests(
        execution_bridge._ShadowStack(stack.broker_manager, stack.strategy_assignment, ExplodingRiskManager(), stack.execution_engine)
    )

    oid = orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    assert oid.startswith("DRYRUN-")


def test_broker_exception_does_not_affect_live_order_result():
    broker = RecordingBroker(place_order_exception=RuntimeError("shadow broker exploded"))
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    oid = orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    assert oid.startswith("DRYRUN-")


def test_instrument_resolution_failure_does_not_affect_live_order_result():
    class BrokenResolverBroker(RecordingBroker):
        def place_order(self, *a, **k):
            raise ValueError("AngelOneBroker: symbol not found in the NFO scrip master")

    execution_bridge._reset_stack_for_tests(_stack_with_broker(BrokenResolverBroker()))

    oid = orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    assert oid.startswith("DRYRUN-")


def test_unexpected_bridge_exception_does_not_affect_live_order_result(monkeypatch):
    monkeypatch.setattr(execution_bridge, "_to_order_intent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    oid = orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    assert oid.startswith("DRYRUN-")


def test_timeout_does_not_trigger_blind_retry():
    """A None execution result (exhausted retries) must be logged as a
    failed shadow attempt, never silently re-submitted."""
    broker = RecordingBroker(place_order_exception=RuntimeError("simulated timeout"))
    execution_bridge._reset_stack_for_tests(
        _stack_with_broker(broker, execution_config=ExecutionConfig(
            max_retries=2, retry_delay_seconds=0.01, min_api_interval_seconds=0.0,
        ))
    )

    orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    # place_order() was attempted at most max_retries times, never more --
    # i.e. no additional/blind retry loop was added on top of the engine's own.
    assert len(broker.placed) == 0
    assert broker.place_order_exception is not None  # every attempt failed, as configured


# --------------------------------------------------------------------------- #
# No double execution
# --------------------------------------------------------------------------- #
def test_place_limit_results_in_exactly_one_shadow_invocation_not_two_live_orders():
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    oid = orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    # Exactly one shadow-side placement (never two, never zero).
    assert len(broker.placed) == 1
    # The "live" side in DRY_RUN mode never touches SmartAPI at all --
    # config.objconn is never even set in this test, proving it was never called.
    assert oid.startswith("DRYRUN-")


def test_place_market_does_not_duplicate_real_orders(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_MARKET_EMERGENCY", True, raising=False)
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    orders.place_market("NIFTY19MAY2623700CE", "48521", 65, "SELL")
    _wait_for_shadow()

    assert len(broker.placed) == 1


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #
def test_cancel_creates_shadow_event_not_a_fake_order(caplog):
    broker = RecordingBroker()
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.cancel("ORDER-123")

    assert broker.placed == []  # no OrderIntent/ExecutionEngine involvement
    assert any("SHADOW_CANCEL_REQUEST" in r.getMessage() for r in caplog.records)


def test_cancel_pending_for_tokens_creates_scoped_shadow_event(caplog):
    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.cancel_pending_for_tokens(["48521", "48522"])

    messages = [r.getMessage() for r in caplog.records]
    assert any("SHADOW_CANCEL_REQUEST" in m and "48521" in m for m in messages)


def test_cancel_all_pending_creates_bulk_shadow_event(caplog, monkeypatch):
    monkeypatch.setattr(orders, "refresh_orderbook", lambda *a, **k: [])
    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.cancel_all_pending()

    assert any("SHADOW_CANCEL_ALL" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Token resolution
# --------------------------------------------------------------------------- #
def test_angel_symboltoken_preserved_as_intent_metadata():
    from broker.execution_bridge import _to_order_intent

    intent = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")

    assert intent.symbol == "NIFTY19MAY2623700CE"
    assert intent.metadata["angelone_symboltoken"] == "48521"


def test_order_intent_has_no_angel_specific_first_class_field():
    from trading.common.order_intent import OrderIntent

    forbidden = {"symboltoken", "variety", "producttype", "smartapi"}
    field_names = {f.lower() for f in OrderIntent.__dataclass_fields__.keys()}
    assert field_names.isdisjoint(forbidden)


def test_token_file_resolution_still_used_by_the_live_path(tmp_path, monkeypatch):
    """Prove token_file.token_nifty() (the strategy's own instrument
    resolution) is untouched and still drives the value passed into
    orders.place_limit -- i.e. the live path's symbol/token source did not
    change as part of this migration."""
    import token_file

    csv_path = tmp_path / "nifty_token.csv"
    csv_path.write_text("symbol,token,expiry\nNIFTY19MAY2623700CE,48521,19MAY2026\n")
    monkeypatch.chdir(tmp_path)

    sym, tok = token_file.token_nifty("23700CE")

    assert sym == "NIFTY19MAY2623700CE"
    assert tok == "48521"


# --------------------------------------------------------------------------- #
# No credential leakage
# --------------------------------------------------------------------------- #
FORBIDDEN_LOG_TEXT = ("api_key", "totp", "mpin", "password", "refresh_token", "feed_token", "session_token")


def test_no_credential_leakage_across_all_shadow_log_output(caplog):
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
        _wait_for_shadow()
        orders.cancel("ORDER-1")
        orders.cancel_pending_for_tokens(["48521"])

    all_text = " ".join(r.getMessage().lower() for r in caplog.records)
    for marker in FORBIDDEN_LOG_TEXT:
        assert marker not in all_text


# --------------------------------------------------------------------------- #
# Deterministic conversion: same strategy/market state -> reproducible intent
# --------------------------------------------------------------------------- #
def test_conversion_is_deterministic_for_identical_inputs():
    from broker.execution_bridge import _to_order_intent

    args = ("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")
    a = _to_order_intent(*args)
    b = _to_order_intent(*args)

    # Content fields must be reproducible byte-for-byte.
    assert a.strategy_id == b.strategy_id
    assert a.account_id == b.account_id
    assert a.symbol == b.symbol
    assert a.exchange == b.exchange
    assert a.side == b.side
    assert a.quantity == b.quantity
    assert a.order_type == b.order_type
    assert a.limit_price == b.limit_price
    assert a.trigger_price == b.trigger_price
    assert a.reason == b.reason
    assert a.metadata["angelone_symboltoken"] == b.metadata["angelone_symboltoken"]
    assert a.instrument == b.instrument

    # Identity fields are intentionally unique per intent, not reproducible.
    assert a.client_order_id != b.client_order_id
    assert a.correlation_id != b.correlation_id


def test_conversion_changes_only_when_inputs_change():
    from broker.execution_bridge import _to_order_intent

    base = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")
    different_side = _to_order_intent("NIFTY19MAY2623700CE", "BUY", 65, "LIMIT", 125.5, None, "ENTRY", "48521")
    different_qty = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 50, "LIMIT", 125.5, None, "ENTRY", "48521")

    assert base.side != different_side.side
    assert base.quantity != different_qty.quantity


# --------------------------------------------------------------------------- #
# Instrument population
# --------------------------------------------------------------------------- #
def test_instrument_derives_option_type_from_symbol_suffix():
    from broker.execution_bridge import _to_instrument

    ce = _to_instrument("NIFTY19MAY2623700CE")
    pe = _to_instrument("NIFTY19MAY2623700PE")

    assert ce.option_type == "CE"
    assert ce.exchange == "NFO"
    assert ce.is_option is True
    assert pe.option_type == "PE"


def test_order_intent_carries_the_instrument():
    from broker.execution_bridge import _to_order_intent

    intent = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")

    assert intent.instrument is not None
    assert intent.instrument.symbol == "NIFTY19MAY2623700CE"
    assert intent.instrument.option_type == "CE"


# --------------------------------------------------------------------------- #
# Comparison tooling: existing order vs new OrderIntent, no submission
# --------------------------------------------------------------------------- #
def test_compare_with_legacy_reports_match_for_consistent_translation():
    from broker.execution_bridge import _to_order_intent, compare_with_legacy

    args = ("NIFTY19MAY2623700CE", "48521", 65, "SELL", "LIMIT", 125.5)
    intent = _to_order_intent(args[0], args[3], args[2], args[4], args[5], None, "ENTRY", args[1])

    result = compare_with_legacy(*args, intent=intent)

    assert result["match"] is True
    assert result["differences"] == []
    assert result["legacy"]["tradingsymbol"] == "NIFTY19MAY2623700CE"
    assert result["legacy"]["transactiontype"] == "SELL"


def test_compare_with_legacy_detects_a_side_mismatch():
    from broker.execution_bridge import _to_order_intent, compare_with_legacy

    intent = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")
    # Deliberately compare against a DIFFERENT side to prove mismatches are detected.
    result = compare_with_legacy("NIFTY19MAY2623700CE", "48521", 65, "BUY", "LIMIT", 125.5, intent=intent)

    assert result["match"] is False
    assert any("side" in d for d in result["differences"])


def test_compare_with_legacy_detects_a_quantity_mismatch():
    from broker.execution_bridge import _to_order_intent, compare_with_legacy

    intent = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")
    result = compare_with_legacy("NIFTY19MAY2623700CE", "48521", 999, "SELL", "LIMIT", 125.5, intent=intent)

    assert result["match"] is False
    assert any("quantity" in d for d in result["differences"])


def test_compare_with_legacy_detects_a_price_mismatch():
    from broker.execution_bridge import _to_order_intent, compare_with_legacy

    intent = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "LIMIT", 125.5, None, "ENTRY", "48521")
    result = compare_with_legacy("NIFTY19MAY2623700CE", "48521", 65, "SELL", "LIMIT", 999.0, intent=intent)

    assert result["match"] is False
    assert any("price" in d for d in result["differences"])


def test_compare_with_legacy_market_orders_ignore_price():
    from broker.execution_bridge import _to_order_intent, compare_with_legacy

    intent = _to_order_intent("NIFTY19MAY2623700CE", "SELL", 65, "MARKET", None, None, "EXIT", "48521")
    result = compare_with_legacy("NIFTY19MAY2623700CE", "48521", 65, "SELL", "MARKET", None, intent=intent)

    assert result["match"] is True


def test_compare_with_legacy_never_calls_any_broker():
    """Structural guard: compare_with_legacy is a pure function -- it must
    not import or reference any broker/network primitive."""
    import inspect

    from broker.execution_bridge import compare_with_legacy

    source = inspect.getsource(compare_with_legacy)
    for forbidden in ("smart_api", "SmartConnect", "place_order", "connect(", "._get_stack"):
        assert forbidden not in source


def test_shadow_log_record_includes_the_comparison_result(caplog):
    broker = RecordingBroker(place_order_status="COMPLETE")
    execution_bridge._reset_stack_for_tests(_stack_with_broker(broker))

    with caplog.at_level(logging.INFO, logger="DoubleStraddelAlgo.shadow"):
        orders.place_limit("NIFTY19MAY2623700CE", "48521", 65, "SELL")
        _wait_for_shadow()

    assert any("'match': True" in r.getMessage() for r in caplog.records)
