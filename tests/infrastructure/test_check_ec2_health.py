"""check_ec2_health Lambda action -- ported from
trading/infrastructure/lambda/test_check_ec2_health_local.py."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

_PING = datetime(2026, 8, 13, 0, 0, 0, tzinfo=timezone.utc)
_PING_ISO = _PING.isoformat()
_PING_EARLIER = datetime(2026, 8, 12, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def health_orch(orch):
    orch.ec2_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
    }
    orch.ssm_client.describe_instance_information.return_value = {
        "InstanceInformationList": [{"PingStatus": "Online", "LastPingDateTime": _PING}]
    }
    return orch


def test_fully_healthy(health_orch):
    r = health_orch.lambda_handler({"action": "check_ec2_health"}, None)
    assert r == {
        "success": True, "server_id": "i-test123", "ec2_status": "RUNNING",
        "ssm_status": "ONLINE", "last_ping": _PING_ISO, "healthy": True,
    }


def test_ec2_running_ssm_not_registered(health_orch):
    health_orch.ssm_client.describe_instance_information.return_value = {"InstanceInformationList": []}
    r = health_orch.lambda_handler({"action": "check_ec2_health"}, None)
    assert r["success"] is True
    assert r["ec2_status"] == "RUNNING"
    assert r["ssm_status"] == "NOT_REGISTERED"
    assert r["healthy"] is False


def test_ssm_connection_lost(health_orch):
    health_orch.ssm_client.describe_instance_information.return_value = {
        "InstanceInformationList": [{"PingStatus": "ConnectionLost", "LastPingDateTime": _PING_EARLIER}]
    }
    r = health_orch.lambda_handler({"action": "check_ec2_health"}, None)
    assert r["ssm_status"] == "CONNECTIONLOST" and r["healthy"] is False


def test_ec2_stopped_is_unhealthy(health_orch):
    health_orch.ec2_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]
    }
    r = health_orch.lambda_handler({"action": "check_ec2_health"}, None)
    assert r["ec2_status"] == "STOPPED" and r["healthy"] is False


def test_describe_instances_failure_is_not_a_crash(health_orch):
    health_orch.ec2_client.describe_instances.side_effect = ClientError(
        {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "not found"}}, "DescribeInstances",
    )
    r = health_orch.lambda_handler({"action": "check_ec2_health"}, None)
    assert r["success"] is False
