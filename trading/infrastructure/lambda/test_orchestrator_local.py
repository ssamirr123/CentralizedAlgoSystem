"""
Local test harness for orchestrator.py -- mocks boto3 SSM/EC2 clients so
we can verify the Lambda's routing/validation/response-shaping logic
without real AWS access. Not a permanent part of the deployment; delete
or move to a real test suite later.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["INSTANCE_ID"] = "i-test123"
os.environ["REPO_PATH"] = r"C:\trading-app"

import orchestrator  # noqa: E402

# Captured before any test below reassigns orchestrator._list_enabled_algos
# to a MagicMock -- needed later to test the REAL filtering logic, not the
# stubbed-out version other tests substitute for it.
_real_list_enabled_algos = orchestrator._list_enabled_algos

failures = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        failures.append(name)


# --- missing action ---
result = orchestrator.lambda_handler({}, None)
check("missing action -> error", result == {"success": False, "error": "event.action is required"}, str(result))

# --- unknown action ---
result = orchestrator.lambda_handler({"action": "bogus"}, None)
check("unknown action -> error", result["success"] is False and "unknown action" in result["error"], str(result))

# --- missing INSTANCE_ID ---
del os.environ["INSTANCE_ID"]
result = orchestrator.lambda_handler({"action": "get_command_status", "job_id": "x"}, None)
check("missing INSTANCE_ID -> error", result["success"] is False, str(result))
os.environ["INSTANCE_ID"] = "i-test123"

# --- start_ec2: already running ---
orchestrator.ec2_client = MagicMock()
orchestrator.ec2_client.describe_instances.return_value = {
    "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
}
result = orchestrator.lambda_handler({"action": "start_ec2"}, None)
check(
    "start_ec2 already running -> success, no start_instances call",
    result == {"success": True, "server_id": "i-test123", "status": "RUNNING"}
    and not orchestrator.ec2_client.start_instances.called,
    str(result),
)

# --- start_ec2: stopped -> starts it ---
orchestrator.ec2_client.describe_instances.return_value = {
    "Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]
}
result = orchestrator.lambda_handler({"action": "start_ec2"}, None)
check(
    "start_ec2 stopped -> calls start_instances, returns STARTING",
    result == {"success": True, "server_id": "i-test123", "status": "STARTING"}
    and orchestrator.ec2_client.start_instances.called,
    str(result),
)

# --- start_algo: missing algo_id ---
result = orchestrator.lambda_handler({"action": "start_algo"}, None)
check("start_algo missing algo_id -> error", result["success"] is False, str(result))

# --- start_algo: happy path ---
orchestrator.ssm_client = MagicMock()
orchestrator.ssm_client.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
result = orchestrator.lambda_handler({"action": "start_algo", "algo_id": "example_strategy"}, None)
check(
    "start_algo -> async job_id + STARTING",
    result == {
        "success": True,
        "job_id": "cmd-123",
        "server_id": "i-test123",
        "algo_id": "example_strategy",
        "status": "STARTING",
    },
    str(result),
)
sent_command = orchestrator.ssm_client.send_command.call_args.kwargs["Parameters"]["commands"][0]
check(
    "start_algo builds correct PowerShell command",
    sent_command == 'cd "C:\\trading-app"; python trading\\agent\\trading_agent.py START_ALGO example_strategy',
    sent_command,
)

# --- get_command_status: still running ---
orchestrator.ssm_client.get_command_invocation.return_value = {"Status": "InProgress"}
result = orchestrator.lambda_handler({"action": "get_command_status", "job_id": "cmd-123"}, None)
check("get_command_status in progress", result["status"] == "IN_PROGRESS", str(result))

# --- get_command_status: success with valid agent JSON ---
agent_json = json.dumps({"algo": "example_strategy", "status": "RUNNING", "pid": 4242, "started_at": "2026-01-01T00:00:00Z"})
orchestrator.ssm_client.get_command_invocation.return_value = {
    "Status": "Success",
    "StandardOutputContent": agent_json,
    "StandardErrorContent": "",
}
result = orchestrator.lambda_handler({"action": "get_command_status", "job_id": "cmd-123"}, None)
check(
    "get_command_status success -> real agent status surfaced, not just SSM success",
    result["success"] is True and result["status"] == "RUNNING" and result["pid"] == 4242,
    str(result),
)

# --- get_command_status: SSM succeeded but agent output unparsable ---
orchestrator.ssm_client.get_command_invocation.return_value = {
    "Status": "Success",
    "StandardOutputContent": "not json",
    "StandardErrorContent": "some warning",
}
result = orchestrator.lambda_handler({"action": "get_command_status", "job_id": "cmd-123"}, None)
check(
    "get_command_status unparsable output -> UNKNOWN, not falsely Success",
    result["success"] is False and result["status"] == "UNKNOWN",
    str(result),
)

# --- get_algo_status: resolves within poll window ---
orchestrator.ssm_client.get_command_invocation.return_value = {
    "Status": "Success",
    "StandardOutputContent": agent_json,
    "StandardErrorContent": "",
}
result = orchestrator.lambda_handler({"action": "get_algo_status", "algo_id": "example_strategy"}, None)
check(
    "get_algo_status resolves via short poll",
    result["status"] == "RUNNING" and result["pid"] == 4242,
    str(result),
)

# --- *_all_algos: missing API_BASE_URL/CONTROL_API_KEY -> error ---
result = orchestrator.lambda_handler({"action": "start_all_algos"}, None)
check("start_all_algos missing env vars -> error", result["success"] is False, str(result))

os.environ["API_BASE_URL"] = "http://api.example.com"
os.environ["CONTROL_API_KEY"] = "test-api-key"

# --- *_all_algos: no enabled algos ---
orchestrator._list_enabled_algos = MagicMock(return_value=[])
result = orchestrator.lambda_handler({"action": "start_all_algos"}, None)
check(
    "start_all_algos no enabled algos -> success, empty results",
    result == {"success": True, "results": [], "message": "no enabled algos found"},
    str(result),
)

# --- *_all_algos: two enabled algos, one disabled (filtered by _list_enabled_algos itself) ---
orchestrator._list_enabled_algos = MagicMock(return_value=[
    {"algo_id": "algo_a", "server_id": "ec2-1", "enabled": True},
    {"algo_id": "algo_b", "server_id": "ec2-1", "enabled": True},
])
orchestrator.ssm_client = MagicMock()
orchestrator.ssm_client.send_command.side_effect = [
    {"Command": {"CommandId": "cmd-a"}},
    {"Command": {"CommandId": "cmd-b"}},
]
result = orchestrator.lambda_handler({"action": "start_all_algos"}, None)
check("start_all_algos -> success, 2 results", result["success"] is True and len(result["results"]) == 2, str(result))
check(
    "start_all_algos -> each result has correct algo_id/job_id",
    {r["algo_id"] for r in result["results"]} == {"algo_a", "algo_b"}
    and {r["job_id"] for r in result["results"]} == {"cmd-a", "cmd-b"},
    str(result),
)
check("start_all_algos -> sent one SSM command per algo", orchestrator.ssm_client.send_command.call_count == 2)

# --- *_all_algos: correct single-action mapping (stop_all_algos -> STOP_ALGO) ---
orchestrator.ssm_client.send_command.side_effect = None
orchestrator.ssm_client.send_command.return_value = {"Command": {"CommandId": "cmd-x"}}
orchestrator.lambda_handler({"action": "stop_all_algos"}, None)
sent = orchestrator.ssm_client.send_command.call_args.kwargs["Parameters"]["commands"][0]
check("stop_all_algos sends STOP_ALGO (not START_ALGO)", "STOP_ALGO" in sent, sent)

# --- *_all_algos: one algo's SSM send fails -> overall success=False, other still attempted ---
def _side_effect(*args, **kwargs):
    commands = kwargs["Parameters"]["commands"][0]
    if "algo_a" in commands:
        raise orchestrator.ClientError({"Error": {"Code": "Throttling", "Message": "rate limited"}}, "SendCommand")
    return {"Command": {"CommandId": "cmd-b"}}

orchestrator._list_enabled_algos = MagicMock(return_value=[
    {"algo_id": "algo_a", "server_id": "ec2-1", "enabled": True},
    {"algo_id": "algo_b", "server_id": "ec2-1", "enabled": True},
])
orchestrator.ssm_client.send_command.side_effect = _side_effect
result = orchestrator.lambda_handler({"action": "start_all_algos"}, None)
check("partial failure -> overall success False", result["success"] is False, str(result))
check(
    "partial failure -> failed algo has success:False, other still succeeded",
    len(result["results"]) == 2
    and any(r["algo_id"] == "algo_a" and r["success"] is False for r in result["results"])
    and any(r["algo_id"] == "algo_b" and r["success"] is True for r in result["results"]),
    str(result),
)

# --- _list_enabled_algos: real filtering logic (not mocked) against a fake urlopen ---
class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


import unittest.mock as mock  # noqa: E402

with mock.patch("orchestrator.urllib.request.urlopen") as mock_urlopen:
    mock_urlopen.return_value = _FakeResponse([
        {"algo_id": "enabled_one", "enabled": True},
        {"algo_id": "disabled_one", "enabled": False},
    ])
    algos = _real_list_enabled_algos("http://api.example.com", "key")
    check(
        "_list_enabled_algos filters out disabled algos (real logic, not mocked)",
        algos == [{"algo_id": "enabled_one", "enabled": True}],
        str(algos),
    )
    request_sent = mock_urlopen.call_args.args[0]
    check(
        "_list_enabled_algos calls the correct URL with X-API-Key header",
        request_sent.full_url == "http://api.example.com/api/algos" and request_sent.headers.get("X-api-key") == "key",
        f"url={request_sent.full_url} headers={dict(request_sent.headers)}",
    )

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
