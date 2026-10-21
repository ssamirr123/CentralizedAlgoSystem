"""Scrip master download resilience (2026-10-21 IncompleteRead crash)."""

import http.client
import sys
from pathlib import Path

import pytest
import requests

from trading.common import scrip_master


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return [{"name": "NIFTY", "exch_seg": "NFO"}]


@pytest.fixture
def patched(monkeypatch, tmp_path):
    monkeypatch.setattr(scrip_master.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(scrip_master.time, "sleep", lambda s: None)


def test_retries_incomplete_read_then_caches(patched, monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ChunkedEncodingError(http.client.IncompleteRead(b"x", 5))
        return _Resp()

    monkeypatch.setattr(scrip_master.requests, "get", fake_get)
    assert scrip_master.load_scrip_master()[0]["name"] == "NIFTY"
    assert len(calls) == 3
    monkeypatch.setattr(scrip_master.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert scrip_master.load_scrip_master()[0]["name"] == "NIFTY"   # disk cache


def test_raises_after_all_attempts(patched, monkeypatch):
    monkeypatch.setattr(scrip_master.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    with pytest.raises(requests.ConnectionError):
        scrip_master.load_scrip_master()


def test_double_straddle_survives_download_failure(patched, monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2] / "trading" / "algos" / "DoubleStraddelAlgo"
    sys.path.insert(0, str(root))
    monkeypatch.chdir(tmp_path)
    import token_file
    monkeypatch.setattr(scrip_master.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))

    # No previous csv -> clear failure, not a silent bad state.
    with pytest.raises(RuntimeError):
        token_file.download_token()

    # Previous csv with a future expiry -> reused.
    (tmp_path / "nifty_token.csv").write_text("token,symbol,expiry\n1,NIFTY01JAN2099X,01JAN2099\n")
    token_file.download_token()
