"""Fixtures for the orchestrator Lambda tests.

orchestrator.py lives in trading/infrastructure/lambda/ (not a package) and
builds boto3 clients at import. We add it to sys.path, neutralise boto3 at
import time, and hand each test the module with fresh MagicMock clients so
module-global mutations never leak between tests. No real AWS, ever.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = Path(__file__).resolve().parents[2] / "trading" / "infrastructure" / "lambda"
if str(_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_DIR))

# dummy AWS env so botocore never reaches for IMDS / real credentials
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def orch(monkeypatch):
    """Fresh import of orchestrator with mocked boto3 clients."""
    with patch("boto3.client", return_value=MagicMock()):
        if "orchestrator" in sys.modules:
            module = importlib.reload(sys.modules["orchestrator"])
        else:
            module = importlib.import_module("orchestrator")

    module.ec2_client = MagicMock()
    module.ssm_client = MagicMock()

    for var in ("INSTANCE_ID", "REPO_PATH", "API_BASE_URL", "CONTROL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("INSTANCE_ID", "i-test123")
    monkeypatch.setenv("REPO_PATH", r"C:\trading-app")
    return module
