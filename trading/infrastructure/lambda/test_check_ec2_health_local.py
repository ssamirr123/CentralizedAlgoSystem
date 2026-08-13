"""Local test for the check_ec2_health Lambda action. Not permanent."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["INSTANCE_ID"] = "i-test123"

import orchestrator  # noqa: E402

failures = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        failures.append(name)


# --- fully healthy: EC2 running, SSM online ---
orchestrator.ec2_client = MagicMock()
orchestrator.ec2_client.describe_instances.return_value = {
    "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
}
orchestrator.ssm_client = MagicMock()
orchestrator.ssm_client.describe_instance_information.return_value = {
    "InstanceInformationList": [{"PingStatus": "Online", "LastPingDateTime": "2026-08-13T00:00:00Z"}]
}
result = orchestrator.lambda_handler({"action": "check_ec2_health"}, None)
check(
    "fully healthy -> healthy:True",
    result == {
        "success": True, "server_id": "i-test123", "ec2_status": "RUNNING",
        "ssm_status": "ONLINE", "last_ping": "2026-08-13T00:00:00Z", "healthy": True,
    },
    str(result),
)

# --- EC2 running but SSM agent dead/unregistered -- the exact gap this action exists to catch ---
orchestrator.ssm_client.describe_instance_information.return_value = {"InstanceInformationList": []}
result = orchestrator.lambda_handler({"action": "check_ec2_health"}, None)
check(
    "EC2 running but SSM not registered -> healthy:False (this is the whole point)",
    result["success"] is True and result["ec2_status"] == "RUNNING"
    and result["ssm_status"] == "NOT_REGISTERED" and result["healthy"] is False,
    str(result),
)

# --- EC2 running but SSM agent registered, offline (was online before, went dark) ---
orchestrator.ssm_client.describe_instance_information.return_value = {
    "InstanceInformationList": [{"PingStatus": "ConnectionLost", "LastPingDateTime": "2026-08-12T00:00:00Z"}]
}
result = orchestrator.lambda_handler({"action": "check_ec2_health"}, None)
check(
    "SSM ConnectionLost -> healthy:False",
    result["ssm_status"] == "CONNECTIONLOST" and result["healthy"] is False,
    str(result),
)

# --- EC2 stopped -> healthy:False regardless of SSM (can't be healthy if not even running) ---
orchestrator.ec2_client.describe_instances.return_value = {
    "Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]
}
orchestrator.ssm_client.describe_instance_information.return_value = {
    "InstanceInformationList": [{"PingStatus": "Online", "LastPingDateTime": "2026-08-13T00:00:00Z"}]
}
result = orchestrator.lambda_handler({"action": "check_ec2_health"}, None)
check(
    "EC2 stopped -> healthy:False even if SSM shows stale Online from before shutdown",
    result["ec2_status"] == "STOPPED" and result["healthy"] is False,
    str(result),
)

# --- describe_instances itself fails ---
from botocore.exceptions import ClientError  # noqa: E402

orchestrator.ec2_client.describe_instances.side_effect = ClientError(
    {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "not found"}}, "DescribeInstances",
)
result = orchestrator.lambda_handler({"action": "check_ec2_health"}, None)
check("describe_instances failure -> success:False, not a crash", result["success"] is False, str(result))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
