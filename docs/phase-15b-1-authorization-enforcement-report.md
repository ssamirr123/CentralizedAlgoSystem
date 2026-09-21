# Phase 15B.1 — Execution Authorization Enforcement: Report

**No real broker order was placed. No `placeOrder`/`modifyOrder`/`cancelOrder` API was ever called. Live trading was not enabled.**

## Implementation summary

Closed the known limitation flagged at the end of Phase 15B: `StrategyExecutionEngine.execute()` did not consult `TradingAccount.authorization_state`. A new, additional hard gate now runs immediately after `TradingAccount` resolution and strictly before `RiskManager.validate()`, matching the required flow:

```
OrderIntent -> TradingAccount resolution -> AuthorizationState validation -> RiskManager
            -> ExecutionMode -> LiveCanaryGuard/existing live gates -> Kill Switch
            -> Idempotency -> Broker execution
```

This gate supplements every existing gate — it replaces none of them, and passing it is never sufficient on its own for an order to reach a broker.

## Execution path before

```
StrategyExecutionEngine.execute(intent)
  0. CentralKillSwitch.engaged                 -- unconditional
  1. Idempotency replay
  2. RiskManager.validate()                     -- 15 checks
  3. Resolve TradingAccount                      -- authorization_state never read
  4. Mode-specific gate (LiveCanaryGuard / RiskLimits.is_live_ready())
  5. Broker resolution + place_order()
  6. Broker response validation
  7. Persist idempotency
  8. Return ExecutionResult
```

## Execution path after

```
StrategyExecutionEngine.execute(intent)
  0. CentralKillSwitch.engaged                 -- unconditional, unchanged
  1. Idempotency replay                         -- unchanged
  2. Resolve TradingAccount + AuthorizationState gate (NEW)
  3. RiskManager.validate()                      -- 15 checks, unchanged
  4. Mode-specific gate (LiveCanaryGuard / RiskLimits.is_live_ready()) -- unchanged
  5. Broker resolution + place_order()           -- unchanged
  6. Broker response validation                  -- unchanged
  7. Persist idempotency                         -- unchanged
  8. Return ExecutionResult
```

`TradingAccount` resolution was moved from (old) step 3 to (new) step 2 — the only structural reordering — specifically so the new gate can run before `RiskManager` ever sees the intent. `CentralKillSwitch` and the idempotency-replay short-circuit were deliberately left in their original positions: both are cheap, universal checks that need no account context, and moving them behind account resolution would add an unnecessary dependency to a check that today has none.

A new audit event, `AUTHORIZATION_STATE_GATE`, is emitted for every intent (pass or fail) — the audit trail for a successful order now begins `AUTHORIZATION_STATE_GATE -> ORDER_INTENT_CREATED -> RISK_DECISION -> ...` instead of starting at `ORDER_INTENT_CREATED`.

## Authorization rules implemented

| `authorization_state` | Effect |
|---|---|
| `DISABLED` | Reject unconditionally, every execution_mode, including `PAPER`/`SHADOW`. |
| `READ_ONLY` (default for every new account) | Sufficient for `PAPER`/`SHADOW`. Insufficient for `LIVE`/`LIVE_CANARY`. |
| `CANARY_READY` | Necessary but not sufficient for `LIVE_CANARY` — must still pass `RiskManager`, then `LiveCanaryGuard.authorize()`. Insufficient for plain `LIVE`. |
| `LIVE_AUTHORIZED` | Necessary but not sufficient for `LIVE` or `LIVE_CANARY` — must still pass `RiskManager`, then `RiskLimits.is_live_ready()` (for `LIVE`) or `LiveCanaryGuard.authorize()` (for `LIVE_CANARY`). |
| `KILLED` | Reject unconditionally, every mode, forever (irreversible per-instance). |
| missing / not a recognized `AccountAuthorizationState` | Fail closed (defensive check — `TradingAccount.__post_init__` already prevents this at construction, but the gate does not trust that invariant blindly). |
| account not found / assignment missing | Fail closed (pre-existing `UnknownAccountError`/`UnknownAssignmentError` handling, unchanged). |
| account disabled (`enabled=False`) | Fail closed, independent of `authorization_state`. |

Never defaults to `LIVE_AUTHORIZED`, `CANARY_READY`, or any particular broker — every new `TradingAccount` still defaults to `authorization_state=READ_ONLY` (Phase 15B's own safe default), and `broker_type_for_id()` still fails closed for anything unregistered.

## Error handling

`trading.common.trading_account.AccountAuthorizationError` (new): carries `account_id`, `authorization_state`, and `reason` as structured attributes — never a credential value. Raised by `_check_authorization_state()` in `trading/common/execution.py`, caught inside `execute()` and converted into a rejected `ExecutionResult`, exactly like the pre-existing `UnknownAssignmentError`/`UnknownAccountError` handling — it never propagates out of `execute()` as an unhandled exception.

## Safety gates preserved (verified, not assumed)

- `CentralKillSwitch` — unchanged position (checked first, unconditionally) and logic. Proven still effective: `test_authorization_pass_plus_kill_switch_active_is_still_rejected`.
- `RiskManager.validate()` — all 15 checks unchanged, still runs for every intent that passes the new gate. Proven: `test_authorization_pass_plus_risk_fail_is_still_rejected`, `test_gate_runs_before_risk_manager_not_instead_of_it`.
- `LiveCanaryGuard.authorize()` — unchanged, still independently required for `LIVE_CANARY`. Proven: `test_authorization_pass_plus_live_canary_guard_fail_is_still_rejected`.
- `RiskLimits.is_live_ready()` (Blocker E) — unchanged, still required for `LIVE`.
- Persistent idempotency (replay + persist, `IdempotencyKeyReuseError`) — unchanged. Proven: `test_authorization_pass_plus_idempotency_key_reuse_mismatch_is_never_silently_replayed`, `test_authorization_gate_does_not_bypass_idempotent_replay_short_circuit`.
- Broker response validation — unchanged.
- `TradingAccountRouter`'s own `DISABLED`/`KILLED` read-time gate (Phase 15B) — unchanged, now logically consistent with this execution-time gate.

## Tests

New test files:
- `tests/common/test_authorization_state_gate.py` (11 tests) — all 5 states × relevant modes, `execute()`-level.
- `tests/common/test_phase_15b_1_safety_regression.py` (8 tests) — fail-closed edge cases, two-account isolation, and the four required "authorization PASS + X FAIL = rejected" combinations.

Fixture fixes to existing tests (legitimate — granting explicit authorization to match a newly-enforced requirement, the same precedent as Phase 14.6 Blocker E requiring explicit `RiskLimits` for `LIVE`):
- `tests/common/test_phase_14_6_hardening.py` — `_stack()` now resolves `authorization_state` from `execution_mode` (`LIVE_AUTHORIZED` for `LIVE`, `CANARY_READY` for `LIVE_CANARY`) unless explicitly overridden.
- `tests/common/test_live_canary_execution_wiring.py` — `_build_stack()` now grants `authorization_state=CANARY_READY`.
- `tests/common/test_observability_wiring.py` — two tests asserting an exact audit-event sequence updated to include the new leading `AUTHORIZATION_STATE_GATE` event.

```
Total collected: 1217
Passed:          1214
Failed:          3   (pre-existing baseline, unrelated -- see below)
Skipped:         0
```

### The 3 pre-existing failures (unchanged from Phase 15B, not introduced here)

```
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[CombinedVwapNifty]
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[DoubleStraddelAlgo]
tests/security/test_config_fails_safe.py::test_algo_config_empty_when_env_unset[Vwap_Algo_Nifty_hedge]
```

Same root cause documented in `docs/phase-15b-validation-report.md`: the real local `trading/.env` file leaks through each algo's own `dotenv` loader, overriding `monkeypatch.delenv` in this environment. Confirmed unrelated to Phase 15B.1 — this file was not touched this phase, and the traceback involves no Phase 15B/15B.1 code.

## Real broker calls / order calls / orders placed

```
Real broker calls:        0
Real order calls:         0
Real orders placed:       0
```

Every test in this phase uses `PaperBroker`, `dry_run=True`, or a `FakeSmartApi`/`ShadowBroker` double — never a real network call.

## Known limitations

1. `AccountAuthorizationState` is still not wired into `TradingAccountRouter`'s write-side (there is none — that class remains read-only by design) beyond the existing `DISABLED`/`KILLED` check.
2. No second real broker account exists in this environment to validate the gate against a genuine `LIVE_AUTHORIZED` account end-to-end with real credentials (only synthetic/fake doubles were used, per this phase's own explicit rules).
3. The 3 pre-existing `test_config_fails_safe.py` failures remain unresolved (environment-specific, out of scope for this phase).
