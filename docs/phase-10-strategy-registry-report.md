# Phase 10 — Strategy Registry

```text
Strategy
├── initialize()
├── start()
├── stop()
├── generate_order_intents()
├── get_status()
└── get_metrics()
```

`trading/common/strategy.py` defines this interface (`Strategy`, an ABC) plus the full status vocabulary (`StrategyStatus`) and a concrete `BaseStrategy` that implements all lifecycle/metrics bookkeeping so individual adapters only override a few small hooks. `trading/common/strategy_registry.py` defines `StrategyRegistry`, which holds every registered `Strategy` by `strategy_id` and exposes lifecycle control + read access without callers ever touching a concrete strategy class.

**The Strategy interface contains no broker-specific method anywhere** — verified both by design and by a structural test (`test_strategy_interface_contains_no_broker_specific_method_or_signature`) that inspects every public member of `Strategy` for broker-shaped names/signatures. A strategy's only broker-facing output is `generate_order_intents() -> list[OrderIntent]`, the same broker-independent `OrderIntent` type from Phase 1.

---

## StrategyStatus

All seven required values, exactly as specified:

```
ENABLED | DISABLED | STARTING | RUNNING | STOPPED | ERROR | SHADOW
```

`SHADOW` is deliberately distinct from `RUNNING`: a strategy constructed with `execution_mode=ExecutionMode.SHADOW` (the default for every concrete adapter — see below) reports `SHADOW` once started, not `RUNNING`, so any caller can tell shadow-only activity apart from real-flow-eligible activity without separately consulting `execution_mode`.

Transition rules (enforced by `BaseStrategy`, raising `InvalidStrategyStateError` otherwise):

```
DISABLED --enable()--> ENABLED --start()--> STARTING --(success)--> RUNNING | SHADOW
                                                        --(failure)--> ERROR
RUNNING/SHADOW --stop()--> STOPPED
STOPPED/ERROR --enable()--> ENABLED
```

`disable()` is refused while a strategy is `STARTING`/`RUNNING`/`SHADOW` — it must be `stop()`ped first.

---

## StrategyRegistry

Holds `Strategy` objects by `strategy_id`: `register()`/`unregister()`/`get()`, lifecycle control (`enable`/`disable`/`start`/`stop`/`start_all`/`stop_all`), and read access (`get_status`/`get_metrics`/`get_all_statuses`/`get_all_metrics`/`generate_order_intents`/`generate_all_order_intents`). `start_all()`/`generate_all_order_intents()` are deliberately best-effort — they only act on strategies already in the right state (`ENABLED` to start; `RUNNING`/`SHADOW` to generate) rather than silently enabling/starting something on the caller's behalf, so a configuration mistake surfaces instead of being papered over.

**`StrategyRegistry` is explicitly NOT a replacement for the existing `StrategyAssignment`** (Phase 1/7, `trading/common/strategy_assignment.py` — `strategy_id -> account_id/execution_mode/risk_profile`). The two are complementary: `StrategyRegistry` answers "what strategies exist, and what is each one's own lifecycle/metrics/generated-intents state?"; `StrategyAssignment` answers "which `TradingAccount` is a strategy routed to?" Neither needs to know about the other's internals — `StrategyAssignment` was not modified in this phase.

`StrategyRegistry` never imports anything under `trading.common.brokers` and never calls a broker — verified structurally (`test_registry_module_has_no_broker_import`).

---

## The Three Registered Strategies

`trading/common/strategies/` holds one adapter per live algo:

| Adapter | `strategy_id` |
|---|---|
| `DoubleStraddleStrategy` | `"DoubleStraddelAlgo"` |
| `CombinedVwapNiftyStrategy` | `"CombinedVwapNifty"` |
| `VwapAlgoNiftyHedgeStrategy` | `"Vwap_Algo_Nifty_hedge"` |

Each is a thin `BaseStrategy` subclass. **None of the three imports anything from its corresponding `trading/algos/<Name>/` directory, and none touches that algo's live process in any way** — verified structurally (`test_adapter_source_never_imports_a_live_algo_module` parses each adapter's AST; `test_registering_all_three_adapters_never_touches_trading_algos_modules` proves `sys.modules` gains no `trading.algos.*` entry across a full register → enable → start → generate → stop cycle for all three).

**`generate_order_intents()` returns `[]` for all three, today.** This is deliberate and is the honest scope of this phase, per the explicit instruction "do not change trading behavior": porting each algo's real entry/exit decision logic into this interface is future work, not attempted here. For `DoubleStraddelAlgo` specifically, the pre-existing Phase 3 shadow bridge (`trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py`) is a separate, already-working mechanism that mirrors the live algo's real order decisions into `OrderIntent` objects — this phase does not duplicate or replace it; a future phase could make `DoubleStraddleStrategy.generate_order_intents()` pull from that bridge's output instead of re-deriving the decision logic.

All three default to `execution_mode=ExecutionMode.SHADOW` (the same `ExecutionMode` enum from `trading_account.py`), matching this repo's established convention (Phase 3's execution bridge, Phase 5B's `ConnectedShadowBroker`, Phase 8/9's `read_only`-by-default adapters) of defaulting new integration surfaces to the safest mode.

---

## The Strategy Code Did Not Change

```
git status --short trading/algos/
 M trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py   (pre-existing, from Phase 3 — untouched this phase)
 M trading/algos/DoubleStraddelAlgo/nifty_token.csv               (pre-existing, a Phase 5A data refresh — untouched this phase)
```

No new modification to any file under `trading/algos/*` in this phase. `CombinedVwapNifty/*` and `Vwap_Algo_Nifty_hedge/*` remain completely untouched, as they have been in every prior phase.

---

## Files Created
```
trading/common/strategy.py
trading/common/strategy_registry.py
trading/common/strategies/__init__.py
trading/common/strategies/double_straddle.py
trading/common/strategies/combined_vwap_nifty.py
trading/common/strategies/vwap_algo_nifty_hedge.py
tests/common/test_strategy.py
tests/common/test_strategy_registry.py
tests/common/test_strategy_adapters.py
docs/phase-10-strategy-registry-report.md
```

No existing file was modified. No production/strategy files were modified.

---

## Tests
```
python -m pytest tests/common/test_strategy.py -v              → 22 passed
python -m pytest tests/common/test_strategy_registry.py -v     → 17 passed
python -m pytest tests/common/test_strategy_adapters.py -v     → 22 passed
python -m pytest tests/common/ tests/algos/ tests/tools/ tests/validation/ -q
→ full regression suite (pre-existing + new) — see final count below
```

`test_strategy.py` covers: interface shape (exactly the six documented abstract methods, no broker-specific name/signature anywhere on `Strategy`, `Strategy` cannot be instantiated directly), the full lifecycle state machine (every legal and illegal transition), error handling (`_on_start`/`_on_stop`/`_on_generate_order_intents` raising transitions to `ERROR` and re-raises; the optional `mark_error()` registry-facing extra), metrics bookkeeping (intent counts, timestamps), and that all seven `StrategyStatus` values are exactly as specified.

`test_strategy_registry.py` covers: registration (including duplicate rejection and unknown-strategy lookup) with all three real adapters, lifecycle control through the registry (`enable`/`start`/`stop`/`start_all`/`stop_all`, including the "only acts on strategies in the right state" best-effort behavior), read access (`get_status`/`get_metrics`/`get_all_statuses`/`get_all_metrics`), order-intent generation (single and all, including the inactive-strategy rejection), and the structural no-broker-import check.

`test_strategy_adapters.py` covers: each of the three adapters' `strategy_id`, `Strategy` conformance, default `execution_mode=SHADOW`, full lifecycle reaching `SHADOW` then `STOPPED`, `generate_order_intents()` returning `[]` today, an alternate `PAPER` execution mode reaching `RUNNING` instead, and the two safety-structural tests (AST-level no-live-algo-import check; `sys.modules` side-effect check across a full lifecycle for all three).

---

## Safety Verification

- **No live order was placed, modified, or cancelled** — no adapter here ever calls a broker; `generate_order_intents()` returns `[]` for all three strategies today, so nothing downstream (RiskManager/ExecutionEngine/BrokerManager) is ever reached from this registry.
- **No live algo file was modified.**
- **No live algo module is ever imported by any file created in this phase** — proven both by static AST inspection and by a runtime `sys.modules` check across a full lifecycle.
- **No credentials were touched, read, hardcoded, printed, or committed** — this phase never constructs a `TradingConfig`/`BrokerCredentials` at all.

## Known Limitations

1. **`generate_order_intents()` is a stub (returns `[]`) for all three strategies** — this phase builds the registry/interface skeleton only; porting each algo's real decision logic (DoubleStraddelAlgo's straddle entry/exit, CombinedVwapNifty's VWAP logic, Vwap_Algo_Nifty_hedge's hedge logic) into this interface is explicitly deferred, per "do not change trading behavior."
2. **`StrategyRegistry` is not wired to `StrategyAssignment`, `RiskManager`, or `StrategyExecutionEngine`** — this phase does not build an end-to-end "registry pulls intents → risk → execution" loop; it only proves the registry/interface layer itself is sound. Wiring that loop together is natural follow-up work once decision-logic integration (Known Limitation #1) exists to make it meaningful.
3. **`STARTING` is transient and not independently observable today** — `BaseStrategy.start()` sets it, then immediately resolves to `RUNNING`/`SHADOW` or `ERROR` within the same call, since `_on_start()` for all three current adapters is synchronous and instantaneous. A future adapter with a genuinely asynchronous startup could hold `STARTING` for longer; nothing here needs to change to support that.

## Recommendation for a Future Phase
Wire `StrategyRegistry.generate_all_order_intents()` into a loop that hands each intent to the existing `RiskManager`/`StrategyExecutionEngine` (which already consult `StrategyAssignment`/`BrokerManager` on their own) — but only after a decision is made on how much of each algo's real decision logic to port into `_on_generate_order_intents()`, and only in shadow mode initially, mirroring every prior phase's rollout discipline.

STOP after this report — no live order was enabled at any point, and no live algo behavior changed.
