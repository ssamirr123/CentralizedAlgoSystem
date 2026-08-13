"""Local test for LogShipper -- mocks requests.post."""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trading.common.log_shipper import LogShipper, should_ship  # noqa: E402

failures = []


def check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    print(f"[{status_}] {name} {detail}")
    if not condition:
        failures.append(name)


# --- normal send ---
with patch("trading.common.log_shipper.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=True, status_code=200)

    shipper = LogShipper(algo_name="x", server_name="y", api_base_url="http://127.0.0.1:9999", api_key="k")
    shipper.start()
    shipper.ship(level="INFO", event="ENTRY", details={"strike": 24750})
    time.sleep(0.5)
    shipper.stop()

    check("event was posted", mock_post.call_count == 1, mock_post.call_count)
    call = mock_post.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    payload = call.kwargs.get("json")
    check("posted to /api/logs", url == "http://127.0.0.1:9999/api/logs", url)
    check(
        "payload correct",
        payload["algo_id"] == "x" and payload["server_id"] == "y" and payload["event"] == "ENTRY"
        and payload["details"] == {"strike": 24750},
        str(payload),
    )

# --- stop() drains queued items before returning ---
with patch("trading.common.log_shipper.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=True, status_code=200)
    shipper2 = LogShipper(algo_name="x", server_name="y", api_base_url="http://x", api_key="k")
    shipper2.start()
    for i in range(5):
        shipper2.ship(level="INFO", event=f"EVENT_{i}")
    shipper2.stop()  # should drain all 5 before returning
    check("stop() drains all queued items", mock_post.call_count == 5, mock_post.call_count)

# --- overflow drops OLDEST, keeps newest ---
with patch("trading.common.log_shipper.requests.post") as mock_post:
    # never resolve the send -- simulate a stalled network so nothing drains
    # while we fill the queue past its max size
    mock_post.side_effect = Exception("network stalled")
    shipper3 = LogShipper(
        algo_name="x", server_name="y", api_base_url="http://x", api_key="k", max_queue_size=3,
    )
    # Don't start the worker thread -- test the queue logic directly, in isolation
    shipper3.ship(level="INFO", event="EVENT_0")
    shipper3.ship(level="INFO", event="EVENT_1")
    shipper3.ship(level="INFO", event="EVENT_2")
    check("queue at max size", shipper3._queue.qsize() == 3, shipper3._queue.qsize())
    shipper3.ship(level="INFO", event="EVENT_3")  # should drop EVENT_0, keep queue at 3
    check("queue still at max size after overflow", shipper3._queue.qsize() == 3, shipper3._queue.qsize())
    check("dropped count incremented", shipper3._dropped_count == 1, shipper3._dropped_count)

    remaining_events = []
    while not shipper3._queue.empty():
        remaining_events.append(shipper3._queue.get_nowait()["event"])
    check(
        "oldest (EVENT_0) dropped, newest 3 retained",
        remaining_events == ["EVENT_1", "EVENT_2", "EVENT_3"],
        str(remaining_events),
    )

# --- should_ship still importable/consistent from this module ---
check("should_ship('ENTRY', 20) True", should_ship("ENTRY", 20) is True)
check("should_ship('RANDOM', 20) False", should_ship("RANDOM", 20) is False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
