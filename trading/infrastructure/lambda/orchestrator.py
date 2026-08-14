"""
Trading Control Center — orchestration Lambda.

Single well-designed function routed by event["action"], rather than one
Lambda per action (per the project's own guidance to avoid unnecessary
Lambda duplication when a single function stays clean).

Actions:
    start_ec2, stop_ec2                              -- EC2 power state
    check_ec2_health                                  -- Milestone 12: EC2 power state AND SSM Agent
                                                          responsiveness, which start_ec2/stop_ec2 alone
                                                          never distinguish (an instance can be power-
                                                          state RUNNING with a dead/unregistered SSM Agent)
    start_algo, stop_algo, restart_algo, update_algo  -- async, via SSM
    start_all_algos, stop_all_algos, update_all_algos -- Milestone 11: same as above, for every
                                                          enabled algo (for EventBridge Scheduler --
                                                          one static payload per schedule, not one
                                                          rule per algo)
    get_command_status                                -- poll a job_id from the above
    get_algo_status                                   -- convenience: STATUS + short bounded wait
    sync_repo                                         -- `git pull --ff-only` in REPO_PATH, short bounded
                                                          wait (like get_algo_status, not fire-and-forget --
                                                          called synchronously right after a new strategy is
                                                          registered via POST /api/algos, so the instance
                                                          actually has the code for whatever algo_name was
                                                          just added; a DB row alone puts no file on disk)

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
    INSTANCE_ID       -- target EC2 instance ID (required). Currently a single
                          hardcoded target -- this Lambda has no per-server
                          routing yet, so it always controls whichever one
                          instance this points at.
    REPO_PATH         -- path to the repo on the instance, e.g. /root/trading-app
                          (required for algo actions). Algo commands assume a
                          Linux target (AWS-RunShellScript, venv/bin/python3) --
                          see _build_algo_shell_command.
    AWS_REGION        -- set automatically by the Lambda runtime; not user-configured
    API_BASE_URL      -- the deployed control-center API (required for *_all_algos actions --
                          these need to know which algos exist, which only the API/DB knows)
    CONTROL_API_KEY   -- auth for the above (same key the dashboard uses)

*_all_algos actions call GET {API_BASE_URL}/api/algos via urllib (stdlib only
-- deliberately not the `requests` package, to keep deployment a single-file
zip with no dependency packaging step) rather than connecting to Supabase
directly. This Lambda's job stays "invoke SSM," not "know how to query
Postgres" -- that's already the API layer's responsibility (Milestone 6),
and duplicating it here would mean bundling SQLAlchemy/psycopg2 into a
Lambda package, which is real packaging complexity for no architectural
benefit when the API already exposes exactly this list.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
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

ALL_ALGOS_ACTIONS = {"start_all_algos", "stop_all_algos", "update_all_algos"}

# get_algo_status polls briefly since STATUS is a fast local check on the
# instance (no network calls, no download) — unlike start/stop/update,
# which can legitimately take a while and should stay fully async.
STATUS_POLL_TIMEOUT_SECONDS = 10
STATUS_POLL_INTERVAL_SECONDS = 1

# sync_repo is called synchronously from POST /api/algos (a Vercel
# serverless function with its own timeout, no maxDuration configured --
# likely a ~10-15s default), so this budget has to leave headroom for
# Lambda invoke overhead + the API's own DB round trip, not use the
# whole thing itself. A small git pull normally resolves in 1-3s anyway.
SYNC_REPO_POLL_TIMEOUT_SECONDS = 8
SYNC_REPO_POLL_INTERVAL_SECONDS = 1


def _log_event(event_name: str, **details: Any) -> None:
    logger.info(json.dumps({"event": event_name, **details}, default=str))


def _error(message: str, **extra: Any) -> dict:
    return {"success": False, "error": message, **extra}


def _get_env(name: str) -> str | None:
    return os.environ.get(name)


def _build_algo_shell_command(
    repo_path: str, agent_command: str, algo_name: str, lines: int | None,
    server_name: str | None = None, api_base_url: str | None = None, control_api_key: str | None = None,
) -> str:
    """Targets a Linux instance -- venv/bin/python3 (not bare python3),
    since AL2023's RPM-installed requests/urllib3 conflict with pip's in
    system site-packages (see install_deps_linux.sh), matching the
    identical pattern already used by ssm_invoke.py's manual CLI tool.

    Exports STRATEGY_NAME/SERVER_NAME/API_BASE_URL/CONTROL_API_KEY inline
    on START_ALGO/RESTART_ALGO -- each SSM send_command runs in a fresh,
    non-persistent shell session with no login-shell profile sourcing, so
    there's nowhere on the instance these could otherwise reliably live.
    Without them, main.py's heartbeat sender / log shipper silently
    no-op (config.control_api_key falsy -> CONTROL_CENTER_HEARTBEAT_SKIPPED)
    -- the process runs, but the control center never hears from it. The
    exports land in the trading_agent.py process's own environment, which
    Python's subprocess.Popen inherits into the detached strategy process
    by default (no explicit env= is passed), so this reaches the actual
    long-running child too, not just the short-lived launcher."""
    parts = []
    if agent_command in ("START_ALGO", "RESTART_ALGO") and server_name and api_base_url and control_api_key:
        parts.append(
            f'export STRATEGY_NAME="{algo_name}" SERVER_NAME="{server_name}" '
            f'API_BASE_URL="{api_base_url}" CONTROL_API_KEY="{control_api_key}";'
        )
    parts += [f'cd "{repo_path}";', "venv/bin/python3 trading/agent/trading_agent.py", agent_command, algo_name]
    if agent_command == "LOGS" and lines is not None:
        parts.append(f"--lines {lines}")
    return " ".join(parts)


def _send_ssm_command(instance_id: str, shell_command: str) -> str:
    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [shell_command]},
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

def _action_check_ec2_health(instance_id: str, event: dict) -> dict:
    """Milestone 12: distinguishes "EC2 power state" (describe_instances,
    already used by start_ec2/stop_ec2 above) from "is the SSM Agent on
    the instance actually responsive" (describe_instance_information) --
    an instance can be power-state RUNNING while SSM control is dead
    (agent crashed, network ACL change, IAM role detached), which the
    power-state check alone would never catch. This is exactly the gap
    that motivated this action: nothing before Milestone 12 distinguished
    these two failure modes."""
    try:
        power_state = ec2_client.describe_instances(InstanceIds=[instance_id])
        ec2_status = power_state["Reservations"][0]["Instances"][0]["State"]["Name"]
    except (ClientError, IndexError, KeyError) as exc:
        return _error(f"could not describe instance: {exc}", server_id=instance_id)

    try:
        ssm_info = ssm_client.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
        )
        instances = ssm_info.get("InstanceInformationList", [])
    except (ClientError, BotoCoreError) as exc:
        return _error(f"describe_instance_information failed: {exc}", server_id=instance_id, ec2_status=ec2_status)

    if not instances:
        ssm_status = "NOT_REGISTERED"
        last_ping = None
    else:
        ssm_status = instances[0].get("PingStatus", "UNKNOWN")
        last_ping_dt = instances[0].get("LastPingDateTime")
        last_ping = last_ping_dt.isoformat() if last_ping_dt else None

    healthy = ec2_status == "running" and ssm_status == "Online"
    return {
        "success": True,
        "server_id": instance_id,
        "ec2_status": ec2_status.upper(),
        "ssm_status": ssm_status.upper(),
        "last_ping": last_ping,
        "healthy": healthy,
    }


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
    shell_command = _build_algo_shell_command(
        repo_path, agent_command, algo_name, lines,
        server_name=_get_env("SERVER_NAME"), api_base_url=_get_env("API_BASE_URL"),
        control_api_key=_get_env("CONTROL_API_KEY"),
    )

    try:
        command_id = _send_ssm_command(instance_id, shell_command)
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


class AlgoListError(RuntimeError):
    pass


def _list_enabled_algos(api_base_url: str, api_key: str) -> list[dict]:
    request = urllib.request.Request(
        f"{api_base_url.rstrip('/')}/api/algos",
        headers={"X-API-Key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            algos = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise AlgoListError(f"could not fetch algo list from {api_base_url}/api/algos: {exc}") from exc

    return [a for a in algos if a.get("enabled")]


def _action_all_algos_command(action: str, instance_id: str, repo_path: str, event: dict) -> dict:
    """Same underlying SSM command as _action_algo_command, applied to
    every enabled algo instead of one named in the event -- for
    EventBridge Scheduler, which passes a single static payload per rule
    rather than one rule per algo."""
    api_base_url = _get_env("API_BASE_URL")
    api_key = _get_env("CONTROL_API_KEY")
    if not api_base_url or not api_key:
        return _error("API_BASE_URL and CONTROL_API_KEY environment variables are required for *_all_algos actions")

    try:
        algos = _list_enabled_algos(api_base_url, api_key)
    except AlgoListError as exc:
        return _error(str(exc))

    if not algos:
        _log_event("ALL_ALGOS_NONE_ENABLED", action=action)
        return {"success": True, "results": [], "message": "no enabled algos found"}

    single_action = action.removesuffix("_all_algos") + "_algo"  # e.g. start_all_algos -> start_algo
    results = [
        _action_algo_command(single_action, instance_id, repo_path, {"algo_id": algo["algo_id"]})
        for algo in algos
    ]
    _log_event("ALL_ALGOS_COMMAND_SENT", action=action, algo_count=len(results))
    return {"success": all(r.get("success") for r in results), "results": results}


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

    shell_command = _build_algo_shell_command(repo_path, "STATUS", algo_name, None)
    try:
        command_id = _send_ssm_command(instance_id, shell_command)
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


def _action_sync_repo(instance_id: str, repo_path: str, event: dict) -> dict:
    """`git pull --ff-only` in repo_path -- deliberately NOT routed through
    trading_agent.py's own UPDATE command, which pre-checks the target
    algo already exists on disk (_algo_main_path) BEFORE pulling. That's
    backwards for this action's actual purpose: making a brand-new
    algo_name (which by definition doesn't exist on disk yet) show up on
    the instance in the first place. A plain repo-wide pull has no such
    precondition -- it just brings in whatever's new, including a
    strategy folder that's never existed there before.

    Not parsed as trading_agent.py JSON output (unlike the other algo
    actions) -- git's own stdout/stderr is passed back close to verbatim
    instead of being forced into that shape."""
    shell_command = f'cd "{repo_path}"; git pull --ff-only 2>&1'
    try:
        command_id = _send_ssm_command(instance_id, shell_command)
    except (ClientError, BotoCoreError) as exc:
        return _error(f"send_command failed: {exc}", server_id=instance_id)

    deadline = time.monotonic() + SYNC_REPO_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            result = _get_command_result(command_id, instance_id)
        except (ClientError, BotoCoreError) as exc:
            return _error(f"get_command_invocation failed: {exc}", job_id=command_id)

        ssm_status = result.get("Status", "Pending")
        if ssm_status in ("Success", "Failed", "Cancelled", "TimedOut"):
            output = result.get("StandardOutputContent", "").strip() or result.get("StandardErrorContent", "").strip()
            _log_event("SYNC_REPO_DONE", server_id=instance_id, ssm_status=ssm_status, job_id=command_id)
            return {
                "success": ssm_status == "Success",
                "job_id": command_id,
                "ssm_status": ssm_status,
                "server_id": instance_id,
                "output": output,
            }
        time.sleep(SYNC_REPO_POLL_INTERVAL_SECONDS)

    # Didn't finish within the short bounded wait -- the pull is still
    # running on the instance (slow network, large diff). Report that
    # rather than blocking the caller (a synchronous API request) longer.
    _log_event("SYNC_REPO_POLL_TIMED_OUT", server_id=instance_id, job_id=command_id)
    return {
        "success": True,
        "job_id": command_id,
        "server_id": instance_id,
        "status": "PENDING",
        "message": "sync still running — poll get_command_status with this job_id",
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
    elif action == "check_ec2_health":
        result = _action_check_ec2_health(instance_id, event)
    elif action in ALGO_ACTIONS:
        repo_path = _get_env("REPO_PATH")
        if not repo_path:
            return _error("REPO_PATH environment variable is not set")
        result = _action_algo_command(action, instance_id, repo_path, event)
    elif action in ALL_ALGOS_ACTIONS:
        repo_path = _get_env("REPO_PATH")
        if not repo_path:
            return _error("REPO_PATH environment variable is not set")
        result = _action_all_algos_command(action, instance_id, repo_path, event)
    elif action == "get_command_status":
        result = _action_get_command_status(instance_id, event)
    elif action == "get_algo_status":
        repo_path = _get_env("REPO_PATH")
        if not repo_path:
            return _error("REPO_PATH environment variable is not set")
        result = _action_get_algo_status(instance_id, repo_path, event)
    elif action == "sync_repo":
        repo_path = _get_env("REPO_PATH")
        if not repo_path:
            return _error("REPO_PATH environment variable is not set")
        result = _action_sync_repo(instance_id, repo_path, event)
    else:
        result = _error(f"unknown action: {action}")

    _log_event("LAMBDA_RESULT", action=action, result=result)
    return result
