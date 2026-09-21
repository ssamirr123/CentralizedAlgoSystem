# Phase 15B.1 — Execution Authorization Enforcement: Plan

## 1. Current execution path (as of Phase 15B, before this phase's changes)

```
StrategyExecutionEngine.execute(intent)
  0. CentralKillSwitch.engaged check          -- unconditional, every mode
  1. Idempotency replay (persistent store)     -- authoritative
  2. RiskManager.validate()                    -- 15 checks, includes its own KILL_SWITCH check
  3. Resolve TradingAccount (strategy_id -> account_id -> TradingAccount)
  4. Mode-specific structural gate:
       LIVE_CANARY -> LiveCanaryGuard.authorize() required
       LIVE        -> RiskLimits.is_live_ready() required
       PAPER/SHADOW -> no additional gate
       other        -> rejected (unrecognized execution_mode)
  5. BrokerManager.get_broker() -> BrokerClient.place_order()
  6. Broker response validation
  7. Persist idempotency record
  8. Return ExecutionResult
```

## 2. Where authorization state is currently checked

`TradingAccount.authorization_state` (Phase 15B) exists as a field and is consulted by `TradingAccountRouter` (Phase 15B, read-only operations only — `get_funds`/`get_positions`/`get_orders`/`get_account_state`) to block `DISABLED`/`KILLED` accounts from even being read. It is **not** consulted anywhere in `StrategyExecutionEngine.execute()` — the actual order-execution path.

## 3. Where it is missing

`StrategyExecutionEngine.execute()` resolves `TradingAccount` at step 3 (above) purely to read `account.execution_mode` and `account.broker_id` — `account.authorization_state` is never read. This means a `TradingAccount` freshly constructed with the Phase 15B safe default (`authorization_state=READ_ONLY`) can, today, still reach `LiveCanaryGuard`/`RiskLimits.is_live_ready()` and beyond if its `execution_mode` happens to be `LIVE_CANARY`/`LIVE` — the account-level "has a human actually authorized this account" question is never asked at the one point that matters (immediately before any broker mutation can occur).

## 4. Proposed enforcement point

Insert an explicit **AuthorizationState gate** immediately after `TradingAccount` resolution and **before** `RiskManager.validate()` — matching the required flow:

```
OrderIntent -> TradingAccount resolution -> AuthorizationState validation -> RiskManager
            -> ExecutionMode -> LiveCanaryGuard/existing live gates -> Kill Switch
            -> Idempotency -> Broker execution
```

Concretely, this requires moving `TradingAccount` resolution earlier in `execute()` (it previously happened *after* `RiskManager.validate()`) so the new gate can run before `RiskManager` sees the intent at all. `CentralKillSwitch` (step 0, unconditional) and the idempotency-replay short-circuit (step 1, needs no account) stay exactly where they are — both are cheap, universal checks that don't need `TradingAccount`, and reordering them behind account resolution would add an unnecessary broker/account-registry dependency to a check that today has none.

Rules to implement (Step 3 of the phase brief):

| authorization_state | Effect |
|---|---|
| `DISABLED` | Reject unconditionally, every execution_mode. |
| `READ_ONLY` | Sufficient for `PAPER`/`SHADOW` (simulated, no broker mutation). Insufficient for `LIVE`/`LIVE_CANARY` — rejected before `RiskManager`. |
| `CANARY_READY` | Necessary but NOT sufficient for `LIVE_CANARY` — must still pass `RiskManager`, then `LiveCanaryGuard.authorize()`. Insufficient for plain `LIVE`. |
| `LIVE_AUTHORIZED` | Necessary but NOT sufficient for `LIVE` — must still pass `RiskManager`, then `RiskLimits.is_live_ready()`. Also satisfies `LIVE_CANARY`'s requirement (a fully live-authorized account may run canary-scale orders too), but still must separately pass `LiveCanaryGuard.authorize()` for `LIVE_CANARY` intents. |
| `KILLED` | Reject unconditionally, every execution_mode, forever (irreversible per-instance — see `TradingAccount.set_killed()`). |

Explicitly: **this gate never replaces** `RiskManager`, `LiveCanaryGuard`, `CentralKillSwitch`, idempotency, or broker-response validation. It is a strictly additional, earlier check. An account passing this gate still must pass every gate below it exactly as before this phase.

## 5. Existing safety gates that must remain (verified present, unmodified in logic)

- `CentralKillSwitch` — checked first, unconditionally, unchanged position and logic.
- `RiskManager.validate()` — all 15 checks, unchanged, still runs for every intent that passes the new gate.
- `LiveCanaryGuard.authorize()` — unchanged; still required and still independently enforced for `LIVE_CANARY`.
- `RiskLimits.is_live_ready()` (Blocker E) — unchanged; still required for `LIVE`.
- Persistent idempotency (replay + persist) — unchanged in position and logic.
- Broker response validation — unchanged.
- `TradingAccountRouter`'s own `DISABLED`/`KILLED` read-time gate — unchanged, and now logically consistent with the new execution-time gate (both refuse `DISABLED`/`KILLED` unconditionally).

## 6. Test plan

1. Unit tests for the gate function in isolation (all 5 states × relevant modes).
2. `execute()`-level tests proving: `PAPER`/`SHADOW` unaffected by default `READ_ONLY`; `DISABLED`/`KILLED` block even `PAPER`; `LIVE` requires exactly `LIVE_AUTHORIZED`; `LIVE_CANARY` requires `CANARY_READY` or `LIVE_AUTHORIZED`; a disabled-but-otherwise-authorized account is still rejected.
3. Explicit two-account isolation test: `ACCOUNT_A=LIVE_AUTHORIZED`, `ACCOUNT_B=READ_ONLY`, proving A can proceed past the gate while B cannot, and neither's state leaks to the other.
4. Safety-regression combination tests proving the new gate does not substitute for any existing gate: authorization PASS + risk FAIL = rejected; authorization PASS + kill switch ACTIVE = rejected; authorization PASS + LiveCanaryGuard FAIL = rejected; authorization PASS + idempotency-key-reuse-mismatch = raised/rejected (not silently replayed).
5. Full existing regression suite re-run; any existing test that constructs a `LIVE`/`LIVE_CANARY` `TradingAccount` and expects `execute()` to succeed must now also explicitly set `authorization_state` — this is treated the same way Phase 14.6 Blocker E required existing LIVE tests to configure explicit `RiskLimits`: an update to a test's fixture to match a new, legitimate gate, not a weakening of anything.

No architectural conflict was found — proceeding to implementation.
