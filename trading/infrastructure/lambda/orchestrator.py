"""
Trading Control Center — orchestration Lambda.

Single well-designed function routed by event["action"], rather than one
Lambda per action (per the project's own guidance to avoid unnecessary
Lambda duplication when a single function stays clean).

Actions:
    start_ec2, stop_ec2                              -- EC2 power state
    start_algo, stop_algo, restart_algo, update_algo  -- async, via SSM
    get_command_status                                -- poll a job_id from the above
    get_algo_status                                   -- convenience: STATUS + short bounded wait

IMPORTANT — matches this project's own safety principle: algo-control
actions do NOT wait for the algo to actually be running before returning.
They send the SSM command and return immediately with a job_id and
status "STARTING"/"STOPPING"/etc. The caller must poll get_command_status
to learn the REAL outcome (which itself reflects trading_agent.py's own
process-liveness verification, not "the SSM command was accepted"). A
slow remote operation (we've seen a plain git clone hang for minutes on a
credential prompt) would otherwise either blow the Lambda timeout or hold
an expensive invocation open pointlessly.

Configuration via Lambda environment variables:
    INSTANCE_ID   -- target EC2 instance ID (required)
    REPO_PATH     -- path to the repo on the instance, e.g. C:\\trading-app (required for algo actions)
    AWS_REGION    -- set automatically by the Lambda runtime; not user-configured
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm_client = boto3.client("ssm")
ec2_client = boto3.client("ec2")

ALGO_ACTIONS = {
    "start_algo": "START_ALGO",
    "stop_algo": "STOP_ALGO",
    "restart_algo": "RESTART_ALGO",
    "update_algo": "UPDATE",
}

# get_algo_status polls briefly since STATUS is a fast local check on the
# instance (no network calls, no download) — unlike start/stop/update,
# which can legitimately take a while and should stay fully async.
STATUS_POLL_TIMEOUT_SECONDS = 10
STATUS_POLL_INTERVAL_SECONDS = 1


def _log_event(event_name: str, **details: Any) -> None:
    logger.info(json.dumps({"event": event_name, **details}, default=str))


def _error(message: str, **extra: Any) -> dict:
    return {"success": False, "error": message, **extra}


def _get_env(name: str) -> str | None:
    return os.environ.get(name)


def _build_algo_powershell(repo_path: str, agent_command: str, algo_name: str, lines: int | None) -> str:
    parts = [f'cd "{repo_path}";', "python trading\\agent\\trading_agent.py", agent_command, algo_name]
    if agent_command == "LOGS" and lines is not None:
        parts.append(f"--lines {lines}")
    return " ".join(parts)


def _send_ssm_command(instance_id: str, powershell_command: str) -> str:
    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunPowerShellScript",
        Parameters={"commands": [powershell_command]},
    )
    return response["Command"]["CommandId"]


def _get_command_result(command_id: str, instance_id: str) -> dict:
    try:
        result = ssm_client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except ClientError as exc:
        # InvocationDoesNotExist is expected immediately after send_command
        # while SSM is still registering it — not a real error, just "not
        # ready yet" from the caller's perspective.
        if exc.response.get("Error", {}).get("Code") == "InvocationDoesNotExist":
            return {"Status": "Pending"}
        raise
    return result


def _parse_agent_output(stdout: str) -> dict | None:
    """trading_agent.py prints one JSON object to stdout. Best-effort parse
    — if it's not valid JSON (crashed before printing, unexpected output),
    return None and let the caller fall back to raw SSM status."""
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_start_ec2(instance_id: str, event: dict) -> dict:
    try:
        current = ec2_client.describe_instances(InstanceIds=[instance_id])
        state = current["Reservations"][0]["Instances"][0]["State"]["Name"]
    except (ClientError, IndexError, KeyError) as exc:
        return _error(f"could not describe instance: {exc}", server_id=instance_id)

    if state in ("running", "pending"):
        _log_event("EC2_ALREADY_RUNNING", instance_id=instance_id, state=state)
        return {"success": True, "server_id": instance_id, "status": state.upper()}

    try:
        ec2_client.start_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        return _error(f"start_instances failed: {exc}", server_id=instance_id)

    _log_event("EC2_START_REQUESTED", instance_id=instance_id)
    return {"success": True, "server_id": instance_id, "status": "STARTING"}


def _action_stop_ec2(instance_id: str, event: dict) -> dict:
    try:
        current = ec2_client.describe_instances(InstanceIds=[instance_id])
        state = current["Reservations"][0]["Instances"][0]["State"]["Name"]
    except (ClientError, IndexError, KeyError) as exc:
        return _error(f"could not describe instance: {exc}", server_id=instance_id)

    if state in ("stopped", "stopping"):
        _log_event("EC2_ALREADY_STOPPED", instance_id=instance_id, state=state)
        return {"success": True, "server_id": instance_id, "status": state.upper()}

    try:
        ec2_client.stop_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        return _error(f"stop_instances failed: {exc}", server_id=instance_id)

    _log_event("EC2_STOP_REQUESTED", instance_id=instance_id)
    return {"success": True, "server_id": instance_id, "status": "STOPPING"}


def _action_algo_command(action: str, instance_id: str, repo_path: str, event: dict) -> dict:
    algo_name = event.get("algo_id") or event.get("algo_name")
    if not algo_name:
        return _error("algo_id (or algo_name) is required for this action")

    agent_command = ALGO_ACTIONS[action]
    lines = event.get("lines")
    powershell_command = _build_algo_powershell(repo_path, agent_command, algo_name, lines)

    try:
        command_id = _send_ssm_command(instance_id, powershell_command)
    except (ClientError, BotoCoreError) as exc:
        return _error(f"send_command failed: {exc}", server_id=instance_id, algo_id=algo_name)

    pending_status = {
        "START_ALGO": "STARTING",
        "STOP_ALGO": "STOPPING",
        "RESTART_ALGO": "RESTARTING",
        "UPDATE": "UPDATING",
    }[agent_command]

    _log_event("ALGO_COMMAND_SENT", action=action, algo_id=algo_name, server_id=instance_id, job_id=command_id)

    return {
        "success": True,
        "job_id": command_id,
        "server_id": instance_id,
        "algo_id": algo_name,
        "status": pending_status,
    }


def _action_get_command_status(instance_id: str, event: dict) -> dict:
    command_id = event.get("job_id") or event.get("command_id")
    if not command_id:
        return _error("job_id (or command_id) is required for this action")

    try:
        result = _get_command_result(command_id, instance_id)
    except (ClientError, BotoCoreError) as exc:
        return _error(f"get_command_invocation failed: {exc}", job_id=command_id)

    ssm_status = result.get("Status", "Pending")
    stdout = result.get("StandardOutputContent", "")
    stderr = result.get("StandardErrorContent", "")

    if ssm_status not in ("Success", "Failed", "Cancelled", "TimedOut"):
        # Still running on the instance — no real result to report yet.
        return {"success": True, "job_id": command_id, "ssm_status": ssm_status, "status": "IN_PROGRESS"}

    agent_result = _parse_agent_output(stdout)
    if agent_result is not None:
        # This is the real, verified outcome from trading_agent.py itself
        # (process-liveness checked), not just "the SSM command finished."
        return {
            "success": ssm_status == "Success",
            "job_id": command_id,
            "ssm_status": ssm_status,
            **agent_result,
        }

    # SSM finished but we couldn't parse agent output (crash before it
    # printed anything, unexpected stderr, etc.) — report what we have
    # rather than claiming a status we can't actually verify.
    return {
        "success": False,
        "job_id": command_id,
        "ssm_status": ssm_status,
        "status": "UNKNOWN",
        "stdout": stdout,
        "stderr": stderr,
    }


def _action_get_algo_status(instance_id: str, repo_path: str, event: dict) -> dict:
    algo_name = event.get("algo_id") or event.get("algo_name")
    if not algo_name:
        return _error("algo_id (or algo_name) is required for this action")

    powershell_command = _build_algo_powershell(repo_path, "STATUS", algo_name, None)
    try:
        command_id = _send_ssm_command(instance_id, powershell_command)
    except (ClientError, BotoCoreError) as exc:
        return _error(f"send_command failed: {exc}", server_id=instance_id, algo_id=algo_name)

    deadline = time.monotonic() + STATUS_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = _action_get_command_status(instance_id, {"job_id": command_id})
        if result.get("status") != "IN_PROGRESS":
            return result
        time.sleep(STATUS_POLL_INTERVAL_SECONDS)

    # Didn't finish within the short bounded wait — hand back the job_id
    # so the caller can poll get_command_status instead of blocking longer.
    _log_event("STATUS_POLL_TIMED_OUT", algo_id=algo_name, job_id=command_id)
    return {
        "success": True,
        "job_id": command_id,
        "server_id": instance_id,
        "algo_id": algo_name,
        "status": "PENDING",
        "message": "status check still running — poll get_command_status with this job_id",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    action = event.get("action")
    _log_event("LAMBDA_INVOKED", action=action)

    if not action:
        return _error("event.action is required")

    instance_id = _get_env("INSTANCE_ID")
    if not instance_id:
        return _error("INSTANCE_ID environment variable is not set")

    if action == "start_ec2":
        result = _action_start_ec2(instance_id, event)
    elif action == "stop_ec2":
        result = _action_stop_ec2(instance_id, event)
    elif action in ALGO_ACTIONS:
        repo_path = _get_env("REPO_PATH")
        if not repo_path:
            return _error("REPO_PATH environment variable is not set")
        result = _action_algo_command(action, instance_id, repo_path, event)
    elif action == "get_command_status":
        result = _action_get_command_status(instance_id, event)
    elif action == "get_algo_status":
        repo_path = _get_env("REPO_PATH")
        if not repo_path:
            return _error("REPO_PATH environment variable is not set")
        result = _action_get_algo_status(instance_id, repo_path, event)
    else:
        result = _error(f"unknown action: {action}")

    _log_event("LAMBDA_RESULT", action=action, result=result)
    return result
