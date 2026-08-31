"""
Invokes the orchestration Lambda (TradingOrchestrator) synchronously
(RequestResponse) and returns its parsed JSON response.

Credentials come from the Backend EC2 instance role, which must allow
`lambda:InvokeFunction` on TradingOrchestrator. `LAMBDA_FUNCTION_NAME`
and `AWS_REGION` are read from the environment.
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from trading.core.config import load_settings


class LambdaInvokeError(RuntimeError):
    pass


def invoke_orchestrator(action: str, **payload: Any) -> dict:
    settings = load_settings()
    function_name = settings.lambda_function_name
    if not function_name:
        raise LambdaInvokeError("LAMBDA_FUNCTION_NAME environment variable is not set")

    client = boto3.client("lambda", region_name=settings.aws_region or None)
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
    RequestResponse call can't wait out inside a single HTTP request. The
    Lambda has its own
    15-minute budget and reports progress back via PATCH /api/servers/{id}
    using its own API_BASE_URL/CONTROL_API_KEY env vars, the same pattern
    *_all_algos already uses to read the algo list."""
    settings = load_settings()
    function_name = settings.lambda_function_name
    if not function_name:
        raise LambdaInvokeError("LAMBDA_FUNCTION_NAME environment variable is not set")

    client = boto3.client("lambda", region_name=settings.aws_region or None)
    event = {"action": action, **payload}

    try:
        client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(event).encode("utf-8"),
        )
    except (ClientError, BotoCoreError) as exc:
        raise LambdaInvokeError(f"Lambda async invoke failed: {exc}") from exc
