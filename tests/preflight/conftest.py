"""
Phase 15D.3-R: environment isolation for trading/preflight/live_canary.py's
own test suite.

`trading.preflight.live_canary.main()` calls `_load_dotenv_if_present()`,
which -- correctly, for production use -- mutates the real, global
`os.environ` directly from `trading/.env` with no cleanup (a real process
wants its configuration to persist for its whole lifetime). That is
NOT a bug in production, but it IS a test-isolation hazard: any test in
this file that calls `main()` permanently leaks whatever `trading/.env`
happens to contain into the rest of the pytest process, silently changing
what every LATER test (in this file or, since os.environ is truly
process-global, any other file collected afterward) observes as "the
environment."

This was purely latent and invisible as long as `trading/.env` had no
`CANARY_*` keys. It became a real, reproducible failure once a legitimate,
human-authorized live-canary run added real `CANARY_*` values to that
file (Phase 15D.2) -- `test_unconfigured_environment_fails_every_canary_
check` and `test_dry_run_checks_are_skipped_not_silently_passed_without_
configuration` then incorrectly observed a "configured" environment when
run after `test_main_exits_nonzero_when_unconfigured` in the same process.

Fix: snapshot the full environment before every test in this directory
and restore it exactly afterward, regardless of what the test (or any
code it calls) did to `os.environ`. This is deliberately NOT a change to
`_load_dotenv_if_present()`'s production behavior -- a real deployment
still wants persistent, process-lifetime environment mutation from its
own `.env` file; only the TEST boundary needs isolation.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
