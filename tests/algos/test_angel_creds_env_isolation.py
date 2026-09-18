"""Phase 15D.3-R: regression coverage for the `_angel_creds()` environment-
leak fix.

Root cause (see docs/phase-15d-3-post-canary-verification-report.md):
`_angel_creds()` in DoubleStraddelAlgo/CombinedVwapNifty/Vwap_Algo_Nifty_
hedge's own config.py used to read trading/.env and call
`os.environ.setdefault(key, value)` for EVERY line in the file -- not just
the Angel One credential keys it actually consumes. Since `trading/.env`
is a REAL, git-ignored file that (as of Phase 15D.2) carries real
`CANARY_*`/`ANGELONE_A_*`/`ANGELONE_B_*` values, importing any of these
three config modules permanently leaked those unrelated keys into
process-global `os.environ` for the rest of whatever process imported
them -- silently breaking `tests/preflight`'s own "environment must be
unconfigured" tests whenever `tests/algos/test_doublestraddel_execution_
bridge.py` (which imports DoubleStraddelAlgo's config) was collected in
the same pytest run.

These tests run against the REAL `trading/.env` file (no path mocking) --
that file genuinely exists in this repo checkout and is exactly the
scenario that broke before. They assert the NEGATIVE property (unrelated
keys never appear) rather than mocking the file's content, since mocking
would test a hypothetical `.env` rather than the actual one that caused
the incident.
"""
from __future__ import annotations

import trading.algos.CombinedVwapNifty.config as combined_vwap_config
import trading.algos.DoubleStraddelAlgo.config as double_straddel_config
import trading.algos.Vwap_Algo_Nifty_hedge.config as vwap_hedge_config

_UNRELATED_PREFIXES = ("CANARY_", "ANGELONE_A_", "ANGELONE_B_")
_REQUIRED_KEYS = ("ANGELONE_CLIENT_ID", "ANGELONE_API_KEY", "ANGELONE_MPIN", "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET")

_MODULES = {
    "DoubleStraddelAlgo": double_straddel_config,
    "CombinedVwapNifty": combined_vwap_config,
    "Vwap_Algo_Nifty_hedge": vwap_hedge_config,
}


def _clear_all_related_env(monkeypatch):
    for key in _REQUIRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    import os

    for key in list(os.environ):
        if key.startswith(_UNRELATED_PREFIXES):
            monkeypatch.delenv(key, raising=False)


def _assert_no_unrelated_leak(after_env):
    leaked = [k for k in after_env if k.startswith(_UNRELATED_PREFIXES)]
    assert leaked == [], f"_angel_creds() leaked unrelated keys into os.environ: {leaked}"


class TestAngelCredsDoesNotLeakUnrelatedConfig:
    """Covers all three modules that share the identical _angel_creds()
    implementation -- the exact fix applied to each."""

    def test_double_straddel_algo_no_leak(self, monkeypatch):
        _clear_all_related_env(monkeypatch)
        double_straddel_config._angel_creds()
        import os

        _assert_no_unrelated_leak(os.environ)

    def test_combined_vwap_nifty_no_leak(self, monkeypatch):
        _clear_all_related_env(monkeypatch)
        combined_vwap_config._angel_creds()
        import os

        _assert_no_unrelated_leak(os.environ)

    def test_vwap_algo_nifty_hedge_no_leak(self, monkeypatch):
        _clear_all_related_env(monkeypatch)
        vwap_hedge_config._angel_creds()
        import os

        _assert_no_unrelated_leak(os.environ)

    def test_required_credentials_still_resolve_when_present(self, monkeypatch):
        """The fix must not break legitimate credential resolution --
        setting the 4 (5, counting the MPIN/PASSWORD alias) real keys
        explicitly in os.environ (simulating what a real deployment does,
        or what trading/.env genuinely provides) must still flow through
        to the returned dict correctly."""
        monkeypatch.setenv("ANGELONE_CLIENT_ID", "AA1234")
        monkeypatch.setenv("ANGELONE_API_KEY", "test-key")
        monkeypatch.setenv("ANGELONE_MPIN", "1234")
        monkeypatch.setenv("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

        creds = double_straddel_config._angel_creds()
        assert creds["clientid"] == "AA1234"
        assert creds["apikey"] == "test-key"
        assert creds["mpin"] == "1234"
        assert creds["token"] == "JBSWY3DPEHPK3PXP"

    def test_mpin_password_alias_still_works(self, monkeypatch):
        monkeypatch.delenv("ANGELONE_MPIN", raising=False)
        monkeypatch.setenv("ANGELONE_PASSWORD", "5678")
        creds = double_straddel_config._angel_creds()
        assert creds["mpin"] == "5678"

    def test_explicit_caller_value_is_not_overwritten_by_dotenv_fallback(self, monkeypatch):
        """setdefault() semantics: an explicitly-set env var must survive
        _angel_creds() untouched, exactly like before this fix -- only the
        SCOPE of what gets defaulted from the file changed, not the
        never-overwrite guarantee."""
        monkeypatch.setenv("ANGELONE_CLIENT_ID", "EXPLICIT_VALUE")
        double_straddel_config._angel_creds()
        import os

        assert os.environ["ANGELONE_CLIENT_ID"] == "EXPLICIT_VALUE"

    def test_a_previously_present_unrelated_key_is_left_untouched(self, monkeypatch):
        """This fix must not actively DELETE a CANARY_*-style key that was
        already legitimately present before the call -- it only stops
        introducing NEW ones. (Distinguishes "never sets" from "actively
        clears", which would be a different, unwanted behavior change.)"""
        monkeypatch.setenv("CANARY_ACCOUNT_ID", "PRE_EXISTING_VALUE")
        double_straddel_config._angel_creds()
        import os

        assert os.environ["CANARY_ACCOUNT_ID"] == "PRE_EXISTING_VALUE"


def test_importing_the_module_directly_does_not_leak_either(monkeypatch):
    """The end-to-end scenario that actually broke tests/preflight: merely
    IMPORTING one of these config modules (triggering its module-level
    `_ANGEL = _angel_creds()`) must not leak unrelated trading/.env keys.
    Re-invokes _angel_creds() directly (re-importing an already-imported
    module is a no-op in Python) to exercise the exact same code path the
    module-level statement already ran once at first import."""
    _clear_all_related_env(monkeypatch)
    double_straddel_config._angel_creds()
    import os

    _assert_no_unrelated_leak(os.environ)
