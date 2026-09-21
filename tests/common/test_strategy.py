"""Phase 10: the common Strategy interface + BaseStrategy's lifecycle
bookkeeping, exercised with a minimal concrete test double (not any of the
three real algo adapters -- those get their own dedicated test file)."""
from __future__ import annotations

import inspect

import pytest

from trading.common.order_intent import OrderIntent
from trading.common.strategy import (
    BaseStrategy,
    InvalidStrategyStateError,
    Strategy,
    StrategyMetrics,
    StrategyStatus,
)
from trading.common.trading_account import ExecutionMode

# Substrings that would indicate a broker-specific concept leaking into the
# Strategy interface -- the interface must never mention any of these.
_BROKER_MARKERS = ("broker", "order_id", "smart_api", "breeze", "dhan", "angelone", "place_order", "cancel_order")


class _RecordingStrategy(BaseStrategy):
    """A minimal concrete Strategy for testing BaseStrategy's own
    bookkeeping in isolation, independent of any real algo adapter."""

    def __init__(self, strategy_id: str = "TEST_STRATEGY", *, execution_mode=ExecutionMode.SHADOW, intents=None, fail_on: str | None = None):
        super().__init__(strategy_id, execution_mode=execution_mode)
        self._intents = intents if intents is not None else []
        self._fail_on = fail_on
        self.started = 0
        self.stopped = 0

    def _on_start(self) -> None:
        if self._fail_on == "start":
            raise RuntimeError("boom-on-start")
        self.started += 1

    def _on_stop(self) -> None:
        if self._fail_on == "stop":
            raise RuntimeError("boom-on-stop")
        self.stopped += 1

    def _on_generate_order_intents(self):
        if self._fail_on == "generate":
            raise RuntimeError("boom-on-generate")
        return self._intents


def _intent(strategy_id: str) -> OrderIntent:
    from trading.common.broker import OrderSide, OrderType

    return OrderIntent(strategy_id=strategy_id, account_id="ANY", symbol="NIFTY", exchange="NFO",
                        side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)


# --------------------------------------------------------------------------- #
# Interface shape: no broker-specific method anywhere on Strategy
# --------------------------------------------------------------------------- #
def test_strategy_interface_has_exactly_the_documented_methods():
    abstract_methods = Strategy.__abstractmethods__
    assert abstract_methods == {"initialize", "start", "stop", "generate_order_intents", "get_status", "get_metrics"}


def test_strategy_interface_contains_no_broker_specific_method_or_signature():
    for name, member in inspect.getmembers(Strategy):
        if name.startswith("_"):
            continue
        text = name.lower()
        for marker in _BROKER_MARKERS:
            assert marker not in text, f"Strategy.{name} looks broker-specific ({marker!r})"
        if inspect.isfunction(member):
            sig = str(inspect.signature(member)).lower()
            for marker in _BROKER_MARKERS:
                assert marker not in sig, f"Strategy.{name}{sig} mentions {marker!r}"


def test_strategy_is_abstract():
    with pytest.raises(TypeError):
        Strategy()  # type: ignore[abstract]


# --------------------------------------------------------------------------- #
# Lifecycle: disabled -> enabled -> starting -> running/shadow -> stopped
# --------------------------------------------------------------------------- #
def test_new_strategy_starts_disabled():
    s = _RecordingStrategy()
    assert s.get_status() == StrategyStatus.DISABLED


def test_enable_then_start_reaches_shadow_by_default():
    s = _RecordingStrategy()
    s.enable()
    assert s.get_status() == StrategyStatus.ENABLED
    s.start()
    assert s.get_status() == StrategyStatus.SHADOW
    assert s.started == 1


def test_start_with_paper_execution_mode_reaches_running():
    s = _RecordingStrategy(execution_mode=ExecutionMode.PAPER)
    s.enable()
    s.start()
    assert s.get_status() == StrategyStatus.RUNNING


def test_stop_transitions_back_to_stopped():
    s = _RecordingStrategy()
    s.enable()
    s.start()
    s.stop()
    assert s.get_status() == StrategyStatus.STOPPED
    assert s.stopped == 1


def test_cannot_start_a_disabled_strategy():
    s = _RecordingStrategy()
    with pytest.raises(InvalidStrategyStateError):
        s.start()


def test_cannot_start_an_already_running_strategy():
    s = _RecordingStrategy()
    s.enable()
    s.start()
    with pytest.raises(InvalidStrategyStateError):
        s.start()


def test_cannot_stop_a_disabled_strategy():
    s = _RecordingStrategy()
    with pytest.raises(InvalidStrategyStateError):
        s.stop()


def test_cannot_disable_a_running_strategy_without_stopping_first():
    s = _RecordingStrategy()
    s.enable()
    s.start()
    with pytest.raises(InvalidStrategyStateError):
        s.disable()


def test_can_reenable_after_stop():
    s = _RecordingStrategy()
    s.enable()
    s.start()
    s.stop()
    s.enable()
    assert s.get_status() == StrategyStatus.ENABLED


def test_initialize_resets_to_disabled():
    s = _RecordingStrategy()
    s.initialize()
    assert s.get_status() == StrategyStatus.DISABLED


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def test_start_failure_transitions_to_error_and_reraises():
    s = _RecordingStrategy(fail_on="start")
    s.enable()
    with pytest.raises(RuntimeError):
        s.start()
    assert s.get_status() == StrategyStatus.ERROR
    assert s.get_metrics().error_count == 1
    assert "boom-on-start" in s.get_metrics().last_error


def test_generate_order_intents_failure_transitions_to_error():
    s = _RecordingStrategy(fail_on="generate")
    s.enable()
    s.start()
    with pytest.raises(RuntimeError):
        s.generate_order_intents()
    assert s.get_status() == StrategyStatus.ERROR


def test_can_reenable_after_error():
    s = _RecordingStrategy(fail_on="start")
    s.enable()
    with pytest.raises(RuntimeError):
        s.start()
    s.enable()
    assert s.get_status() == StrategyStatus.ENABLED


def test_mark_error_is_an_optional_registry_facing_extra():
    s = _RecordingStrategy()
    s.enable()
    s.start()
    s.mark_error("supervisor detected a stall")
    assert s.get_status() == StrategyStatus.ERROR
    assert s.get_metrics().last_error == "supervisor detected a stall"


def test_cannot_generate_order_intents_when_not_active():
    s = _RecordingStrategy()
    with pytest.raises(InvalidStrategyStateError):
        s.generate_order_intents()


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_metrics_track_generated_intent_count():
    s = _RecordingStrategy(intents=[_intent("TEST_STRATEGY"), _intent("TEST_STRATEGY")])
    s.enable()
    s.start()
    intents = s.generate_order_intents()
    assert len(intents) == 2
    metrics = s.get_metrics()
    assert isinstance(metrics, StrategyMetrics)
    assert metrics.intents_generated == 2
    assert metrics.last_intent_at != ""
    assert metrics.status == StrategyStatus.SHADOW


def test_metrics_do_not_advance_on_an_empty_generation():
    s = _RecordingStrategy(intents=[])
    s.enable()
    s.start()
    s.generate_order_intents()
    assert s.get_metrics().intents_generated == 0
    assert s.get_metrics().last_intent_at == ""


def test_metrics_record_started_and_stopped_timestamps():
    s = _RecordingStrategy()
    s.enable()
    s.start()
    assert s.get_metrics().started_at != ""
    s.stop()
    assert s.get_metrics().stopped_at != ""


# --------------------------------------------------------------------------- #
# All seven StrategyStatus values from the spec are represented
# --------------------------------------------------------------------------- #
def test_status_enum_has_exactly_the_required_values():
    assert {s.value for s in StrategyStatus} == {
        "enabled", "disabled", "starting", "running", "stopped", "error", "shadow",
    }
