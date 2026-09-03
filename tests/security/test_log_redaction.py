"""Stage 20 -- centralized log redaction."""
from __future__ import annotations

import json
import logging

import pytest

from trading.common.logger import (
    RootJsonFormatter,
    SecretRedactionFilter,
    configure_logging,
    redact_text,
)

SECRET = "S3cr3tV4lue123ABC"


@pytest.mark.parametrize(
    "raw",
    [
        f"logging in with api_key={SECRET}",
        f'{{"secret_key": "{SECRET}"}}',
        f"mpin={SECRET} totp_secret={SECRET}",
        f"Authorization: Bearer {SECRET}.{SECRET}.{SECRET}",
        f"DATABASE_URL=postgresql://algo:{SECRET}@10.0.0.1:5432/db",
        f"breeze_session_token: '{SECRET}'",
        f"CONTROL_API_KEY={SECRET}",
    ],
)
def test_redact_text_removes_the_value(raw):
    out = redact_text(raw)
    assert SECRET not in out
    assert "<redacted>" in out


def test_redact_text_keeps_ordinary_text():
    msg = "NIFTY 25000 CE premium 151.55 combined VWAP 265.4"
    assert redact_text(msg) == msg


def test_aws_key_and_pem_redacted():
    assert "AKIA" not in redact_text("aws_access_key_id AKIAABCDEFGHIJKLMNOP")
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIabc" not in redact_text(pem)


def test_filter_scrubs_before_formatting(caplog):
    flt = SecretRedactionFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "connect api_key=%s", (SECRET,), None)
    assert flt.filter(rec) is True
    assert SECRET not in rec.getMessage()


def test_root_formatter_emits_redacted_json():
    configure_logging(force=True)
    fmt = RootJsonFormatter()
    rec = logging.LogRecord("trading.api", logging.WARNING, __file__, 1,
                            "breeze generate_session failed: session_token=%s", (SECRET,), None)
    payload = json.loads(fmt.format(rec))
    assert SECRET not in json.dumps(payload)
    assert "<redacted>" in payload["message"]
