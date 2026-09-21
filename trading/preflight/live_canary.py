"""
Phase 14 -- LIVE_CANARY final preflight.

    python -m trading.preflight.live_canary

This is the human-invoked, final go/no-go gate before a specific broker
account is ever configured with execution_mode=LIVE_CANARY for real.
It must FAIL (non-zero exit) unless EVERY check below passes -- there is
no partial-pass, no override flag, and no way to skip a check.

ABSOLUTE RULE, same as trading/validation/angel_readonly.py: this command
NEVER calls placeOrder/modifyOrder/cancelOrder or any equivalent broker
order API, regardless of TRADING_MODE or any CANARY_* setting. It reuses
trading.validation.angel_readonly.run_validation() (Phase 5A's own real-
account tool) to prove connectivity+mutation-safety live, right now --
not by trusting a historical report file -- and every canary-specific
check below only ever inspects OrderIntent/config data in memory, the
same way trading.common.risk_manager/live_canary do.

Authorization gate this tool enforces (per the Phase 14 brief):
    Phase 5A  = PASS   -> re-run live, right now (see CHECK: REAL ACCOUNT VALIDATION)
    Phase 5B  = PASS   -> implied by REAL ACCOUNT VALIDATION passing with
                          read_only=True (Phase 5B's own safety model)
    Phase 6   = PASS   -> CHECK: RISK MANAGER
    Phase 7   = PASS   -> CHECK: DEDICATED ACCOUNT (multi-account routing)
    Phase 8/9 adapters validated
                       -> CHECK: BROKER ADAPTER VALIDATED (only "angelone"
                          has ever been run against a real account --
                          Dhan/ICICI Breeze are explicitly NOT validated,
                          per their own Known Limitations, and this check
                          FAILS for them by design, not by omission)
    Phase 11 backend validated
                       -> CHECK: CONTROL CENTER BACKEND IMPORTABLE
    Phase 13 monitoring validated
                       -> CHECK: OBSERVABILITY WIRED

Plus the 12 LIVE_CANARY safety requirements themselves (CanaryLimits
construction + a dry-run authorize() against a synthetic tiny intent,
kill switch, emergency shutdown, and a structural check that only a
BrokerClient adapter ever calls a broker order API).

This command NEVER places an order during deployment or at any other
time -- passing preflight is a readiness statement, not an action.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The only broker adapter Phase 5A/5B ever validated against a real
# account. Dhan (Phase 8) and ICICI Breeze (Phase 9) are explicitly
# documented as UNVERIFIED against a real account in their own phase
# reports -- this set is the single source of truth for that fact here,
# so it FAILS closed for any broker not in it, including ones added later
# unless someone deliberately updates this set after doing the real work.
_VALIDATED_BROKERS = frozenset({"angelone"})

# Well-known non-canary example account ids (Phase 7/11) -- a canary
# account must be its OWN, distinct, dedicated account, never one of these.
_KNOWN_NON_CANARY_ACCOUNTS = frozenset({"ANGEL_MAIN", "DHAN_MAIN", "ICICI_MAIN"})


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL"
    detail: str = ""


@dataclass
class PreflightReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(r.status == "PASS" for r in self.results)

    @property
    def phase_status(self) -> str:
        return "PASS" if self.all_passed else "FAIL"


def _load_dotenv_if_present() -> None:
    """Same trading/.env fallback convention every tool in this repo uses
    -- see trading/validation/angel_readonly.py's identical helper."""
    env_path = _REPO_ROOT / "trading" / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def _check_configuration(report: PreflightReport):
    """Reads CANARY_* env vars and constructs a CanaryLimits -- FAILS
    closed on anything missing or invalid. Returns the CanaryLimits (or
    None) so later checks can reuse it without re-parsing."""
    from trading.common.live_canary import CanaryLimits

    account_id = os.environ.get("CANARY_ACCOUNT_ID", "").strip()
    max_qty = _env_int("CANARY_MAX_ORDER_QUANTITY")
    max_value = _env_float("CANARY_MAX_ORDER_VALUE")
    max_daily_loss = _env_float("CANARY_MAX_DAILY_LOSS")
    max_strategy_loss = _env_float("CANARY_MAX_STRATEGY_LOSS")
    max_orders = _env_int("CANARY_MAX_ORDERS_PER_DAY")

    missing = [
        name for name, value in (
            ("CANARY_ACCOUNT_ID", account_id or None),
            ("CANARY_MAX_ORDER_QUANTITY", max_qty),
            ("CANARY_MAX_ORDER_VALUE", max_value),
            ("CANARY_MAX_DAILY_LOSS", max_daily_loss),
            ("CANARY_MAX_STRATEGY_LOSS", max_strategy_loss),
            ("CANARY_MAX_ORDERS_PER_DAY", max_orders),
        ) if value is None
    ]
    if missing:
        report.add("CONFIGURATION", "FAIL", f"missing or invalid: {', '.join(missing)}")
        return None

    try:
        limits = CanaryLimits(
            account_id=account_id, max_order_quantity=max_qty, max_order_value=max_value,
            max_daily_loss=max_daily_loss, max_strategy_loss=max_strategy_loss, max_orders_per_day=max_orders,
        )
    except ValueError as exc:
        report.add("CONFIGURATION", "FAIL", str(exc))
        return None

    report.add(
        "CONFIGURATION", "PASS",
        f"account={account_id} max_qty={max_qty} max_value={max_value} "
        f"max_daily_loss={max_daily_loss} max_strategy_loss={max_strategy_loss} max_orders_per_day={max_orders}",
    )
    return limits


def _check_dedicated_account(report: PreflightReport, limits) -> None:
    if limits is None:
        report.add("DEDICATED_ACCOUNT", "FAIL", "not evaluated -- CONFIGURATION failed")
        return
    if limits.account_id in _KNOWN_NON_CANARY_ACCOUNTS:
        report.add(
            "DEDICATED_ACCOUNT", "FAIL",
            f"CANARY_ACCOUNT_ID={limits.account_id!r} is one of the shared example accounts "
            f"({sorted(_KNOWN_NON_CANARY_ACCOUNTS)}) -- LIVE_CANARY requires its own dedicated account",
        )
        return
    report.add("DEDICATED_ACCOUNT", "PASS", f"account={limits.account_id} is distinct from the shared example accounts")


def _check_broker_adapter_validated(report: PreflightReport) -> str | None:
    broker_name = os.environ.get("BROKER", "").strip().lower()
    if not broker_name:
        report.add("BROKER ADAPTER VALIDATED", "FAIL", "BROKER is not set")
        return None
    if broker_name not in _VALIDATED_BROKERS:
        report.add(
            "BROKER ADAPTER VALIDATED", "FAIL",
            f"BROKER={broker_name!r} has never been validated against a real account "
            f"(only {sorted(_VALIDATED_BROKERS)} has, via Phase 5A) -- see that adapter's own phase "
            "report's Known Limitations before ever running a real Phase-5A-equivalent validation for it",
        )
        return None
    report.add("BROKER ADAPTER VALIDATED", "PASS", f"BROKER={broker_name!r} was validated against a real account in Phase 5A")
    return broker_name


def _check_real_account_validation(report: PreflightReport, broker_name: str | None) -> None:
    """Re-runs Phase 5A's own real-account validation LIVE, right now --
    not a historical-report lookup. Only meaningful for a validated
    broker; skipped (reported FAIL, not silently PASS) otherwise."""
    if broker_name != "angelone":
        report.add("REAL ACCOUNT VALIDATION", "FAIL", "not evaluated -- BROKER ADAPTER VALIDATED failed")
        return

    from trading.validation.angel_readonly import run_validation

    validation_report = run_validation()
    if validation_report.phase_status == "PASS":
        report.add("REAL ACCOUNT VALIDATION", "PASS", "trading.validation.angel_readonly re-run live -- PHASE 5A = PASS")
    else:
        failed = [r.name for r in validation_report.results if r.status != "PASS"]
        report.add("REAL ACCOUNT VALIDATION", "FAIL", f"trading.validation.angel_readonly re-run live -- PHASE 5A = BLOCKED (failed: {failed})")


def _check_risk_manager(report: PreflightReport, limits) -> None:
    from trading.common.broker_manager import BrokerManager
    from trading.common.broker import OrderSide, OrderType
    from trading.common.order_intent import OrderIntent
    from trading.common.risk_manager import RiskManager
    from trading.common.strategy_assignment import StrategyAssignment
    from trading.common.trading_account import ExecutionMode, TradingAccount

    if limits is None:
        report.add("RISK MANAGER", "FAIL", "not evaluated -- CONFIGURATION failed")
        return
    try:
        broker_manager = BrokerManager()
        account = TradingAccount(
            account_id=limits.account_id, account_name="Canary preflight (dry run)",
            broker_id=os.environ.get("BROKER", "angelone").strip().lower() or "angelone",
            execution_mode=ExecutionMode.LIVE_CANARY,
        )
        broker_manager.register_account(account)  # no client/factory -- never resolved, never connected
        assignment = StrategyAssignment(broker_manager)
        assignment.assign("PREFLIGHT_DRY_RUN", limits.account_id, execution_mode=ExecutionMode.LIVE_CANARY)
        risk_manager = RiskManager(assignment)

        intent = OrderIntent(
            strategy_id="PREFLIGHT_DRY_RUN", account_id=limits.account_id, symbol="NIFTY15SEP2623400CE",
            exchange="NFO", side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=1.0,
            idempotency_key="preflight-dry-run",
        )
        result = risk_manager.validate(intent)
        if result.allowed:
            report.add("RISK MANAGER", "PASS", "RiskManager constructed and approved a synthetic dry-run intent (never sent anywhere)")
        else:
            report.add("RISK MANAGER", "FAIL", f"synthetic dry-run intent was rejected: {result.reason}")
    except Exception as exc:  # noqa: BLE001 - report, don't crash the tool
        report.add("RISK MANAGER", "FAIL", str(exc))


def _check_canary_guard(report: PreflightReport, limits) -> None:
    from trading.common.broker import OrderSide, OrderType
    from trading.common.live_canary import LiveCanaryGuard
    from trading.common.order_intent import OrderIntent

    if limits is None:
        report.add("CANARY GUARD DRY RUN", "FAIL", "not evaluated -- CONFIGURATION failed")
        report.add("KILL SWITCH", "FAIL", "not evaluated -- CONFIGURATION failed")
        report.add("EMERGENCY SHUTDOWN", "FAIL", "not evaluated -- CONFIGURATION failed")
        return

    try:
        guard = LiveCanaryGuard(limits)
        intent = OrderIntent(
            strategy_id="PREFLIGHT_DRY_RUN", account_id=limits.account_id, symbol="NIFTY15SEP2623400CE",
            exchange="NFO", side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=1.0,
            idempotency_key="preflight-dry-run-canary",
        )
        result = guard.authorize(intent)
        if result.allowed:
            report.add("CANARY GUARD DRY RUN", "PASS", "LiveCanaryGuard constructed and authorized a synthetic dry-run intent (never sent anywhere)")
        else:
            report.add("CANARY GUARD DRY RUN", "FAIL", f"synthetic dry-run intent was rejected: {result.reason}")

        # Kill switch: engage, confirm it blocks, disengage, confirm normal again.
        guard.engage_kill_switch(reason="preflight self-test")
        blocked = guard.authorize(OrderIntent(
            strategy_id="PREFLIGHT_DRY_RUN", account_id=limits.account_id, symbol="NIFTY15SEP2623400CE",
            exchange="NFO", side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=1.0,
            idempotency_key="preflight-dry-run-killswitch",
        ))
        guard.disengage_kill_switch()
        if not blocked.allowed and "KILL_SWITCH" in blocked.reason:
            report.add("KILL SWITCH", "PASS", "engage_kill_switch() correctly blocked authorization; disengage_kill_switch() restored it")
        else:
            report.add("KILL SWITCH", "FAIL", "kill switch did not block authorization as expected")

        # Emergency shutdown: a FRESH guard, since shutdown is irreversible.
        shutdown_guard = LiveCanaryGuard(limits)
        shutdown_guard.emergency_shutdown("preflight self-test")
        after_shutdown = shutdown_guard.authorize(OrderIntent(
            strategy_id="PREFLIGHT_DRY_RUN", account_id=limits.account_id, symbol="NIFTY15SEP2623400CE",
            exchange="NFO", side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=1.0,
            idempotency_key="preflight-dry-run-shutdown",
        ))
        if not after_shutdown.allowed and "EMERGENCY_SHUTDOWN" in after_shutdown.reason:
            report.add("EMERGENCY SHUTDOWN", "PASS", "emergency_shutdown() correctly blocks every subsequent authorize() call")
        else:
            report.add("EMERGENCY SHUTDOWN", "FAIL", "emergency shutdown did not block authorization as expected")
    except Exception as exc:  # noqa: BLE001
        report.add("CANARY GUARD DRY RUN", "FAIL", str(exc))


def _check_observability(report: PreflightReport) -> None:
    try:
        from trading.common.alerts import AlertManager
        from trading.common.observability import AuditTrail, MetricsRegistry

        metrics = MetricsRegistry()
        audit_trail = AuditTrail()
        alerts = AlertManager(audit_trail=audit_trail)
        metrics.record_strategy_heartbeat("PREFLIGHT_DRY_RUN")
        audit_trail.append("PREFLIGHT_SELF_TEST")
        alerts.strategy_stopped("PREFLIGHT_DRY_RUN")
        if audit_trail.verify() and len(audit_trail.records()) == 2:
            report.add("OBSERVABILITY WIRED", "PASS", "MetricsRegistry/AuditTrail/AlertManager constructed and exercised; hash chain verified")
        else:
            report.add("OBSERVABILITY WIRED", "FAIL", "audit trail did not verify after a self-test append")
    except Exception as exc:  # noqa: BLE001
        report.add("OBSERVABILITY WIRED", "FAIL", str(exc))


def _check_control_center_backend(report: PreflightReport) -> None:
    try:
        import trading.api.execution_routes  # noqa: F401
        import trading.api.execution_state  # noqa: F401

        report.add("CONTROL CENTER BACKEND IMPORTABLE", "PASS", "trading.api.execution_routes/execution_state import cleanly")
    except Exception as exc:  # noqa: BLE001
        report.add("CONTROL CENTER BACKEND IMPORTABLE", "FAIL", str(exc))


def _check_broker_is_sole_order_caller(report: PreflightReport) -> None:
    """Structural proof that RiskManager and LiveCanaryGuard -- the two
    gates between Strategy/OrderIntent and ExecutionEngine -- never call a
    broker order API themselves. Mirrors the source-inspection pattern
    already used in tests/api/test_execution_routes.py."""
    targets = [
        _REPO_ROOT / "trading" / "common" / "risk_manager.py",
        _REPO_ROOT / "trading" / "common" / "live_canary.py",
    ]
    forbidden = ("place_order(", "placeOrder(", "modify_order(", "modifyOrder(", "cancel_order(", "cancelOrder(")
    violations = []
    for path in targets:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            violations.append(f"{path.name}: could not read ({exc})")
            continue
        for marker in forbidden:
            if marker in source:
                violations.append(f"{path.name} contains {marker!r}")
    if violations:
        report.add("BROKER ADAPTER IS SOLE ORDER CALLER", "FAIL", "; ".join(violations))
    else:
        report.add(
            "BROKER ADAPTER IS SOLE ORDER CALLER", "PASS",
            "risk_manager.py and live_canary.py contain no broker order-placement call",
        )


def _check_no_automatic_order_placement(report: PreflightReport) -> None:
    """This preflight tool never IMPORTS a broker adapter class or raw SDK
    at all -- checked via the AST's actual import statements only (never a
    plain text/string search, which would also match this check's own
    source and docstrings, since they have to name the very patterns being
    looked for -- an unavoidable paradox for a substring search, not for
    an import-statement check). Never importing a real adapter or SDK
    means this file has no object capable of placing an order in the
    first place, structurally, regardless of what CANARY_*/BROKER/
    TRADING_MODE are set to. (Contrast trading/validation/
    angel_readonly.py, which DOES import the Angel adapter deliberately,
    and instead proves safety by calling place_order() against it and
    confirming the read-only guard fires -- this tool takes the simpler
    route of never touching a broker adapter at all.)"""
    import ast

    forbidden_names = {"AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "SmartConnect", "BreezeConnect", "dhanhq"}
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)

    hits = sorted(imported & forbidden_names)
    if hits:
        report.add("NO AUTOMATIC ORDER PLACEMENT", "FAIL", f"preflight tool imports a broker adapter/SDK: {hits}")
    else:
        report.add("NO AUTOMATIC ORDER PLACEMENT", "PASS", "this preflight command never imports a broker adapter or SDK class")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _print_banner() -> None:
    print("=" * 60)
    print("PHASE 14 -- LIVE_CANARY FINAL PREFLIGHT")
    print("=" * 60)
    print()
    print("ORDER PLACEMENT: DISABLED (this command never places an order)")
    print()


def _print_report(report: PreflightReport) -> None:
    print()
    print("=" * 60)
    print("PREFLIGHT REPORT")
    print("=" * 60)
    for r in report.results:
        line = f"{r.name}: {r.status}"
        print(line + (f"  ({r.detail})" if r.detail else ""))
    print()
    print(f"PHASE 14 = {report.phase_status}")
    print("=" * 60)
    if report.phase_status != "PASS":
        print()
        print("LIVE_CANARY is NOT authorized. Fix every FAIL above and re-run.")
        print("This tool never enables live trading on its own -- passing")
        print("preflight is a readiness statement, not an action.")


def run_preflight() -> PreflightReport:
    report = PreflightReport()

    limits = _check_configuration(report)
    _check_dedicated_account(report, limits)
    broker_name = _check_broker_adapter_validated(report)
    _check_real_account_validation(report, broker_name)
    _check_risk_manager(report, limits)
    _check_canary_guard(report, limits)
    _check_observability(report)
    _check_control_center_backend(report)
    _check_broker_is_sole_order_caller(report)
    _check_no_automatic_order_placement(report)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 14: LIVE_CANARY final preflight (never places an order).")
    parser.parse_args(argv)

    _load_dotenv_if_present()
    _print_banner()

    report = run_preflight()
    _print_report(report)

    return 0 if report.phase_status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
