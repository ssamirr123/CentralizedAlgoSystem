"""Phase 14: trading/preflight/live_canary.py -- the LIVE_CANARY final
preflight command. Every test here runs with real Angel credentials
cleared (tests/conftest.py does this for every test), so any accidental
real-account call would fail loudly rather than silently succeed; the one
"full PASS" test explicitly mocks the real-account validation step
instead of relying on real credentials being present or absent.
"""
from __future__ import annotations

from trading.preflight.live_canary import run_preflight


def _set_canary_env(monkeypatch, **overrides):
    values = {
        "CANARY_ACCOUNT_ID": "ANGEL_CANARY",
        "CANARY_MAX_ORDER_QUANTITY": "1",
        "CANARY_MAX_ORDER_VALUE": "200",
        "CANARY_MAX_DAILY_LOSS": "500",
        "CANARY_MAX_STRATEGY_LOSS": "500",
        "CANARY_MAX_ORDERS_PER_DAY": "3",
        "BROKER": "angelone",
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _status(report, name: str) -> str:
    return next(r.status for r in report.results if r.name == name)


# --------------------------------------------------------------------------- #
# Fail-closed defaults
# --------------------------------------------------------------------------- #
def test_unconfigured_environment_fails_every_canary_check():
    report = run_preflight()
    assert report.phase_status == "FAIL"
    assert _status(report, "CONFIGURATION") == "FAIL"
    assert _status(report, "DEDICATED_ACCOUNT") == "FAIL"


def test_no_automatic_order_placement_check_always_passes():
    """This check doesn't depend on configuration -- it's a structural
    fact about the preflight tool's own source, true regardless."""
    report = run_preflight()
    assert _status(report, "NO AUTOMATIC ORDER PLACEMENT") == "PASS"


def test_broker_is_sole_order_caller_check_always_passes():
    report = run_preflight()
    assert _status(report, "BROKER ADAPTER IS SOLE ORDER CALLER") == "PASS"


def test_observability_and_backend_checks_pass_without_any_canary_config():
    report = run_preflight()
    assert _status(report, "OBSERVABILITY WIRED") == "PASS"
    assert _status(report, "CONTROL CENTER BACKEND IMPORTABLE") == "PASS"


# --------------------------------------------------------------------------- #
# Configuration validation
# --------------------------------------------------------------------------- #
def test_partial_configuration_still_fails(monkeypatch):
    monkeypatch.setenv("CANARY_ACCOUNT_ID", "ANGEL_CANARY")
    monkeypatch.delenv("CANARY_MAX_ORDER_QUANTITY", raising=False)
    report = run_preflight()
    assert _status(report, "CONFIGURATION") == "FAIL"


def test_non_positive_limit_fails_configuration(monkeypatch):
    _set_canary_env(monkeypatch, CANARY_MAX_ORDER_QUANTITY="0")
    report = run_preflight()
    assert _status(report, "CONFIGURATION") == "FAIL"


def test_full_configuration_passes(monkeypatch):
    _set_canary_env(monkeypatch)
    report = run_preflight()
    assert _status(report, "CONFIGURATION") == "PASS"


# --------------------------------------------------------------------------- #
# Dedicated account
# --------------------------------------------------------------------------- #
def test_shared_example_account_fails_dedicated_account_check(monkeypatch):
    _set_canary_env(monkeypatch, CANARY_ACCOUNT_ID="ANGEL_MAIN")
    report = run_preflight()
    assert _status(report, "DEDICATED_ACCOUNT") == "FAIL"


def test_a_distinct_account_id_passes_dedicated_account_check(monkeypatch):
    _set_canary_env(monkeypatch)
    report = run_preflight()
    assert _status(report, "DEDICATED_ACCOUNT") == "PASS"


# --------------------------------------------------------------------------- #
# Broker adapter validation status
# --------------------------------------------------------------------------- #
def test_dhan_broker_fails_adapter_validated_check(monkeypatch):
    _set_canary_env(monkeypatch, BROKER="dhan")
    report = run_preflight()
    assert _status(report, "BROKER ADAPTER VALIDATED") == "FAIL"
    assert _status(report, "REAL ACCOUNT VALIDATION") == "FAIL"


def test_icici_breeze_broker_fails_adapter_validated_check(monkeypatch):
    _set_canary_env(monkeypatch, BROKER="icici_breeze")
    report = run_preflight()
    assert _status(report, "BROKER ADAPTER VALIDATED") == "FAIL"


def test_angelone_broker_passes_adapter_validated_check(monkeypatch):
    _set_canary_env(monkeypatch)
    report = run_preflight()
    assert _status(report, "BROKER ADAPTER VALIDATED") == "PASS"


def test_unset_broker_fails_adapter_validated_check(monkeypatch):
    _set_canary_env(monkeypatch, BROKER=None)
    report = run_preflight()
    assert _status(report, "BROKER ADAPTER VALIDATED") == "FAIL"


# --------------------------------------------------------------------------- #
# Real account validation -- cleared credentials means an honest FAIL,
# never a silent skip pretending to be a PASS.
# --------------------------------------------------------------------------- #
def test_real_account_validation_fails_without_credentials(monkeypatch):
    _set_canary_env(monkeypatch)  # tests/conftest.py already clears ANGELONE_* for every test
    report = run_preflight()
    assert _status(report, "REAL ACCOUNT VALIDATION") == "FAIL"
    assert report.phase_status == "FAIL"


def test_real_account_validation_pass_is_required_for_overall_pass(monkeypatch):
    """Mocks trading.validation.angel_readonly.run_validation() to return
    a canned PASS -- proves the preflight tool's OWN logic reaches overall
    PASS once every check passes, without needing real credentials or
    network access in this test."""
    import trading.validation.angel_readonly as angel_readonly

    class _FakeReport:
        phase_status = "PASS"
        results = []

    monkeypatch.setattr(angel_readonly, "run_validation", lambda: _FakeReport())
    _set_canary_env(monkeypatch)

    report = run_preflight()
    assert _status(report, "REAL ACCOUNT VALIDATION") == "PASS"
    assert report.phase_status == "PASS"


# --------------------------------------------------------------------------- #
# Canary guard / risk manager dry runs
# --------------------------------------------------------------------------- #
def test_risk_manager_and_canary_guard_dry_runs_pass_with_sane_limits(monkeypatch):
    _set_canary_env(monkeypatch)
    report = run_preflight()
    assert _status(report, "RISK MANAGER") == "PASS"
    assert _status(report, "CANARY GUARD DRY RUN") == "PASS"
    assert _status(report, "KILL SWITCH") == "PASS"
    assert _status(report, "EMERGENCY SHUTDOWN") == "PASS"


def test_dry_run_checks_are_skipped_not_silently_passed_without_configuration():
    report = run_preflight()
    assert _status(report, "RISK MANAGER") == "FAIL"
    assert _status(report, "CANARY GUARD DRY RUN") == "FAIL"
    assert _status(report, "KILL SWITCH") == "FAIL"
    assert _status(report, "EMERGENCY SHUTDOWN") == "FAIL"


# --------------------------------------------------------------------------- #
# main() exit codes
# --------------------------------------------------------------------------- #
def test_main_exits_nonzero_when_unconfigured(capsys):
    from trading.preflight.live_canary import main

    code = main([])
    assert code != 0
    out = capsys.readouterr().out
    assert "PHASE 14 = FAIL" in out


def test_main_exits_zero_when_fully_configured_and_mocked_pass(monkeypatch, capsys):
    import trading.validation.angel_readonly as angel_readonly
    from trading.preflight.live_canary import main

    class _FakeReport:
        phase_status = "PASS"
        results = []

    monkeypatch.setattr(angel_readonly, "run_validation", lambda: _FakeReport())
    _set_canary_env(monkeypatch)

    code = main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "PHASE 14 = PASS" in out
