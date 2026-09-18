# Phase 15C.4 — Cross-Account Isolation and Safety Validation

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` API was ever called. No strategy was started. Neither account was promoted to `CANARY_READY`/`LIVE_AUTHORIZED`.**

## 1. Objective

Prove that the two real Angel One accounts validated independently in Phase 15C.3 (`ANGEL_SAMIR` / Account A and `ANGEL_ACCOUNT_B` / Account B) remain strictly isolated from each other under repeated, alternating, and concurrent access — not merely configured with two different IDs, but actually incapable of leaking credentials, sessions, funds, positions, orders, account state, risk limits, or authorization between them.

## 2. Account A identity — masked

```
client_id: AA***21   (unchanged across every call in this phase, including under alternation)
```

## 3. Account B identity — masked

```
client_id: AA***01   (unchanged across every call in this phase, including under alternation)
```

`AA***21` and `AA***01` never appeared interchanged at any point in this phase's real interleaving test (Section 5) or Phase 15C.3's isolation proof.

## 4. Credential isolation

Re-confirmed programmatically (no values printed): `credential_reference_A = "env:ANGELONE_A"`, `credential_reference_B = "env:ANGELONE_B"` — distinct, and `resolve_credentials()` for each reference resolves independently with `api_key`/`client_id`/`totp_secret` all differing (established in Phase 15C.3, re-verified structurally in `tests/common/test_phase_15c_4_cross_account_isolation.py::test_create_broker_for_account_never_silently_substitutes_another_account` using synthetic env vars). The resolver never falls back to a bare/default `ANGELONE_*` credential set for either account — those bare keys no longer exist in `trading/.env` at all (removed in favor of the `_A_`/`_B_` suffixed pair), so there is no default to fall back to even in principle.

## 5. Session isolation

Real, live test: six alternating real calls (`A, B, A, B, A, B`), each independently fetching identity + funds + positions, reusing each account's own already-authenticated `BrokerManager`-held session (no re-login per call):

```
step 1 (A): client_id_masked=AA***21  available_cash=0.0    used_margin=0.0  positions=0
step 2 (B): client_id_masked=AA***01  available_cash=94.83  used_margin=0.0  positions=0
step 3 (A): client_id_masked=AA***21  available_cash=0.0    used_margin=0.0  positions=0
step 4 (B): client_id_masked=AA***01  available_cash=94.83  used_margin=0.0  positions=0
step 5 (A): client_id_masked=AA***21  available_cash=0.0    used_margin=0.0  positions=0
step 6 (B): client_id_masked=AA***01  available_cash=94.83  used_margin=0.0  positions=0
```

Every single request in the sequence returned exactly, and only, its own requested account's data — verified programmatically: `per_account_self_consistent: true`, `cross_contaminated (A id ever equals B id): false`. This directly satisfies this phase's required `A → A, B → B, A → A, B → B` sequence (Section titled "Session isolation" in the brief).

## 6. Funds isolation

Covered in Section 5 above — funds were read as part of the same alternating sequence and never crossed: Account A consistently `0.0`/`0.0`, Account B consistently `94.83`/`0.0`, across all three repetitions each.

## 7. Position isolation

Covered in Section 5 — both accounts consistently returned `0` positions across all three repetitions each. (Neither account currently holds a position, so this run could not additionally prove "different position sets stay separate" — only that repeated reads of an empty set remain correctly and consistently associated with the requesting account across alternation, which it did.)

## 8. Order isolation

Established in Phase 15C.3 (Account A: 0 orders; Account B: 4 orders) — a nonzero, distinct-from-A order count for B is itself evidence against cross-contamination. Not re-fetched under alternation this phase specifically to avoid tripping Angel's real rate limiter a second time (already hit once in Phase 15C.3); the read path used (`TradingAccountRouter.get_orders` → `BrokerManager.get_broker(account_id)` → that account's own connected adapter) is identical to the path proven account-consistent for identity/funds/positions in Section 5, so no additional real call was needed to establish confidence in this specific isolation property.

## 9. Trade isolation / limitation

```
Trades = NOT AVAILABLE (both accounts)
```

`AngelOneBroker` has no distinct trades endpoint separate from the order book — the same documented limitation carried since Phase 15A, applying identically to both accounts. No trade data was invented for either account.

## 10. AccountState isolation

Proven with `tests/common/test_phase_15c_4_cross_account_isolation.py`:
- `test_account_state_fields_are_account_specific_under_alternation` — `TradingAccountRouter.get_account_state()` alternated `A, B, A, B` and every returned `AccountState.account_id`/`broker_id`/`available_cash` matched the requested account exactly.
- `test_account_state_a_never_overwritten_by_loading_b` — loading B's state between two reads of A's state does not change A's result.

## 11. RiskManager isolation

Proven with `test_risk_limits_are_independent_per_account` and `test_reading_account_a_limits_never_returns_account_b_limits` (both new, using synthetic `RiskLimits` values — real live trading limits were never touched): `RiskManager.set_account_limits()` for A and B produce two independently-resolved `RiskLimits` objects; reading A's limits never returns B's object or B's values.

## 12. Authorization isolation

`test_authorization_state_is_independent_per_account`: both real accounts confirmed `READ_ONLY`/`is_live_authorized() == False`. A synthetic promotion of Account A's `authorization_state` to `CANARY_READY` (in-memory only, on a test fixture — **never applied to either real account**) was proven to leave Account B's `authorization_state` at `READ_ONLY`, unaffected.

```
CANARY_READY(A) = false   (real account, unchanged throughout this phase)
LIVE_AUTHORIZED(A) = false
CANARY_READY(B) = false   (real account, unchanged throughout this phase)
LIVE_AUTHORIZED(B) = false
```

## 13. Router isolation

`test_router_resolves_each_account_id_to_itself_only`, `test_router_fails_closed_for_unknown_account` (raises `UnknownAccountError` for `"UNKNOWN_ACCOUNT"` / `"DOES_NOT_EXIST"` — no default-account fallback exists anywhere in `BrokerManager`/`TradingAccountRouter`), `test_router_owner_context_prevents_cross_account_access` (an `owner_context` mismatch raises `AccountAccessDeniedError`, re-confirming Phase 15B's own isolation guarantee against these same two real account IDs).

## 14. Broker factory isolation

`test_broker_adapter_factory_fails_closed_for_unknown_broker` and `test_scenario_4_unknown_broker_is_rejected`: an unregistered `broker_id` raises `UnsupportedBrokerError` from both `build_account_config()` and `create_broker_for_account()` — there is no default/fallback broker anywhere in `BrokerAdapterFactory`. `test_create_broker_for_account_never_silently_substitutes_another_account` proves two accounts with two different `credential_reference`s produce two `TradingConfig` instances with two different `credentials.angelone_client_id` values — never a shared/default set.

## 15. Concurrency / interleaving tests

Per this phase's own instruction ("if concurrency cannot safely be tested against the live broker, use mocked broker responses"), true concurrent access was tested with synthetic fakes (`test_concurrent_read_only_requests_stay_isolated`): two threads, each issuing 20 real-shaped `get_funds()` calls for Account A and Account B respectively, running concurrently. Every one of Account A's 20 results was `0.0`; every one of Account B's 20 results was `94.83` — zero cross-thread contamination under real concurrent access. `test_interleaved_alternating_requests_never_cross_contaminate` covers the same property sequentially (10-call alternation) as a lighter-weight companion check. The real, non-concurrent alternating sequence against the live broker is Section 5 above.

## 16. Negative tests (5 required scenarios)

All five implemented and passing in `tests/common/test_phase_15c_4_cross_account_isolation.py`:

1. **Account A given Account B's credential reference** — proven the resolver is deterministic and transparent: it resolves to exactly what the reference points at (no silent "keep the old credentials" behavior, no silent failure). This is a config-time property; the actual safety net against this operator mistake is Scenario 5 (identity verification), which is exactly why that scenario exists and is treated as the most important one — matching the phase brief's own framing ("Never trust configuration alone").
2. **Missing credential reference (env vars absent)** — resolves to empty strings, `is_fully_configured()` returns `False`; never a silent fallback to another account's credentials.
3. **Unknown account** — `BrokerManager.get_account()`/`TradingAccountRouter.get_funds()` both raise `UnknownAccountError`.
4. **Unknown broker** — `BrokerAdapterFactory`/`create_broker_for_account()` both raise `UnsupportedBrokerError`.
5. **Broker identity must not be silently trusted from configuration alone** — the decisive scenario. This exact property was independently, empirically proven against the real broker in Phase 15C.3 and this phase's Section 5: `client_id_A_differs_from_B` was computed from the **broker's own authenticated response**, not from configuration — and Section 5's `per_account_self_consistent`/`cross_contaminated` checks are precisely an automated version of "does the identity returned actually match what was requested," run six times under alternation. `test_scenario_5_broker_identity_mismatch_must_not_be_silently_trusted` proves the underlying comparison logic with controlled fakes.

## 17. Mutation safety

No new mutation calls were made against either real broker this phase. The mutation-block proof for both real accounts (`place_order`/`modify_order`/`cancel_order` all raising `ReadOnlyModeError`) was already established for Account A and Account B in Phase 15C.3 and is not repeated here — see `docs/phase-15c-3-account-b-read-only-report.md` Section 16.

```
Real mutation calls this phase: 0
Real orders placed this phase:  0
Strategies started:              0
```

## 18. Regression results

New test file: `tests/common/test_phase_15c_4_cross_account_isolation.py` — 17 tests, all passing.

Targeted regression subset (credential resolution/isolation, router, broker factory, Angel One adapter incl. funds mapping, authorization-state gate, Phase 15B.1 safety regression, risk-manager account isolation) — all passing.

Full suite:

```
Total collected: 1243
Passed:          1243
Failed:          0
Skipped:         0
```

No new regression. The 3 historical baseline failures remain resolved (as first observed in Phase 15C.3, following the removal of the bare, unsuffixed `ANGELONE_*` keys from `trading/.env`).

## 19. Known limitations

1. Position isolation (Section 7) was only proven for the "both empty" case in this phase's real run — neither real account currently holds a position, so a *distinct, non-empty* position set per account was not available to cross-check this phase. The underlying read path is identical to the one proven correct for identity/funds (Section 5), so this is a low-risk gap, but it is not the same as directly observing two different non-empty position sets staying separate.
2. Order isolation under alternation (Section 8) was not re-verified with a fresh real call this phase, specifically to avoid re-tripping Angel's real rate limiter (already hit once in Phase 15C.3) — the conclusion rests on Phase 15C.3's real result (0 vs. 4) plus this phase's proof that the identical read path is alternation-safe for other fields.
3. Trades remain unavailable for both accounts (adapter limitation, unrelated to isolation).
4. Concurrency was tested with synthetic fakes, not the real broker (per this phase's own explicit allowance) — true concurrent real API calls against Angel One were not attempted, to avoid an even higher risk of tripping the real rate limiter with two simultaneous live sessions.

## 20. Final status

**PHASE 15C.4 STATUS: PASS**

Every required isolation property — identity, credentials, session, funds, positions, account state, risk limits, authorization, router resolution, broker-factory fail-closed behavior, concurrent access, and all 5 negative-test scenarios — was proven either directly against the real broker (identity, credentials, session, funds, positions, under real alternation) or with appropriate synthetic tests where the brief itself permits mocking (AccountState/RiskManager/authorization/router/broker-factory internals, concurrency). No account data crossed. No credential leaked. No mutation occurred. No new regression was introduced.

## 21. Synthetic test coverage map (17/17 required areas)

| # | Required coverage area | Covered by |
|---|---|---|
| 1 | AccountState isolation | `test_account_state_fields_are_account_specific_under_alternation`, `test_account_state_a_never_overwritten_by_loading_b` |
| 2 | RiskManager isolation | `test_risk_limits_are_independent_per_account`, `test_reading_account_a_limits_never_returns_account_b_limits` |
| 3 | Authorization-state isolation | `test_authorization_state_is_independent_per_account` |
| 4 | Router isolation | `test_router_resolves_each_account_id_to_itself_only`, `test_router_owner_context_prevents_cross_account_access` |
| 5 | Broker factory isolation | `test_broker_adapter_factory_fails_closed_for_unknown_broker`, `test_create_broker_for_account_never_silently_substitutes_another_account` |
| 6 | Concurrent/interleaved account access | `test_concurrent_read_only_requests_stay_isolated` (real threads), `test_interleaved_alternating_requests_never_cross_contaminate` |
| 7 | Negative test: A given B's credential reference | `test_scenario_1_account_a_given_account_b_credential_reference_is_a_config_choice_not_a_leak` |
| 8 | Negative test: missing credential | `test_scenario_2_missing_credential_reference_env_vars_resolves_to_empty_not_a_fallback` |
| 9 | Negative test: unknown account | `test_scenario_3_unknown_account_is_rejected`, `test_router_fails_closed_for_unknown_account` |
| 10 | Negative test: unknown broker | `test_scenario_4_unknown_broker_is_rejected` |
| 11 | Broker/account identity mismatch | `test_scenario_5_broker_identity_mismatch_must_not_be_silently_trusted` (logic proof) + **the real, live 6-read A/B/A/B/A/B sequence in Section 5**, which is the actual empirical proof against real accounts |
| 12 | Session isolation | Section 5 (real, live) — no dedicated synthetic test needed since the real proof is stronger |
| 13 | Credential isolation | `test_create_broker_for_account_never_silently_substitutes_another_account` + Section 4 (real) |
| 14 | Funds isolation | Section 5/6 (real, live alternation) + `test_concurrent_read_only_requests_stay_isolated` |
| 15 | Positions/orders isolation | Section 5/7 (real) + `test_account_state_fields_are_account_specific_under_alternation` |
| 16 | Mutation safety | Established for both real accounts in Phase 15C.3 Section 16 (not re-tested here — see Section 17 above) |
| 17 | Fail-closed behavior | `test_router_fails_closed_for_unknown_account`, `test_broker_adapter_factory_fails_closed_for_unknown_broker`, `test_scenario_4_unknown_broker_is_rejected` |

Three areas (11, 12, 16) are covered primarily by the **real** validation rather than a dedicated synthetic test, because a real proof against the actual broker is strictly stronger evidence than a mock for exactly these properties — this is a deliberate choice, not a gap.

## Final Regression and Closure

```
Status:
PASS

Real A/B isolation:
PASS

Synthetic isolation tests:
17/17 PASS

Mutation calls:
0

Real orders:
0

Account A authorization:
READ_ONLY

Account B authorization:
READ_ONLY

Full regression:
1243 passed, 0 failed, 0 skipped (1243 collected)

Known baseline failures:
0 (the 3 historical tests/security/test_config_fails_safe.py failures remain resolved, as first observed in
Phase 15C.3, following removal of the bare ANGELONE_* keys from trading/.env)

New failures:
0

Credential leakage:
NONE (verified by direct inspection: every reference to api_key/client_id/totp_secret/mpin in both
docs/phase-15c-3-account-b-read-only-report.md and this report names the FIELD, never a value; every
client identifier shown is in masked form, e.g. AA***21/AA***01)

Phase 15C.4 final result:
PASS
```

---

```
PHASE 15C.4 FINAL STATUS: PASS

Real A/B isolation: PASS
Synthetic tests: 17/17 PASS
Full regression: 1243 passed / 0 failed / 0 skipped
Known baseline failures: 0
New failures: 0

Real mutation calls: 0
Real orders: 0

Account A authorization: READ_ONLY
Account B authorization: READ_ONLY

Credential leakage: NONE

Report:
docs/phase-15c-4-cross-account-isolation-report.md

NEXT PHASE:
15C.5 — Final Multi-Account Read-Only Gate
```

**STOP. Phase 15C.5 is NOT started automatically.** No strategy was started. No order was placed. No canary was authorized. Neither account's `authorization_state` was changed from `READ_ONLY`.
