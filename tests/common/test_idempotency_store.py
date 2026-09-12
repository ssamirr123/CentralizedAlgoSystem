"""Phase 14.6 Blocker D: trading/common/idempotency_store.py in isolation.
See tests/common/test_execute_idempotency_persistence.py for the
end-to-end StrategyExecutionEngine.execute() + restart-simulation proof."""
from __future__ import annotations

from trading.common.idempotency_store import (
    STATUS_COMPLETED,
    IdempotencyRecord,
    InMemoryIdempotencyStore,
    SqliteIdempotencyStore,
    compute_intent_hash,
)
from trading.common.broker import OrderSide, OrderType
from trading.common.order_intent import OrderIntent


def _record(key="k1", **overrides) -> IdempotencyRecord:
    fields = dict(
        idempotency_key=key, strategy_id="S", account_id="A", intent_hash="h1",
        status=STATUS_COMPLETED, broker_order_id="AO-1", result_json="{}",
        created_at="t1", updated_at="t1",
    )
    fields.update(overrides)
    return IdempotencyRecord(**fields)


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="S", account_id="A", symbol="NIFTY15SEP2623400CE", exchange="NFO",
        side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="k1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


# --------------------------------------------------------------------------- #
# compute_intent_hash
# --------------------------------------------------------------------------- #
def test_identical_intents_hash_identically():
    assert compute_intent_hash(_intent()) == compute_intent_hash(_intent())


def test_different_quantity_hashes_differently():
    assert compute_intent_hash(_intent(quantity=1)) != compute_intent_hash(_intent(quantity=2))


def test_different_idempotency_key_hashes_differently():
    assert compute_intent_hash(_intent(idempotency_key="k1")) != compute_intent_hash(_intent(idempotency_key="k2"))


# --------------------------------------------------------------------------- #
# InMemoryIdempotencyStore -- basic contract (test-only implementation)
# --------------------------------------------------------------------------- #
def test_in_memory_store_get_returns_none_when_absent():
    store = InMemoryIdempotencyStore()
    assert store.get("missing") is None


def test_in_memory_store_put_then_get_round_trips():
    store = InMemoryIdempotencyStore()
    store.put(_record())
    got = store.get("k1")
    assert got is not None
    assert got.broker_order_id == "AO-1"


def test_in_memory_store_does_not_survive_a_fresh_instance():
    """Documents, structurally, exactly why this class must never be used
    as the production store: a "restart" (a fresh instance) loses
    everything."""
    store1 = InMemoryIdempotencyStore()
    store1.put(_record())
    store2 = InMemoryIdempotencyStore()  # simulates a restart with no shared state
    assert store2.get("k1") is None


# --------------------------------------------------------------------------- #
# SqliteIdempotencyStore -- the authoritative, persistent implementation
# --------------------------------------------------------------------------- #
def test_sqlite_store_get_returns_none_when_absent(tmp_path):
    store = SqliteIdempotencyStore(tmp_path / "idem.db")
    assert store.get("missing") is None


def test_sqlite_store_put_then_get_round_trips(tmp_path):
    store = SqliteIdempotencyStore(tmp_path / "idem.db")
    store.put(_record())
    got = store.get("k1")
    assert got is not None
    assert got.strategy_id == "S"
    assert got.account_id == "A"
    assert got.broker_order_id == "AO-1"
    assert got.status == STATUS_COMPLETED


def test_sqlite_store_upserts_on_conflict(tmp_path):
    store = SqliteIdempotencyStore(tmp_path / "idem.db")
    store.put(_record(status="PENDING", broker_order_id=""))
    store.put(_record(status=STATUS_COMPLETED, broker_order_id="AO-2"))
    got = store.get("k1")
    assert got.status == STATUS_COMPLETED
    assert got.broker_order_id == "AO-2"


def test_sqlite_store_persists_across_a_new_instance_same_file():
    """THE restart simulation, at the store level: a brand-new
    SqliteIdempotencyStore instance pointed at the SAME file must see
    what a previous instance wrote -- this is what makes the store
    'authoritative' rather than an in-memory cache."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "idem.db"
        store1 = SqliteIdempotencyStore(db_path)
        store1.put(_record())
        del store1  # simulate the process exiting

        store2 = SqliteIdempotencyStore(db_path)  # simulate a fresh process starting
        got = store2.get("k1")
        assert got is not None
        assert got.broker_order_id == "AO-1"


def test_sqlite_store_empty_key_returns_none(tmp_path):
    store = SqliteIdempotencyStore(tmp_path / "idem.db")
    assert store.get("") is None
