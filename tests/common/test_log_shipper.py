"""LogShipper -- ported from trading/common/test_log_shipper_local.py."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from trading.common.log_shipper import LogShipper, should_ship


def test_normal_send():
    with patch("trading.common.log_shipper.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=True, status_code=200)
        shipper = LogShipper(algo_name="x", server_name="y", api_base_url="http://127.0.0.1:9999", api_key="k")
        shipper.start()
        shipper.ship(level="INFO", event="ENTRY", details={"strike": 24750})
        time.sleep(0.5)
        shipper.stop()

        assert mock_post.call_count == 1
        call = mock_post.call_args
        url = call.args[0] if call.args else call.kwargs.get("url")
        payload = call.kwargs.get("json")
        assert url == "http://127.0.0.1:9999/api/logs"
        assert payload["algo_id"] == "x" and payload["server_id"] == "y"
        assert payload["event"] == "ENTRY" and payload["details"] == {"strike": 24750}


def test_stop_drains_queue():
    with patch("trading.common.log_shipper.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=True, status_code=200)
        shipper = LogShipper(algo_name="x", server_name="y", api_base_url="http://x", api_key="k")
        shipper.start()
        for i in range(5):
            shipper.ship(level="INFO", event=f"EVENT_{i}")
        shipper.stop()
        assert mock_post.call_count == 5


def test_overflow_drops_oldest_keeps_newest():
    with patch("trading.common.log_shipper.requests.post") as mock_post:
        mock_post.side_effect = Exception("network stalled")
        shipper = LogShipper(
            algo_name="x", server_name="y", api_base_url="http://x", api_key="k", max_queue_size=3
        )
        # worker not started -- exercise the queue logic directly
        for i in range(3):
            shipper.ship(level="INFO", event=f"EVENT_{i}")
        assert shipper._queue.qsize() == 3
        shipper.ship(level="INFO", event="EVENT_3")
        assert shipper._queue.qsize() == 3
        assert shipper._dropped_count == 1

        remaining = []
        while not shipper._queue.empty():
            remaining.append(shipper._queue.get_nowait()["event"])
        assert remaining == ["EVENT_1", "EVENT_2", "EVENT_3"]


def test_should_ship_semantics():
    assert should_ship("ENTRY", 20) is True
    assert should_ship("RANDOM", 20) is False
