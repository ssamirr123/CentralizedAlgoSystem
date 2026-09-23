"""Phase 11 (Section 4) -- OPTIONS_INTELLIGENCE_ENABLED API-level tests."""
from __future__ import annotations


def test_options_intelligence_flag_independent_of_ai_flags(client, viewer_auth, monkeypatch):
    """Options Intelligence (Phase 9) has no LLM cost and must be
    controllable independently of every AI_* flag."""
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)  # AI fully off
    monkeypatch.setenv("OPTIONS_INTELLIGENCE_ENABLED", "false")
    r = client.get("/api/options/expiries?underlying=NIFTY", headers=viewer_auth)
    assert r.status_code == 503


def test_options_intelligence_defaults_enabled(client, viewer_auth, monkeypatch):
    monkeypatch.delenv("OPTIONS_INTELLIGENCE_ENABLED", raising=False)
    r = client.get("/api/options/expiries?underlying=NIFTY", headers=viewer_auth)
    # 404 (no expiries loaded in this bare test fixture) proves the request
    # reached real handler logic, not the 503 disabled-gate.
    assert r.status_code != 503
