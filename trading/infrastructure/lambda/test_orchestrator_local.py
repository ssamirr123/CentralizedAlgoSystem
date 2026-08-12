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

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
