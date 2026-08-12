"""Local test for ControlCenterHeartbeatAgent -- mocks requests.post."""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trading.common.heartbeat import ControlCenterHeartbeatAgent  # noqa: E402

failures = []


def check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    print(f"[{status_}] {name} {detail}")
    if not condition:
        failures.append(name)


with patch("trading.common.heartbeat.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=True, status_code=200)

    agent = ControlCenterHeartbeatAgent(
        algo_name="example_strategy",
        server_name="ec2-1",
        api_base_url="http://127.0.0.1:9999",
        api_key="test-key",
        interval_seconds=1,
    )
    agent.update_metrics(status="RUNNING", pnl=500.0, position="LONG")
    agent.start()
    time.sleep(1.5)
    agent.stop()

    check("at least one heartbeat sent", mock_post.call_count >= 1, str(mock_post.call_count))

    call_args = mock_post.call_args_list[0]
    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
    payload = call_args.kwargs.get("json")
    headers = call_args.kwargs.get("headers")

    check("posted to correct endpoint", url == "http://127.0.0.1:9999/api/heartbeat", url)
    check(
        "payload has correct algo_id/server_id/status/pnl/position",
        payload["algo_id"] == "example_strategy" and payload["server_id"] == "ec2-1"
        and payload["status"] == "RUNNING" and payload["pnl"] == 500.0 and payload["position"] == "LONG",
        str(payload),
    )
    check("payload includes cpu/memory keys (psutil)", "cpu" in payload and "memory" in payload, str(payload))
    check("X-API-Key header sent", headers == {"X-API-Key": "test-key"}, str(headers))

# --- retry-then-give-up on repeated failure, does not raise ---
with patch("trading.common.heartbeat.requests.post") as mock_post, \
     patch("trading.common.heartbeat.time.sleep"):  # skip real backoff delays
    mock_post.return_value = MagicMock(ok=False, status_code=500, text="server error")

    agent2 = ControlCenterHeartbeatAgent(
        algo_name="x", server_name="y", api_base_url="http://x", api_key="k",
        interval_seconds=100, max_retries=3,
    )
    result = agent2._send_with_retry(agent2._build_payload())
    check("all retries exhausted -> returns False, no exception raised", result is False, str(result))
    check("retried max_retries times", mock_post.call_count == 3, mock_post.call_count)

# --- 4xx does not retry (client/config issue) ---
with patch("trading.common.heartbeat.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=False, status_code=401, text="unauthorized")
    agent3 = ControlCenterHeartbeatAgent(
        algo_name="x", server_name="y", api_base_url="http://x", api_key="bad-key",
        interval_seconds=100, max_retries=3,
    )
    result = agent3._send_with_retry(agent3._build_payload())
    check("4xx -> no retry, single attempt", mock_post.call_count == 1, mock_post.call_count)
    check("4xx -> returns False", result is False, str(result))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
