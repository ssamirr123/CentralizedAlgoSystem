"""Instrument-master download resilience (2026-10-21 09:35 read timeout)."""

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2] / "trading" / "algos" / "supertrendPivotAlgo"
sys.path.insert(0, str(ROOT))

import option_chain  # noqa: E402


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return [{"name": "NIFTY"}]


def test_retries_then_caches(monkeypatch, tmp_path):
    calls = []

    def fake_get(url, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise requests.ConnectionError("Read timed out")
        return _Resp()

    monkeypatch.setattr(option_chain.requests, "get", fake_get)
    monkeypatch.setattr(option_chain.time, "sleep", lambda s: None)
    monkeypatch.setattr(option_chain.tempfile, "gettempdir", lambda: str(tmp_path))

    oc = option_chain.OptionChain()
    assert oc._load_instruments() == [{"name": "NIFTY"}]
    assert len(calls) == 3

    # A fresh instance (e.g. after restart) must use the disk cache, no network.
    monkeypatch.setattr(option_chain.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert option_chain.OptionChain()._load_instruments() == [{"name": "NIFTY"}]
