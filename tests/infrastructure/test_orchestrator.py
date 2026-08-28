"""orchestrator.py routing / validation / response-shaping.
Ported from trading/infrastructure/lambda/test_orchestrator_local.py.

Two stale expectations from the original are corrected (not weakened):
  * default os_name is now "linux" -> the START_ALGO shell command uses
    `venv/bin/python3 ...`; the PowerShell form is checked explicitly with
    os_name="windows".
  * *_all_algos now also fetches the server list, so _list_servers is
    mocked alongside _list_enabled_algos.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def test_missing_action(orch):
    assert orch.lambda_handler({}, None) == {"success": False, "error": "event.action is required"}


def test_unknown_action(orch):
    r = orch.lambda_handler({"action": "bogus"}, None)
    assert r["success"] is False and "unknown action" in r["error"]


def test_missing_instance_id(orch, monkeypatch):
    monkeypatch.delenv("INSTANCE_ID", raising=False)
    r = orch.lambda_handler({"action": "get_command_status", "job_id": "x"}, None)
    assert r["success"] is False


def test_start_ec2_already_running(orch):
    orch.ec2_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
    }
    r = orch.lambda_handler({"action": "start_ec2"}, None)
    assert r == {"success": True, "server_id": "i-test123", "status": "RUNNING"}
    assert not orch.ec2_client.start_instances.called


def test_start_ec2_stopped_starts_it(orch):
    orch.ec2_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]
    }
    r = orch.lambda_handler({"action": "start_ec2"}, None)
    assert r == {"success": True, "server_id": "i-test123", "status": "STARTING"}
    assert orch.ec2_client.start_instances.called


def test_start_algo_missing_algo_id(orch):
    assert orch.lambda_handler({"action": "start_algo"}, None)["success"] is False


def test_start_algo_happy_path_linux_command(orch):
    orch.ssm_client.send_command.return_value = {"Command": {"CommandId": "cmd-123"}}
    r = orch.lambda_handler({"action": "start_algo", "algo_id": "example_strategy"}, None)
    assert r == {
        "success": True, "job_id": "cmd-123", "server_id": "i-test123",
        "algo_id": "example_strategy", "status": "STARTING",
    }
    sent = orch.ssm_client.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert sent == (
        'cd "C:\\trading-app"; venv/bin/python3 trading/agent/trading_agent.py '
        'START_ALGO example_strategy'
    )


def test_start_algo_windows_command(orch):
    orch.ssm_client.send_command.return_value = {"Command": {"CommandId": "cmd-9"}}
    orch.lambda_handler({"action": "start_algo", "algo_id": "example_strategy", "os_name": "windows"}, None)
    sent = orch.ssm_client.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert sent == (
        'cd "C:\\trading-app"; python trading\\agent\\trading_agent.py START_ALGO example_strategy'
    )


def test_get_command_status_in_progress(orch):
    orch.ssm_client.get_command_invocation.return_value = {"Status": "InProgress"}
    r = orch.lambda_handler({"action": "get_command_status", "job_id": "cmd-123"}, None)
    assert r["status"] == "IN_PROGRESS"


def test_get_command_status_success_surfaces_agent_status(orch):
    agent_json = json.dumps({"algo": "example_strategy", "status": "RUNNING", "pid": 4242,
                             "started_at": "2026-01-01T00:00:00Z"})
    orch.ssm_client.get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": agent_json, "StandardErrorContent": "",
    }
    r = orch.lambda_handler({"action": "get_command_status", "job_id": "cmd-123"}, None)
    assert r["success"] is True and r["status"] == "RUNNING" and r["pid"] == 4242


def test_get_command_status_unparsable_output_is_unknown(orch):
    orch.ssm_client.get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": "not json", "StandardErrorContent": "warn",
    }
    r = orch.lambda_handler({"action": "get_command_status", "job_id": "cmd-123"}, None)
    assert r["success"] is False and r["status"] == "UNKNOWN"


def test_get_algo_status_resolves_via_short_poll(orch):
    agent_json = json.dumps({"algo": "example_strategy", "status": "RUNNING", "pid": 4242,
                             "started_at": "2026-01-01T00:00:00Z"})
    orch.ssm_client.get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": agent_json, "StandardErrorContent": "",
    }
    r = orch.lambda_handler({"action": "get_algo_status", "algo_id": "example_strategy"}, None)
    assert r["status"] == "RUNNING" and r["pid"] == 4242


def test_all_algos_missing_env(orch):
    assert orch.lambda_handler({"action": "start_all_algos"}, None)["success"] is False


@pytest.fixture
def all_algos_env(orch, monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://api.example.com")
    monkeypatch.setenv("CONTROL_API_KEY", "test-api-key")
    servers = {"ec2-1": {"server_id": "ec2-1", "ec2_instance_id": "i-1",
                         "repo_path": "/trading-app", "os": "linux"}}
    monkeypatch.setattr(orch, "_list_servers", MagicMock(return_value=servers))
    return orch


def test_all_algos_none_enabled(all_algos_env):
    all_algos_env._list_enabled_algos = MagicMock(return_value=[])
    r = all_algos_env.lambda_handler({"action": "start_all_algos"}, None)
    assert r == {"success": True, "results": [], "message": "no enabled algos found"}


def test_all_algos_two_enabled(all_algos_env):
    all_algos_env._list_enabled_algos = MagicMock(return_value=[
        {"algo_id": "algo_a", "server_id": "ec2-1", "enabled": True},
        {"algo_id": "algo_b", "server_id": "ec2-1", "enabled": True},
    ])
    all_algos_env.ssm_client = MagicMock()
    all_algos_env.ssm_client.send_command.side_effect = [
        {"Command": {"CommandId": "cmd-a"}}, {"Command": {"CommandId": "cmd-b"}},
    ]
    r = all_algos_env.lambda_handler({"action": "start_all_algos"}, None)
    assert r["success"] is True and len(r["results"]) == 2
    assert {x["algo_id"] for x in r["results"]} == {"algo_a", "algo_b"}
    assert {x["job_id"] for x in r["results"]} == {"cmd-a", "cmd-b"}
    assert all_algos_env.ssm_client.send_command.call_count == 2


def test_stop_all_algos_sends_stop(all_algos_env):
    all_algos_env._list_enabled_algos = MagicMock(return_value=[
        {"algo_id": "algo_a", "server_id": "ec2-1", "enabled": True},
    ])
    all_algos_env.ssm_client = MagicMock()
    all_algos_env.ssm_client.send_command.return_value = {"Command": {"CommandId": "cmd-x"}}
    all_algos_env.lambda_handler({"action": "stop_all_algos"}, None)
    sent = all_algos_env.ssm_client.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert "STOP_ALGO" in sent


def test_all_algos_partial_failure(all_algos_env):
    all_algos_env._list_enabled_algos = MagicMock(return_value=[
        {"algo_id": "algo_a", "server_id": "ec2-1", "enabled": True},
        {"algo_id": "algo_b", "server_id": "ec2-1", "enabled": True},
    ])

    def _side_effect(*a, **kw):
        cmd = kw["Parameters"]["commands"][0]
        if "algo_a" in cmd:
            raise all_algos_env.ClientError(
                {"Error": {"Code": "Throttling", "Message": "rate limited"}}, "SendCommand"
            )
        return {"Command": {"CommandId": "cmd-b"}}

    all_algos_env.ssm_client = MagicMock()
    all_algos_env.ssm_client.send_command.side_effect = _side_effect
    r = all_algos_env.lambda_handler({"action": "start_all_algos"}, None)
    assert r["success"] is False
    assert len(r["results"]) == 2
    assert any(x["algo_id"] == "algo_a" and x["success"] is False for x in r["results"])
    assert any(x["algo_id"] == "algo_b" and x["success"] is True for x in r["results"])


def test_list_enabled_algos_real_filtering(orch):
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("orchestrator.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _FakeResponse([
            {"algo_id": "enabled_one", "enabled": True},
            {"algo_id": "disabled_one", "enabled": False},
        ])
        algos = orch._list_enabled_algos("http://api.example.com", "key")
        assert algos == [{"algo_id": "enabled_one", "enabled": True}]
        req = mock_urlopen.call_args.args[0]
        assert req.full_url == "http://api.example.com/api/algos"
        assert req.headers.get("X-api-key") == "key"
