"""
Local boto3 stand-in for Lambda (Milestone 4 replaces this with a real
Lambda function calling the same SSM API). Sends a command to an EC2
instance via AWS Systems Manager and prints the result — no SSH, no open
inbound ports, just the SSM Agent + an IAM instance role.

Supports both Windows (AWS-RunPowerShellScript) and Linux
(AWS-RunShellScript) targets via --os -- this project's original EC2
instance is Windows, a second instance is Linux, both run the same
Python codebase.

Usage:
    python trading/infrastructure/ssm_invoke.py \
        --instance-id i-0123456789abcdef0 --region ap-south-1 --os windows \
        --repo-path "C:\\trading-app" \
        algo START_ALGO example_strategy

    python trading/infrastructure/ssm_invoke.py \
        --instance-id i-0abcdef0123456789 --region ap-south-1 --os linux \
        --repo-path "/home/ec2-user/trading-app" \
        algo START_ALGO example_strategy

    python trading/infrastructure/ssm_invoke.py \
        --instance-id i-0123456789abcdef0 --region ap-south-1 --os windows \
        raw "Get-Service AmazonSSMAgent"

    python trading/infrastructure/ssm_invoke.py \
        --instance-id i-0abcdef0123456789 --region ap-south-1 --os linux \
        raw "systemctl status amazon-ssm-agent"

Instance ID, region, and repo path are deliberately required arguments
(or env vars) rather than hard-coded — see EC2_INSTANCE_ID / AWS_REGION /
EC2_REPO_PATH below. --os defaults to "windows" to preserve existing
scripts/docs written before Linux support was added.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, WaiterError
except ImportError:
    print("Missing dependency: pip install boto3", file=sys.stderr)
    sys.exit(1)

DOCUMENT_BY_OS = {
    "windows": "AWS-RunPowerShellScript",
    "linux": "AWS-RunShellScript",
}


def send_command(
    instance_id: str,
    region: str,
    commands: list[str],
    document_name: str,
    timeout_seconds: int = 60,
) -> dict:
    """Send a command via SSM SendCommand and block until it completes
    (or the wait itself times out — the command may still finish server-
    side after that; check status separately if so)."""
    client = boto3.client("ssm", region_name=region)

    response = client.send_command(
        InstanceIds=[instance_id],
        DocumentName=document_name,
        Parameters={"commands": commands},
        TimeoutSeconds=timeout_seconds,
    )
    command_id = response["Command"]["CommandId"]

    # SSM needs a moment to register the invocation before it's queryable.
    time.sleep(1)

    waiter = client.get_waiter("command_executed")
    try:
        waiter.wait(
            CommandId=command_id,
            InstanceId=instance_id,
            WaiterConfig={"Delay": 2, "MaxAttempts": max(5, timeout_seconds // 2)},
        )
    except WaiterError:
        pass  # command may have failed or still be running — fetch what we can

    try:
        result = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except (ClientError, BotoCoreError) as exc:
        return {"CommandId": command_id, "Error": str(exc)}

    return {
        "CommandId": command_id,
        "Status": result.get("Status"),
        "StandardOutputContent": result.get("StandardOutputContent", ""),
        "StandardErrorContent": result.get("StandardErrorContent", ""),
        "ResponseCode": result.get("ResponseCode"),
    }


def check_command(instance_id: str, region: str, command_id: str) -> dict:
    """Look up an already-sent command's current status/output without
    sending a new one -- avoids racing a still-running command by
    accidentally re-triggering it, and doesn't depend on the local aws
    CLI being installed/on PATH (uses boto3 directly, same as everything
    else in this script)."""
    client = boto3.client("ssm", region_name=region)
    try:
        result = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except (ClientError, BotoCoreError) as exc:
        return {"CommandId": command_id, "Error": str(exc)}

    return {
        "CommandId": command_id,
        "Status": result.get("Status"),
        "StandardOutputContent": result.get("StandardOutputContent", ""),
        "StandardErrorContent": result.get("StandardErrorContent", ""),
        "ResponseCode": result.get("ResponseCode"),
    }


def build_algo_command(repo_path: str, command: str, algo_name: str, lines: int | None, os_name: str) -> str:
    """Build the one-liner that invokes trading_agent.py on the instance.
    python3 on Linux (python often doesn't exist or is Python 2 on older
    AMIs), python on Windows (matches how it was installed there)."""
    python_bin = "python3" if os_name == "linux" else "python"
    agent_path = "trading/agent/trading_agent.py" if os_name == "linux" else "trading\\agent\\trading_agent.py"
    parts = [
        f'cd "{repo_path}";',
        python_bin,
        agent_path,
        command,
        algo_name,
    ]
    if command == "LOGS" and lines is not None:
        parts.append(f"--lines {lines}")
    return " ".join(parts)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Send commands to a trading EC2 instance via SSM.")
    parser.add_argument("--instance-id", default=os.environ.get("EC2_INSTANCE_ID"), required=False)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"), required=False)
    parser.add_argument("--repo-path", default=os.environ.get("EC2_REPO_PATH"), required=False)
    parser.add_argument(
        "--os", dest="os_name", choices=["windows", "linux"],
        default=os.environ.get("EC2_OS", "windows"),
        help="Target instance OS -- selects the SSM document and command syntax. Default: windows.",
    )
    parser.add_argument("--timeout", type=int, default=60)

    subparsers = parser.add_subparsers(dest="mode", required=True)

    algo_parser = subparsers.add_parser("algo", help="Invoke trading_agent.py on the instance")
    algo_parser.add_argument("command", choices=["START_ALGO", "STOP_ALGO", "RESTART_ALGO", "STATUS", "UPDATE", "LOGS"])
    algo_parser.add_argument("algo_name")
    algo_parser.add_argument("--lines", type=int, default=None)

    check_parser = subparsers.add_parser("check", help="Look up an already-sent command by its CommandId")
    check_parser.add_argument("command_id")

    raw_parser = subparsers.add_parser("raw", help="Run an arbitrary shell/PowerShell command or script file")
    raw_group = raw_parser.add_mutually_exclusive_group(required=True)
    raw_group.add_argument("shell_command", nargs="?", default=None)
    raw_group.add_argument(
        "--script-file",
        help=(
            "Path to a local .ps1 (Windows) or .sh (Linux) file whose contents "
            "get sent as-is. Prefer this over an inline command for anything "
            "beyond trivial one-liners — inline commands typed at a shell "
            "prompt go through your own shell's quoting/interpolation before "
            "ever reaching this script, on top of SSM's own parameter "
            "encoding; multi-variable scripts reliably break that way."
        ),
    )

    args = parser.parse_args()

    if not args.instance_id:
        parser.error("--instance-id is required (or set EC2_INSTANCE_ID)")
    if not args.region:
        parser.error("--region is required (or set AWS_REGION)")

    document_name = DOCUMENT_BY_OS[args.os_name]

    if args.mode == "check":
        result = check_command(args.instance_id, args.region, args.command_id)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("Status") == "Success" else 1

    if args.mode == "algo":
        if not args.repo_path:
            parser.error("--repo-path is required for 'algo' mode (or set EC2_REPO_PATH)")
        commands = [build_algo_command(args.repo_path, args.command, args.algo_name, args.lines, args.os_name)]
    elif args.script_file:
        script_path = Path(args.script_file)
        if not script_path.is_file():
            parser.error(f"--script-file not found: {script_path}")
        commands = script_path.read_text().splitlines()
    else:
        commands = [args.shell_command]

    result = send_command(args.instance_id, args.region, commands, document_name, args.timeout)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("Status") == "Success" else 1


if __name__ == "__main__":
    sys.exit(_cli())
