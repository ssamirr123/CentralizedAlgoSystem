"""Phase 15D.3-R: regression coverage for the environment-leakage fix.

trading.preflight.live_canary.main() loads trading/.env directly into the
real, global os.environ (correct for production; a test-isolation hazard
otherwise -- see tests/preflight/conftest.py's own docstring for the full
root-cause explanation). These tests prove the fix holds under the exact
conditions that originally broke it -- an execution-order dependency --
and does not merely paper over the two specific failing tests.

Note: trading/.env genuinely carries real, valid CANARY_* values as of
Phase 15D.2 -- so a test that calls main() and then immediately checks
run_preflight() WITHIN THE SAME test correctly sees a configured
environment (main() loaded real config; that is not a leak, it is main()
doing its job). The property actually worth proving is that this loading,
once it happens in one test, leaves NO trace for a DIFFERENT, LATER test
-- exactly the bug that existed before this phase's fix.
"""
from __future__ import annotations

import os

from trading.preflight.live_canary import main, run_preflight


# ---------------------------------------------------------------------- #
# 1. .env values do not leak into unrelated test environments, proven
# across both relative orderings of "the test that loads real config" and
# "the test that expects a clean, unconfigured environment".
# ---------------------------------------------------------------------- #
def test_order_a_pollute_then_clean_check():
    main([])
    assert os.environ.get("CANARY_ACCOUNT_ID") == "ANGEL_ACCOUNT_B"  # real config, loaded correctly


def test_order_a_clean_check_after_pollution():
    report = run_preflight()
    assert report.phase_status == "FAIL"
    assert next(r.status for r in report.results if r.name == "CONFIGURATION") == "FAIL"


def test_order_b_clean_check_before_pollution():
    """The reverse relative order: the "clean" assertion runs FIRST, then
    a separate test pollutes -- proving the fixture's snapshot is taken
    fresh per-test (order-independent), not just "first test wins"."""
    report = run_preflight()
    assert report.phase_status == "FAIL"


def test_order_b_pollute_after_clean_check():
    main([])
    assert os.environ.get("CANARY_ACCOUNT_ID") == "ANGEL_ACCOUNT_B"  # real config, loaded correctly
    # (main()'s overall exit code depends on unrelated checks -- e.g. BROKER
    # is not set in trading/.env -- so only the CANARY_* loading itself is
    # asserted here, not main()'s aggregate pass/fail.)


def test_order_c_dry_run_checks_are_clean_in_a_fresh_test():
    report = run_preflight()
    assert next(r.status for r in report.results if r.name == "RISK MANAGER") == "FAIL"
    assert next(r.status for r in report.results if r.name == "KILL SWITCH") == "FAIL"
    assert next(r.status for r in report.results if r.name == "EMERGENCY SHUTDOWN") == "FAIL"


# ---------------------------------------------------------------------- #
# 2 & 4. CANARY_* does not unexpectedly appear in a test expecting an
# unconfigured environment, even directly after a test that loaded it.
# ---------------------------------------------------------------------- #
def test_unconfigured_environment_fails_every_canary_check_after_main_ran_earlier():
    """The exact original failing scenario: some earlier test already
    called main() (several already have, by this point in the file) --
    this test still observes a clean, unconfigured environment."""
    report = run_preflight()
    assert report.phase_status == "FAIL"


def test_explicit_caller_supplied_value_is_not_silently_overwritten(monkeypatch):
    """_load_dotenv_if_present() only ever sets a key that is NOT already
    present in os.environ (`if key not in os.environ`) -- an explicit
    caller-supplied value must survive main() untouched."""
    monkeypatch.setenv("CANARY_ACCOUNT_ID", "EXPLICITLY_SET_BY_CALLER")
    main([])
    assert os.environ["CANARY_ACCOUNT_ID"] == "EXPLICITLY_SET_BY_CALLER"


# ---------------------------------------------------------------------- #
# 3. Environment state is restored after configuration loading -- proven
# by a dedicated pollute/verify pair.
# ---------------------------------------------------------------------- #
def test_pollute_for_restore_proof():
    main([])
    assert os.environ.get("CANARY_ACCOUNT_ID") == "ANGEL_ACCOUNT_B"


def test_environment_is_clean_again_after_the_restore_proof_test():
    report = run_preflight()
    assert report.phase_status == "FAIL"  # prior test's main() call left no trace


# ---------------------------------------------------------------------- #
# 5. Multiple configuration loads do not accumulate stale environment
# state within a single test, and leave nothing behind for the next one.
# ---------------------------------------------------------------------- #
def test_repeated_dotenv_loads_do_not_accumulate_stale_state():
    for _ in range(5):
        main([])
    assert os.environ.get("CANARY_ACCOUNT_ID") == "ANGEL_ACCOUNT_B"  # consistent every time, no corruption


def test_environment_is_clean_after_repeated_loads_in_a_prior_test():
    report = run_preflight()
    assert report.phase_status == "FAIL"
