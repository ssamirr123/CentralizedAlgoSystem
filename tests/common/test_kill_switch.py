"""Phase 14.6: trading/common/kill_switch.py's CentralKillSwitch, in
isolation. See tests/common/test_execution_central_kill_switch.py for the
StrategyExecutionEngine.execute() wiring proof."""
from __future__ import annotations

from trading.common.kill_switch import CentralKillSwitch


def test_starts_disengaged():
    ks = CentralKillSwitch()
    assert ks.engaged is False
    assert ks.engaged_by == ""
    assert ks.reason == ""


def test_engage_sets_all_fields():
    ks = CentralKillSwitch()
    ks.engage(by="user:1", reason="manual halt")
    assert ks.engaged is True
    assert ks.engaged_by == "user:1"
    assert ks.reason == "manual halt"
    assert ks.engaged_at != ""


def test_disengage_clears_engaged_and_reason_but_keeps_history():
    ks = CentralKillSwitch()
    ks.engage(by="user:1", reason="halt")
    ks.disengage(by="user:2")
    assert ks.engaged is False
    assert ks.engaged_by == "user:2"
    assert ks.reason == ""
    assert ks.disengaged_at != ""
    assert ks.engaged_at != ""  # history of the prior engagement is kept


def test_disengage_does_not_grant_any_capability():
    """Disengaging is purely the ABSENCE of this one block -- it must
    never itself be mistaken for an authorization grant. There is no
    method on this class that could plausibly do that (no `authorize()`,
    no side effect beyond its own three fields) -- this test pins that
    fact structurally."""
    ks = CentralKillSwitch()
    assert not hasattr(ks, "authorize")
    assert not hasattr(ks, "allow")
    ks.disengage()
    assert ks.engaged is False  # merely "not blocking", nothing more


def test_repeated_engage_updates_reason_and_actor():
    ks = CentralKillSwitch()
    ks.engage(by="user:1", reason="first")
    ks.engage(by="user:2", reason="second")
    assert ks.engaged_by == "user:2"
    assert ks.reason == "second"
