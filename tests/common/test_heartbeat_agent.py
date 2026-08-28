"""ControlCenterHeartbeatAgent -- ported from
trading/common/test_heartbeat_local.py. requests.post is mocked."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from trading.common.heartbeat import ControlCenterHeartbeatAgent


def test_sends_heartbeat_with_correct_payload_and_headers():
    with patch("trading.common.heartbeat.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=True, status_code=200)
        agent = ControlCenterHeartbeatAgent(
            algo_name="example_strategy", server_name="ec2-1",
            api_base_url="http://127.0.0.1:9999", api_key="test-key", interval_seconds=1,
        )
        agent.update_metrics(status="RUNNING", pnl=500.0, position="LONG")
        agent.start()
        time.sleep(1.5)
        agent.stop()

        assert mock_post.call_count >= 1
        call = mock_post.call_args_list[0]
        url = call.args[0] if call.args else call.kwargs.get("url")
        payload = call.kwargs.get("json")
        assert url == "http://127.0.0.1:9999/api/heartbeat"
        assert payload["algo_id"] == "example_strategy"
        assert payload["server_id"] == "ec2-1"
        assert payload["status"] == "RUNNING"
        assert payload["pnl"] == 500.0
        assert payload["position"] == "LONG"
        assert "cpu" in payload and "memory" in payload
        assert call.kwargs.get("headers") == {"X-API-Key": "test-key"}


def test_retries_then_gives_up_without_raising():
    with patch("trading.common.heartbeat.requests.post") as mock_post, \
         patch("trading.common.heartbeat.time.sleep"):
        mock_post.return_value = MagicMock(ok=False, status_code=500, text="server error")
        agent = ControlCenterHeartbeatAgent(
            algo_name="x", server_name="y", api_base_url="http://x", api_key="k",
            interval_seconds=100, max_retries=3,
        )
        result = agent._send_with_retry(agent._build_payload())
        assert result is False
        assert mock_post.call_count == 3


def test_4xx_does_not_retry():
    with patch("trading.common.heartbeat.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=False, status_code=401, text="unauthorized")
        agent = ControlCenterHeartbeatAgent(
            algo_name="x", server_name="y", api_base_url="http://x", api_key="bad-key",
            interval_seconds=100, max_retries=3,
        )
        result = agent._send_with_retry(agent._build_payload())
        assert result is False
        assert mock_post.call_count == 1
