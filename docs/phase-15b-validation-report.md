# Phase 15B — Validation Report

## Tests

```
Total collected: 1198
Passed:          1195
Failed:          3   (pre-existing baseline, unrelated to Phase 15B -- see below)
Skipped:         0
```

New Phase 15B test files (49 tests, all passing):

- `tests/common/test_broker_types.py` (8)
- `tests/common/test_credentials.py` (10)
- `tests/common/test_broker_adapter_factory.py` (6)
- `tests/common/test_account_router.py` (7)
- `tests/common/test_account_authorization_state.py` (8)
- `tests/common/test_risk_manager_account_isolation.py` (4)
- `tests/common/test_phase_15b_no_live_orders.py` (4, plus 2 covered above)

### The 3 pre-existing failures (baseline, not introduced by this phase)

```
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[CombinedVwapNifty]
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[DoubleStraddelAlgo]
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[Vwap_Algo_Nifty_hedge]
```

Root cause (verified, not assumed): each algo's own `config.py` calls its own `load_dotenv()` at import time, which re-reads the real `trading/.env` file present in this local development environment (containing this project's real Angel One credentials) and repopulates `ANGELONE_CLIENT_ID` etc. *after* the test's `monkeypatch.delenv(...)` has already cleared them for that test — so the test observes the real client ID (masked here, never printed in full) instead of an empty string. This is an environment-specific interaction between a real, local `.env` file and this test's assumption that env vars stay unset once deleted; it would not reproduce in an environment with no `trading/.env` file (e.g. CI). Confirmed via `git diff` that `tests/security/test_config_fails_safe.py` was not touched this session, and no Phase 15B code was involved in the traceback. Per this phase's own rules ("do not bypass/weaken a failing test to make it pass," carried over from the Phase 14 investigation precedent), this was left exactly as found and reported honestly rather than silently worked around.

## Real accounts tested

Zero real broker accounts were exercised this phase. All credential-isolation and account-routing proofs use synthetic environment variables (e.g. `ANGELONE_SAMIR_*`, `ANGELONE_WIFE_*`, set only inside test fixtures and cleaned up automatically by `monkeypatch`) or fully in-memory fake `BrokerClient` doubles. No real secret, password, PIN, TOTP, or access token was requested from, or supplied by, the user.

## Broker adapters tested

Angel One, Dhan, and ICICI Breeze adapters were each constructed through the new `create_broker_for_account()` path and proven to (a) receive independently-resolved credentials and (b) reject every mutating call (`place_order`/`cancel_order`) with `ReadOnlyModeError`, using each adapter's own existing, unmodified internal logic. Zerodha was routed through the same factory for structural completeness; its `connect()` remains an unimplemented stub exactly as before this phase.

## Order APIs called

Zero. `place_order`, `modify_order`, `cancel_order` were called only from within tests that assert they raise `ReadOnlyModeError` (or, in `_FakeBroker`, that assert they must never be reached at all via `AssertionError`).

## Real orders placed

**0.**

## Security findings

- No new code path logs, prints, or exposes a resolved credential value (`AccountCredentials`, `BrokerCredentials`). Reviewed by inspection of every new module.
- `credential_reference` remains a non-secret pointer; the resolver rejects any reference that isn't the `env:<PREFIX>` scheme rather than silently treating an arbitrary string as a lookup key.
- Owner isolation (`TradingAccountRouter`) is proven, not merely asserted: `AccountAccessDeniedError` is raised when an `owner_context` doesn't match the resolved account's `owner_id`.

## Risk findings

- Confirmed a real, pre-existing gap: before this phase, `RiskManager` held exactly one global `RiskLimits` for every account it served — a limit configured for one account (e.g. a wife's smaller account) would have applied identically to every other account (e.g. a much larger primary account), or vice versa. Closed via `RiskManager.set_account_limits()` / `_limits_for()`, with a regression test (`test_risk_manager_account_isolation.py`) proving no leakage between two accounts.
- `StrategyExecutionEngine.execute()` did not yet consult the new `TradingAccount.authorization_state` at the time of this report — **closed in Phase 15B.1**, see `docs/phase-15b-1-authorization-enforcement-report.md`.

## Known limitations

1. ~~`StrategyExecutionEngine.execute()` does not consult `authorization_state` yet~~ — **closed in Phase 15B.1** (see `docs/phase-15b-1-authorization-enforcement-report.md`): a new hard gate now enforces it immediately before `RiskManager.validate()`.
2. Credential isolation is validated with synthetic env vars only — no second real broker account exists in this environment to validate against.
3. `ZerodhaKiteBroker` remains an unimplemented stub.
4. `AccountState.trades` is always empty (no adapter exposes a distinct trades endpoint).
5. `BrokerCapabilities` is documentation-level only; not yet enforced by `BrokerAdapterFactory`.
6. The 3 pre-existing `test_config_fails_safe.py` failures (environment-specific, see above) remain unresolved, per this phase's instruction not to modify unrelated code to force tests green.

PHASE 15B STATUS:
PASS
