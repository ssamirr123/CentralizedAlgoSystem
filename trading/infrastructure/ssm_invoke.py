"""
Local boto3 stand-in for Lambda (Milestone 4 replaces this with a real
Lambda function calling the same SSM API). Sends a command to an EC2
instance via AWS Systems Manager and prints the result — no SSH, no open
inbound ports, just the SSM Agent + an IAM instance role.

Targets Windows instances (AWS-RunPowerShellScript document) since that's
what this project's EC2 instance actually runs.

Usage:
    python trading/infrastructure/ssm_invoke.py \
        --instance-id i-0123456789abcdef0 --region ap-south-1 \
        --repo-path "C:\\trading-app" \
        algo START_ALGO example_strategy

    python trading/infrastructure/ssm_invoke.py \
        --instance-id i-0123456789abcdef0 --region ap-south-1 \
        raw "Get-Service AmazonSSMAgent"

Instance ID, region, and repo path are deliberately required arguments
(or env vars) rather than hard-coded — see EC2_INSTANCE_ID / AWS_REGION /
EC2_REPO_PATH below.
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
    from botocore.exceptions import ClientError, WaiterError
except ImportError:
    print("Missing dependency: pip install boto3", file=sys.stderr)
    sys.exit(1)


def send_powershell_command(
    instance_id: str,
    region: str,
    commands: list[str],
    timeout_seconds: int = 60,
) -> dict:
    """Send a command via SSM SendCommand and block until it completes
    (or the wait itself times out — the command may still finish server-
    side after that; check status separately if so)."""
    client = boto3.client("ssm", region_name=region)

    response = client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunPowerShellScript",
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
    except ClientError as exc:
        return {"CommandId": command_id, "Error": str(exc)}

    return {
        "CommandId": command_id,
        "Status": result.get("Status"),
        "StandardOutputContent": result.get("StandardOutputContent", ""),
        "StandardErrorContent": result.get("StandardErrorContent", ""),
        "ResponseCode": result.get("ResponseCode"),
    }


def build_algo_command(repo_path: str, command: str, algo_name: str, lines: int | None) -> str:
    """Build the PowerShell one-liner that invokes trading_agent.py on the instance."""
    parts = [
        f'cd "{repo_path}";',
        "python trading\\agent\\trading_agent.py",
        command,
        algo_name,
    ]
    if command == "LOGS" and lines is not None:
        parts.append(f"--lines {lines}")
    return " ".join(parts)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Send commands to the trading EC2 instance via SSM.")
    parser.add_argument("--instance-id", default=os.environ.get("EC2_INSTANCE_ID"), required=False)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"), required=False)
    parser.add_argument("--repo-path", default=os.environ.get("EC2_REPO_PATH"), required=False)
    parser.add_argument("--timeout", type=int, default=60)

    subparsers = parser.add_subparsers(dest="mode", required=True)

    algo_parser = subparsers.add_parser("algo", help="Invoke trading_agent.py on the instance")
    algo_parser.add_argument("command", choices=["START_ALGO", "STOP_ALGO", "RESTART_ALGO", "STATUS", "UPDATE", "LOGS"])
    algo_parser.add_argument("algo_name")
    algo_parser.add_argument("--lines", type=int, default=None)

    raw_parser = subparsers.add_parser("raw", help="Run an arbitrary PowerShell command or script file")
    raw_group = raw_parser.add_mutually_exclusive_group(required=True)
    raw_group.add_argument("powershell_command", nargs="?", default=None)
    raw_group.add_argument(
        "--script-file",
        help=(
            "Path to a local .ps1 file whose contents get sent as-is. "
            "Prefer this over an inline command for anything beyond trivial "
            "one-liners — inline PowerShell typed at a PowerShell prompt "
            "goes through your shell's own quoting/interpolation before it "
            "ever reaches this script, on top of SSM's own parameter "
            "encoding; multi-variable scripts reliably break that way."
        ),
    )

    args = parser.parse_args()

    if not args.instance_id:
        parser.error("--instance-id is required (or set EC2_INSTANCE_ID)")
    if not args.region:
        parser.error("--region is required (or set AWS_REGION)")

    if args.mode == "algo":
        if not args.repo_path:
            parser.error("--repo-path is required for 'algo' mode (or set EC2_REPO_PATH)")
        commands = [build_algo_command(args.repo_path, args.command, args.algo_name, args.lines)]
    elif args.script_file:
        script_path = Path(args.script_file)
        if not script_path.is_file():
            parser.error(f"--script-file not found: {script_path}")
        commands = script_path.read_text().splitlines()
    else:
        commands = [args.powershell_command]

    result = send_powershell_command(args.instance_id, args.region, commands, args.timeout)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("Status") == "Success" else 1


if __name__ == "__main__":
    sys.exit(_cli())
