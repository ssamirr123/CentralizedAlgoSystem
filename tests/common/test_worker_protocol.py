"""Phase 16.9: trading/common/worker_protocol.py -- structural proof that
no worker-protocol message can ever carry a broker credential, API
key/secret, access token, or LiveAuthorization token."""
from __future__ import annotations

import dataclasses

from trading.common import worker_protocol as mod

_FORBIDDEN_FIELD_SUBSTRINGS = (
    "credential", "api_key", "api_secret", "access_token", "refresh_token",
    "session_token", "password", "secret", "authorization_token",
)

_MESSAGE_CLASSES = (
    mod.WorkerRegistration, mod.WorkerHeartbeat, mod.StrategyStartCommand, mod.StrategyStopCommand,
    mod.StrategyEvaluateCommand, mod.StrategyRuntimeUpdate, mod.OrderIntentSubmission, mod.OrderIntentResult,
)


def test_no_protocol_message_has_a_credential_or_token_shaped_field():
    for cls in _MESSAGE_CLASSES:
        for f in dataclasses.fields(cls):
            name = f.name.lower()
            for forbidden in _FORBIDDEN_FIELD_SUBSTRINGS:
                assert forbidden not in name, f"{cls.__name__}.{f.name} looks credential-shaped"


def test_module_never_imports_a_broker_adapter():
    import inspect

    source = inspect.getsource(mod)
    for forbidden in (
        "AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "SmartConnect", "SmartAPI",
        "breeze_connect", "BreezeConnect", "place_order", "modify_order", "cancel_order",
    ):
        assert forbidden not in source, forbidden


def test_order_intent_submission_carries_the_existing_order_intent_unmodified():
    from trading.common.broker import OrderSide, OrderType
    from trading.common.order_intent import OrderIntent

    intent = OrderIntent(
        strategy_id="StrategyA", account_id="ACC1", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="k1",
    )
    submission = mod.OrderIntentSubmission(
        worker_id="w1", session_id="s1", strategy_id="StrategyA", evaluation_id="e1", intent=intent,
    )
    assert submission.intent is intent
    assert submission.submission_id != ""
    assert submission.generated_at != ""


def test_worker_heartbeat_carries_no_authorization_claim():
    hb = mod.WorkerHeartbeat(worker_id="w1", session_id="s1", strategy_ids=("StrategyA",), runtime_state="HEALTHY")
    assert not hasattr(hb, "live_authorized")
    assert not hasattr(hb, "authorization_state")
