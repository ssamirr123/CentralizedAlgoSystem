"""
Invokes the Milestone 4 orchestration Lambda synchronously (RequestResponse)
and returns its parsed JSON response.

Vercel serverless functions have no IAM role to assume (unlike the EC2
instance), so this needs real AWS credentials via env vars:
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
Use a dedicated IAM user scoped to ONLY lambda:InvokeFunction on this one
function -- not the trading-control-cli user (that one's for one-time
setup from your machine, this is a always-on production credential a
Vercel serverless function holds).
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


class LambdaInvokeError(RuntimeError):
    pass


def invoke_orchestrator(action: str, **payload: Any) -> dict:
    function_name = os.environ.get("LAMBDA_FUNCTION_NAME")
    if not function_name:
        raise LambdaInvokeError("LAMBDA_FUNCTION_NAME environment variable is not set")

    client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION"))
    event = {"action": action, **payload}

    try:
        response = client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8"),
        )
    except (ClientError, BotoCoreError) as exc:
        raise LambdaInvokeError(f"Lambda invoke failed: {exc}") from exc

    if response.get("FunctionError"):
        raw = response["Payload"].read().decode("utf-8")
        raise LambdaInvokeError(f"Lambda returned an error: {raw}")

    try:
        return json.loads(response["Payload"].read().decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise LambdaInvokeError(f"Lambda response was not valid JSON: {exc}") from exc


def invoke_orchestrator_async(action: str, **payload: Any) -> None:
    """Fire-and-forget (InvocationType=Event) -- for actions that can
    legitimately take minutes (provision_server: attach IAM profile,
    reboot, wait for SSM, clone, install deps), which a synchronous
    RequestResponse call can't wait out inside a single Vercel request
    (hard timeout, no maxDuration configured). The Lambda has its own
    15-minute budget and reports progress back via PATCH /api/servers/{id}
    using its own API_BASE_URL/CONTROL_API_KEY env vars, the same pattern
    *_all_algos already uses to read the algo list."""
    function_name = os.environ.get("LAMBDA_FUNCTION_NAME")
    if not function_name:
        raise LambdaInvokeError("LAMBDA_FUNCTION_NAME environment variable is not set")

    client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION"))
    event = {"action": action, **payload}

    try:
        client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(event).encode("utf-8"),
        )
    except (ClientError, BotoCoreError) as exc:
        raise LambdaInvokeError(f"Lambda async invoke failed: {exc}") from exc
