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
                                                          enabled algo on every server (for EventBridge
                                                          Scheduler -- one static payload per schedule,
                                                          not one rule per algo or per server)
    get_command_status                                -- poll a job_id from the above
    get_algo_status                                   -- convenience: STATUS + short bounded wait
    sync_repo                                         -- `git pull --ff-only` in repo_path, then (if
                                                          algo_id is present) that algo's own
                                                          requirements.txt if it has one -- short bounded
                                                          wait (like get_algo_status, not fire-and-forget --
                                                          called synchronously right after a new strategy is
                                                          registered via POST /api/algos, so the instance
                                                          actually has the code AND deps for whatever
                                                          algo_name was just added; a DB row alone puts no
                                                          file on disk, and provision_server's dependency
                                                          install only ever ran once, at initial server setup)
    provision_server                                  -- fire-and-forget (InvocationType=Event), called
                                                          right after POST /api/servers commits a new row:
                                                          attach the IAM instance profile if not already
                                                          associated, reboot + wait for SSM if the agent
                                                          isn't already online, clone the repo, install
                                                          deps. Reports its own progress back via
                                                          PATCH /api/servers/{name} since nothing is left
                                                          synchronously waiting on this Lambda's result.

IMPORTANT — matches this project's own safety principle: algo-control
actions do NOT wait for the algo to actually be running before returning.
They send the SSM command and return immediately with a job_id and
status "STARTING"/"STOPPING"/etc. The caller must poll get_command_status
to learn the REAL outcome (which itself reflects trading_agent.py's own
process-liveness verification, not "the SSM command was accepted"). A
slow remote operation (we've seen a plain git clone hang for minutes on a
credential prompt) would otherwise either blow the Lambda timeout or hold
an expensive invocation open pointlessly.

Per-server routing: every action that targets a specific instance accepts
instance_id/repo_path/os_name directly in the event payload (the API
resolves these from the servers table row for whichever server_id was
actually selected). Lambda environment variables (INSTANCE_ID/REPO_PATH/
SERVER_OS) are kept ONLY as a fallback default for callers that don't
pass them -- there is no longer a single "the" target instance.

Configuration via Lambda environment variables:
    INSTANCE_ID, REPO_PATH, SERVER_OS  -- fallback defaults, not the primary
                                          routing mechanism anymore (see above)
    EC2_INSTANCE_PROFILE_NAME          -- IAM instance profile provision_server attaches
                                          (default: TradingEC2SSMProfile)
    REPO_CLONE_URL, REPO_BRANCH        -- what provision_server clones
                                          (defaults: this project's repo, web-base-algo-trading-control)
    AWS_REGION        -- set automatically by the Lambda runtime; not user-configured
    API_BASE_URL      -- the deployed control-center API (required for *_all_algos and
                          provision_server -- these need to read/write the DB, which only
                          the API layer can do)
    CONTROL_API_KEY   -- auth for the above (same key the dashboard uses)

*_all_algos and provision_server call the API via urllib (stdlib only --
deliberately not the `requests` package, to keep deployment a single-file
zip with no dependency packaging step) rather than connecting to Supabase
directly. This Lambda's job stays "invoke SSM," not "know how to query
Postgres" -- that's already the API layer's responsibility (Milestone 6),
and duplicating it here would mean bundling SQLAlchemy/psycopg2 into a
Lambda package, which is real packaging complexity for no architectural
benefit when the API already exposes exactly this.
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

# provision_server runs fire-and-forget (InvocationType=Event), so it can
# use this Lambda's full timeout budget (configured to 870s -- see
# DEPLOY.md/setup notes -- comfortably under the 900s/15min hard cap,
# leaving ~30s of headroom for the Lambda's own overhead). A reboot +
# SSM Agent re-registration is the slow part; repo clone/deps install is
# normally much faster than either budget below.
PROVISION_SSM_WAIT_TIMEOUT_SECONDS = 480
PROVISION_SSM_WAIT_INTERVAL_SECONDS = 15
PROVISION_SETUP_TIMEOUT_SECONDS = 300
PROVISION_SETUP_POLL_INTERVAL_SECONDS = 5


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
    """Linux target -- venv/bin/python3 (not bare python3), since AL2023's
    RPM-installed requests/urllib3 conflict with pip's in system
    site-packages (see install_deps_linux.sh), matching the identical
    pattern already used by ssm_invoke.py's manual CLI tool.

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


def _build_algo_powershell_command(
    repo_path: str, agent_command: str, algo_name: str, lines: int | None,
    server_name: str | None = None, api_base_url: str | None = None, control_api_key: str | None = None,
) -> str:
    """Windows target -- bare `python` (no venv), matching how the
    original Windows instance was set up (no AL2023-style RPM/pip
    conflict on that AMI). Same env-export reasoning as the Linux
    builder, PowerShell syntax instead of bash."""
    parts = []
    if agent_command in ("START_ALGO", "RESTART_ALGO") and server_name and api_base_url and control_api_key:
        parts.append(
            f'$env:STRATEGY_NAME="{algo_name}"; $env:SERVER_NAME="{server_name}"; '
            f'$env:API_BASE_URL="{api_base_url}"; $env:CONTROL_API_KEY="{control_api_key}";'
        )
    parts += [f'cd "{repo_path}";', "python trading\\agent\\trading_agent.py", agent_command, algo_name]
    if agent_command == "LOGS" and lines is not None:
        parts.append(f"--lines {lines}")
    return " ".join(parts)


def _build_algo_command(
    os_name: str, repo_path: str, agent_command: str, algo_name: str, lines: int | None,
    server_name: str | None = None, api_base_url: str | None = None, control_api_key: str | None = None,
) -> str:
    builder = _build_algo_powershell_command if os_name == "windows" else _build_algo_shell_command
    return builder(repo_path, agent_command, algo_name, lines, server_name, api_base_url, control_api_key)


def _send_ssm_command(instance_id: str, command: str, os_name: str = "linux") -> str:
    document = "AWS-RunPowerShellScript" if os_name == "windows" else "AWS-RunShellScript"
    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName=document,
        Parameters={"commands": [command]},
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


def _api_request(method: str, path: str, api_base_url: str, api_key: str, body: dict | None = None, timeout: int = 15) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{api_base_url.rstrip('/')}{path}",
        data=data, method=method,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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


def _action_algo_command(action: str, instance_id: str, repo_path: str, os_name: str, event: dict) -> dict:
    algo_name = event.get("algo_id") or event.get("algo_name")
    if not algo_name:
        return _error("algo_id (or algo_name) is required for this action")

    agent_command = ALGO_ACTIONS[action]
    lines = event.get("lines")
    command = _build_algo_command(
        os_name, repo_path, agent_command, algo_name, lines,
        server_name=event.get("server_name") or _get_env("SERVER_NAME"),
        api_base_url=_get_env("API_BASE_URL"), control_api_key=_get_env("CONTROL_API_KEY"),
    )

    try:
        command_id = _send_ssm_command(instance_id, command, os_name)
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
    try:
        algos = _api_request("GET", "/api/algos", api_base_url, api_key, timeout=10)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise AlgoListError(f"could not fetch algo list from {api_base_url}/api/algos: {exc}") from exc
    return [a for a in algos if a.get("enabled")]


def _list_servers(api_base_url: str, api_key: str) -> dict[str, dict]:
    """Returns {server_name: server_dict}, so _action_all_algos_command
    can look up each algo's OWN server's instance_id/repo_path/os --
    without this, every algo would route to whichever single instance_id
    happened to be passed into this action, wrong for any algo that
    isn't on that one server."""
    try:
        servers = _api_request("GET", "/api/servers", api_base_url, api_key, timeout=10)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise AlgoListError(f"could not fetch server list from {api_base_url}/api/servers: {exc}") from exc
    return {s["server_id"]: s for s in servers}


def _action_all_algos_command(action: str, event: dict) -> dict:
    """Same underlying SSM command as _action_algo_command, applied to
    every enabled algo on every server instead of one named in the event
    -- for EventBridge Scheduler, which passes a single static payload
    per rule rather than one rule per algo or per server. Each algo is
    routed to its OWN server's instance_id/repo_path/os (see
    _list_servers), not a single instance_id passed into this action."""
    api_base_url = _get_env("API_BASE_URL")
    api_key = _get_env("CONTROL_API_KEY")
    if not api_base_url or not api_key:
        return _error("API_BASE_URL and CONTROL_API_KEY environment variables are required for *_all_algos actions")

    try:
        algos = _list_enabled_algos(api_base_url, api_key)
        servers_by_name = _list_servers(api_base_url, api_key)
    except AlgoListError as exc:
        return _error(str(exc))

    if not algos:
        _log_event("ALL_ALGOS_NONE_ENABLED", action=action)
        return {"success": True, "results": [], "message": "no enabled algos found"}

    single_action = action.removesuffix("_all_algos") + "_algo"  # e.g. start_all_algos -> start_algo
    results = []
    for algo in algos:
        server = servers_by_name.get(algo["server_id"])
        if server is None:
            results.append(_error(f"unknown server for algo: {algo['server_id']}", algo_id=algo["algo_id"]))
            continue
        results.append(
            _action_algo_command(
                single_action, server["ec2_instance_id"], server["repo_path"], server.get("os", "linux"),
                {"algo_id": algo["algo_id"], "server_name": algo["server_id"]},
            )
        )
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


def _action_get_algo_status(instance_id: str, repo_path: str, os_name: str, event: dict) -> dict:
    algo_name = event.get("algo_id") or event.get("algo_name")
    if not algo_name:
        return _error("algo_id (or algo_name) is required for this action")

    command = _build_algo_command(os_name, repo_path, "STATUS", algo_name, None)
    try:
        command_id = _send_ssm_command(instance_id, command, os_name)
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


def _action_sync_repo(instance_id: str, repo_path: str, os_name: str, event: dict) -> dict:
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
    instead of being forced into that shape.

    Also installs the newly-registered algo's own requirements.txt (if
    algo_id is present in the event and that file exists) -- discovered
    that a brand-new algo pulled in here had never had its own deps
    installed anywhere (provision_server only ran once, at initial
    server setup, long before this algo existed), so it would start,
    crash with ModuleNotFoundError, and land in status ERROR the first
    time anyone hit Start."""
    # git refuses to operate on a repo it doesn't own ("dubious ownership")
    # once SSM (which runs commands as root/SYSTEM) touches a repo whose
    # files are chown'd to a login user (provision_server does this so
    # manual SSH sessions can write to it too, e.g. a strategy's own
    # state/log files) -- discovered this silently broke every git pull
    # here (the overall command still reported "success" because the
    # trailing pip-install step still ran and set the exit code). Marking
    # the path as safe is idempotent, so it's cheap to redo on every call
    # rather than depending on provision_server having done it once.
    algo_name = event.get("algo_id") or event.get("algo_name")
    if os_name == "windows":
        command = (
            f'git config --global --add safe.directory "{repo_path}"; '
            f'cd "{repo_path}"; git pull --ff-only 2>&1 | Out-String'
        )
        if algo_name:
            command += (
                f'; $req = "trading\\algos\\{algo_name}\\requirements.txt"; '
                f'if (Test-Path $req) {{ pip install -r $req 2>&1 | Out-String }}'
            )
    else:
        command = (
            f'git config --global --add safe.directory "{repo_path}"; '
            f'cd "{repo_path}"; git pull --ff-only 2>&1'
        )
        if algo_name:
            command += (
                f'; req="trading/algos/{algo_name}/requirements.txt"; '
                f'if [ -f "$req" ]; then venv/bin/pip install -r "$req" 2>&1; fi'
            )
    try:
        command_id = _send_ssm_command(instance_id, command, os_name)
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


def _wait_for_ssm_command(instance_id: str, command_id: str, timeout_seconds: int, interval_seconds: int) -> dict | None:
    """Polls a raw SSM command (not a trading_agent.py one -- no JSON
    output parsing) to a terminal state. Returns None on timeout so the
    caller can report that distinctly from a real failure."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            result = _get_command_result(command_id, instance_id)
        except (ClientError, BotoCoreError) as exc:
            return {"Status": "Failed", "StandardErrorContent": str(exc), "StandardOutputContent": ""}
        if result.get("Status", "Pending") in ("Success", "Failed", "Cancelled", "TimedOut"):
            return result
        time.sleep(interval_seconds)
    return None


def _action_provision_server(instance_id: str, os_name: str, repo_path: str, server_name: str, event: dict) -> dict:
    """Bootstraps a brand-new EC2 instance so it can actually run algo
    commands: attach the IAM instance profile if not already associated,
    reboot + wait for the SSM Agent to come online if it isn't already
    (the same root cause this project already hit once -- the agent gives
    up trying to get credentials at boot if the profile wasn't attached
    yet, and attaching it after boot doesn't retroactively fix an agent
    that's already given up), clone the repo, install dependencies.

    Reports progress via PATCH /api/servers/{server_name} at each step --
    this runs fire-and-forget (InvocationType=Event from the API), so
    there's no caller left waiting synchronously on this function's
    return value by the time any of this actually happens."""
    api_base_url = _get_env("API_BASE_URL")
    api_key = _get_env("CONTROL_API_KEY")
    if not api_base_url or not api_key:
        _log_event("PROVISION_MISSING_CONFIG", server_name=server_name)
        return _error("API_BASE_URL and CONTROL_API_KEY are required for provisioning callbacks")

    def report(provisioning_status: str, message: str) -> None:
        _log_event("PROVISION_STEP", server_name=server_name, status=provisioning_status, message=message)
        try:
            _api_request(
                "PATCH", f"/api/servers/{server_name}", api_base_url, api_key,
                body={"provisioning_status": provisioning_status, "provisioning_message": message[:2000]},
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            _log_event("PROVISION_REPORT_FAILED", server_name=server_name, error=str(exc))

    # Step 1: attach the IAM instance profile if not already associated.
    profile_name = _get_env("EC2_INSTANCE_PROFILE_NAME") or "TradingEC2SSMProfile"
    try:
        assoc = ec2_client.describe_iam_instance_profile_associations(
            Filters=[{"Name": "instance-id", "Values": [instance_id]}],
        )
        has_profile = any(
            a["State"] in ("associating", "associated")
            for a in assoc.get("IamInstanceProfileAssociations", [])
        )
    except (ClientError, BotoCoreError) as exc:
        report("FAILED", f"could not check instance profile: {exc}")
        return _error(str(exc))

    attached_profile_now = False
    if not has_profile:
        try:
            ec2_client.associate_iam_instance_profile(
                IamInstanceProfile={"Name": profile_name}, InstanceId=instance_id,
            )
        except (ClientError, BotoCoreError) as exc:
            report("FAILED", f"could not attach IAM instance profile '{profile_name}': {exc}")
            return _error(str(exc))
        report("PROVISIONING", "IAM instance profile attached; checking SSM registration")
        attached_profile_now = True

    # Step 2: make sure the SSM Agent is actually online -- reboot if not.
    def ssm_online() -> bool:
        try:
            info = ssm_client.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}],
            )
        except (ClientError, BotoCoreError):
            return False
        instances = info.get("InstanceInformationList", [])
        return bool(instances) and instances[0].get("PingStatus") == "Online"

    if not ssm_online():
        try:
            ec2_client.reboot_instances(InstanceIds=[instance_id])
        except (ClientError, BotoCoreError) as exc:
            report("FAILED", f"could not reboot instance: {exc}")
            return _error(str(exc))
        report(
            "PROVISIONING",
            "Instance rebooting (needed for the SSM Agent to pick up credentials); waiting for it to come online"
            if attached_profile_now else
            "SSM Agent not responding; rebooted instance and waiting for it to come online",
        )

        deadline = time.monotonic() + PROVISION_SSM_WAIT_TIMEOUT_SECONDS
        online = False
        while time.monotonic() < deadline:
            if ssm_online():
                online = True
                break
            time.sleep(PROVISION_SSM_WAIT_INTERVAL_SECONDS)
        if not online:
            report(
                "FAILED",
                f"SSM Agent did not come online within {PROVISION_SSM_WAIT_TIMEOUT_SECONDS}s after reboot",
            )
            return _error("SSM Agent did not come online after reboot")

    report("PROVISIONING", "SSM Agent online; cloning repo and installing dependencies")

    # Step 3: clone the repo (idempotent -- skip if the directory already
    # exists) and install dependencies.
    repo_url = _get_env("REPO_CLONE_URL") or "https://github.com/ssamirr123/CentralizedAlgoSystem.git"
    branch = _get_env("REPO_BRANCH") or "web-base-algo-trading-control"
    if os_name == "windows":
        setup_command = (
            f'git config --global --add safe.directory "{repo_path}"; '
            f'if (-not (Test-Path "{repo_path}")) {{ git clone --branch {branch} {repo_url} "{repo_path}" }}; '
            f'cd "{repo_path}"; pip install -r requirements.txt; '
            # Every algo's own requirements.txt, not just example_strategy's
            # -- discovered installing DoubleStraddelAlgo (needs
            # smartapi-python) that only example_strategy's deps were ever
            # installed, silently leaving every other algo's own
            # dependencies missing until started and crashing with
            # ModuleNotFoundError.
            f'Get-ChildItem -Path "trading\\algos" -Filter requirements.txt -Recurse '
            f'| ForEach-Object {{ pip install -r $_.FullName }}'
        )
    else:
        # A fresh EC2 instance may not have git/python3 preinstalled at all
        # (discovered provisioning samir_linux -- a minimal AMI with neither,
        # unlike Linux-server which had both from manual setup) -- installed
        # here via whichever of yum/apt is present rather than assuming a
        # specific distro, since new servers can be added from any base AMI.
        install_prereqs = (
            'if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then '
            'if command -v yum >/dev/null 2>&1; then sudo yum install -y git python3 python3-pip; '
            'elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update -y && sudo apt-get install -y git python3 python3-pip python3-venv; '
            'fi; fi'
        )
        # Every algo's own requirements.txt, not just example_strategy's --
        # discovered installing DoubleStraddelAlgo (needs smartapi-python)
        # that only example_strategy's deps were ever installed, silently
        # leaving every other algo's own dependencies missing until
        # started and crashing with ModuleNotFoundError. find handles a
        # repo with zero algos yet without erroring (unlike a bare glob).
        install_all_algo_deps = (
            r'find trading/algos -maxdepth 2 -name requirements.txt -exec venv/bin/pip install -r {} \;'
        )
        # SSM runs this whole script as root, so `$(whoami)` here always
        # evaluated to "root" -- chowning root to root, a silent no-op.
        # Discovered this the hard way: samir_linux went through this
        # exact automated pipeline and still ended up entirely root-owned,
        # only surfacing once someone SSH'd in as ec2-user and hit
        # Permission denied. /home's first (and normally only) entry is
        # the actual EC2 login user across every common AMI convention
        # (ec2-user, ubuntu, admin, ...); ec2-user is the fallback only if
        # no /home dirs exist at all.
        chown_target = 'target_user=$(ls /home | head -1); target_user=${target_user:-ec2-user}'
        setup_command = (
            f'{install_prereqs}; '
            f'git config --global --add safe.directory "{repo_path}"; '
            f'if [ ! -d "{repo_path}" ]; then sudo git clone --branch {branch} {repo_url} "{repo_path}"; fi; '
            f'{chown_target}; sudo chown -R "$target_user":"$target_user" "{repo_path}"; '
            f'cd "{repo_path}"; python3 -m venv venv; '
            f'venv/bin/pip install --upgrade pip; '
            f'venv/bin/pip install -r requirements.txt; '
            f'{install_all_algo_deps}'
        )

    try:
        command_id = _send_ssm_command(instance_id, setup_command, os_name)
    except (ClientError, BotoCoreError) as exc:
        report("FAILED", f"send_command for repo setup failed: {exc}")
        return _error(str(exc))

    result = _wait_for_ssm_command(
        instance_id, command_id, PROVISION_SETUP_TIMEOUT_SECONDS, PROVISION_SETUP_POLL_INTERVAL_SECONDS,
    )
    if result is None:
        report("FAILED", f"repo setup did not complete within {PROVISION_SETUP_TIMEOUT_SECONDS}s")
        return _error("repo setup timed out")

    if result.get("Status") == "Success":
        report("READY", "Repo cloned and dependencies installed successfully")
        return {"success": True, "server_id": instance_id, "provisioning_status": "READY"}

    detail = (result.get("StandardErrorContent") or result.get("StandardOutputContent") or "").strip()
    report("FAILED", f"repo setup failed ({result.get('Status')}): {detail}")
    return _error(f"repo setup failed: {result.get('Status')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    action = event.get("action")
    _log_event("LAMBDA_INVOKED", action=action)

    if not action:
        return _error("event.action is required")

    # Per-server routing: prefer what the caller passed (resolved from the
    # servers table for whichever server was actually selected), fall
    # back to this Lambda's own env vars only for callers that don't pass
    # them -- there's no single "the" target instance anymore.
    instance_id = event.get("instance_id") or _get_env("INSTANCE_ID")
    repo_path = event.get("repo_path") or _get_env("REPO_PATH")
    os_name = event.get("os_name") or _get_env("SERVER_OS") or "linux"

    if action in ALL_ALGOS_ACTIONS:
        # Iterates every enabled algo across every server itself -- no
        # single instance_id applies.
        result = _action_all_algos_command(action, event)
        _log_event("LAMBDA_RESULT", action=action, result=result)
        return result

    if action == "provision_server":
        server_name = event.get("server_name")
        if not instance_id or not server_name:
            return _error("instance_id and server_name are required for provision_server")
        result = _action_provision_server(instance_id, os_name, repo_path or "/trading-app", server_name, event)
        _log_event("LAMBDA_RESULT", action=action, result=result)
        return result

    if not instance_id:
        return _error("instance_id is required (pass it explicitly, or set INSTANCE_ID as a fallback)")

    if action == "start_ec2":
        result = _action_start_ec2(instance_id, event)
    elif action == "stop_ec2":
        result = _action_stop_ec2(instance_id, event)
    elif action == "check_ec2_health":
        result = _action_check_ec2_health(instance_id, event)
    elif action in ALGO_ACTIONS:
        if not repo_path:
            return _error("repo_path is required for this action")
        result = _action_algo_command(action, instance_id, repo_path, os_name, event)
    elif action == "get_command_status":
        result = _action_get_command_status(instance_id, event)
    elif action == "get_algo_status":
        if not repo_path:
            return _error("repo_path is required for this action")
        result = _action_get_algo_status(instance_id, repo_path, os_name, event)
    elif action == "sync_repo":
        if not repo_path:
            return _error("repo_path is required for this action")
        result = _action_sync_repo(instance_id, repo_path, os_name, event)
    else:
        result = _error(f"unknown action: {action}")

    _log_event("LAMBDA_RESULT", action=action, result=result)
    return result
