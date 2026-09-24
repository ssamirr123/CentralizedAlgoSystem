"""Phase 11 (Section 4/5) -- independent feature-flag and AI-workload
kill-switch tests. Every flag must fail OPEN (default = prior behavior)
except the original AI_RESEARCH_ENABLED master switch itself, which has
always failed CLOSED (default disabled) and must continue to."""
from __future__ import annotations

import pytest

from trading.ai_research import service as ai_research_service
from trading.ai_research.backtest import service as backtest_service
from trading.ai_options_research import service as options_research_service


def test_master_switch_still_fails_closed(monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    assert ai_research_service.is_enabled() is False
    assert backtest_service.is_enabled() is False
    assert options_research_service.is_enabled() is False


def test_default_behavior_unchanged_when_only_master_switch_set(monkeypatch):
    """Backward compatibility: a deployment that only ever set
    AI_RESEARCH_ENABLED=true (every prior phase's own tests do exactly
    this) must see ALL THREE still enabled after Phase 11."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.delenv("AI_WORKLOADS_ENABLED", raising=False)
    monkeypatch.delenv("AI_BACKTEST_ENABLED", raising=False)
    monkeypatch.delenv("AI_OPTIONS_RESEARCH_ENABLED", raising=False)
    assert ai_research_service.is_enabled() is True
    assert backtest_service.is_enabled() is True
    assert options_research_service.is_enabled() is True


def test_ai_workloads_kill_switch_disables_all_three(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_WORKLOADS_ENABLED", "false")
    assert ai_research_service.is_enabled() is False
    assert backtest_service.is_enabled() is False
    assert options_research_service.is_enabled() is False


def test_ai_workloads_kill_switch_is_a_different_env_var_than_trading_mode(monkeypatch):
    """Section 5: this must NOT be the trading kill switch -- flipping it
    must not touch TRADING_MODE/is_live."""
    from trading.core.config import load_settings

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("AI_WORKLOADS_ENABLED", "false")
    settings = load_settings()
    assert settings.is_live is True  # completely unaffected by the AI kill switch
    monkeypatch.setenv("TRADING_MODE", "paper")


def test_backtest_flag_only_disables_backtest(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_BACKTEST_ENABLED", "false")
    assert ai_research_service.is_enabled() is True
    assert backtest_service.is_enabled() is False
    assert options_research_service.is_enabled() is True


def test_options_research_flag_only_disables_options_research(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_OPTIONS_RESEARCH_ENABLED", "false")
    assert ai_research_service.is_enabled() is True
    assert backtest_service.is_enabled() is True
    assert options_research_service.is_enabled() is False



# API-level tests for OPTIONS_INTELLIGENCE_ENABLED live in
# tests/api/test_options_intelligence_flag.py (they need the client/
# viewer_auth fixtures, which are scoped to tests/api/).
