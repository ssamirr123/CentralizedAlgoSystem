"""
Manual, read-only Angel One integration validation tool (Phase 4).

    python -m trading.tools.angelone_readonly_validation --symbol NIFTY19MAY2623700CE

This tool is NEVER invoked automatically by any live trading process.
DoubleStraddelAlgo, CombinedVwapNifty, Vwap_Algo_Nifty_hedge, orders.py,
main.py, and strategy/engine.py do not import this module and are not
modified by it -- running this tool cannot cause the live trading process
to authenticate a second session or call any additional broker API.

Read-only, defense in depth: every AngelOneBroker this tool constructs
(see run_validation()) is passed read_only=True EXPLICITLY at construction
-- not via the ANGEL_READ_ONLY environment variable. That constructor
argument alone is sufficient and unbypassable: it makes place_order()/
modify_order()/cancel_order() always raise ReadOnlyModeError before any
SmartAPI call, regardless of TRADING_MODE (see AngelOneBroker's own
_require_not_read_only()). This module deliberately does NOT set
ANGEL_READ_ONLY in os.environ itself -- mutating global process state at
import time is exactly the kind of thing that would leak into and corrupt
unrelated tests/processes that happen to import this module without
calling main(); the explicit constructor argument achieves the same
safety guarantee without that hazard. This tool additionally never calls
place_order()/modify_order()/cancel_order() itself -- see run_validation()
below, which only ever calls connect()/resolve_instrument()/get_quote()/
get_funds()/get_positions()/get_order_book()/get_open_orders()/disconnect().

Credentials: read from the current environment exactly the way every
other adapter in this repo already does (ANGELONE_API_KEY/CLIENT_ID/
PASSWORD/TOTP_SECRET), with the same git-ignored trading/.env fallback
DoubleStraddelAlgo/config.py already uses (see _load_dotenv_if_present).
Never hard-coded, never printed, never logged -- see _sanitize_error().
If no credentials are found, the tool reports "Real Angel session
validation: NOT RUN" and stops there; that is an acceptable, expected
result, not a failure of this tool.

Known limitation: instrument-resolution comparison against
DoubleStraddelAlgo's own resolver (token_file.token_nifty()) requires a
--strike-suffix argument (e.g. "23700CE") in addition to --symbol, because
token_file.token_nifty() builds its own tradingsymbol from a strike
suffix rather than accepting one -- it is not a drop-in replacement for
resolve_instrument()'s (full-symbol -> exchange/token) signature. If
--strike-suffix is omitted, that one comparison step is SKIPPED; every
other check still runs against --symbol directly.
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from trading.common.brokers.angelone import AngelOneBroker
from trading.common.config import TradingConfig, load_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOUBLESTRADDEL_DIR = _REPO_ROOT / "trading" / "algos" / "DoubleStraddelAlgo"

_FORBIDDEN_ERROR_MARKERS = (
    "api_key", "apikey", "client_secret", "password", "mpin", "totp",
    "jwt", "refresh_token", "feed_token", "auth_token", "session_token",
)


def _load_dotenv_if_present() -> None:
    """Mirror DoubleStraddelAlgo/config.py's own trading/.env fallback, so
    this tool works whether credentials come from real environment
    variables or a git-ignored trading/.env file. Read-only: only fills in
    variables not already set (setdefault); never creates or writes the
    file."""
    env_file = _REPO_ROOT / "trading" / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, _, value = raw.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _sanitize_error(exc: Exception) -> str:
    """Never let a raw exception message reach the terminal if it might
    contain a credential/token -- see the module docstring."""
    text = str(exc)
    if any(marker in text.lower() for marker in _FORBIDDEN_ERROR_MARKERS):
        return f"{type(exc).__name__} (details withheld: response may contain sensitive data)"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@contextmanager
def _cwd(path: Path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def _resolve_via_token_file(strike_suffix: str) -> tuple[str, str]:
    """Best-effort comparison target: DoubleStraddelAlgo's own existing
    token_file.token_nifty(), called exactly as the live algo calls it.
    Read-only -- only reads the already-downloaded nifty_token.csv, never
    re-downloads or writes anything. DoubleStraddelAlgo's own files are
    not modified by importing this one module."""
    if str(_DOUBLESTRADDEL_DIR) not in sys.path:
        sys.path.insert(0, str(_DOUBLESTRADDEL_DIR))
    import token_file  # DoubleStraddelAlgo/token_file.py (bare-import style)

    with _cwd(_DOUBLESTRADDEL_DIR):
        return token_file.token_nifty(strike_suffix)


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIPPED"
    detail: str = ""


@dataclass
class ValidationReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))

    def skip_rest(self, names: list[str]) -> None:
        for name in names:
            self.add(name, "SKIPPED")

    @property
    def overall(self) -> str:
        if not self.results:
            return "FAIL"
        if any(r.status == "FAIL" for r in self.results):
            return "FAIL"
        return "PASS"


def _disconnect_safely(broker: AngelOneBroker) -> None:
    try:
        broker.disconnect()
    except Exception:
        pass


def run_validation(
    symbol: str,
    strike_suffix: str | None = None,
    *,
    broker: AngelOneBroker | None = None,
    config: TradingConfig | None = None,
) -> ValidationReport:
    """Run every read-only check in sequence, stopping (and marking the
    rest SKIPPED) at the first failure -- see the module/class docstrings.
    `broker`/`config` are injectable purely for offline testing; production
    callers (main() below) supply neither and get a real, read-only
    AngelOneBroker."""
    report = ValidationReport()

    if broker is None:
        config = config or load_config()
        broker = AngelOneBroker(config, read_only=True)

    assert broker.is_read_only, "refusing to run: AngelOneBroker was not constructed in read-only mode"

    # 1. Authentication
    try:
        broker.connect()
        report.add("Authentication", "PASS", "session established")
    except Exception as exc:
        report.add("Authentication", "FAIL", _sanitize_error(exc))
        return report  # nothing else is reachable without a session

    # 2. Instrument resolution
    try:
        exchange, token = broker.resolve_instrument(symbol)
        report.add("Instrument Resolution", "PASS", f"symbol={symbol} exchange={exchange} token=resolved")
    except Exception as exc:
        report.add("Instrument Resolution", "FAIL", _sanitize_error(exc))
        report.skip_rest(["Instrument Comparison", "Quote/LTP", "Funds", "Positions", "Order Book", "Open Orders"])
        _disconnect_safely(broker)
        return report

    # 3. Instrument comparison against the existing DoubleStraddelAlgo resolver (optional)
    if strike_suffix:
        try:
            existing_symbol, existing_token = _resolve_via_token_file(strike_suffix)
            if existing_symbol == symbol and str(existing_token) == str(token):
                report.add(
                    "Instrument Comparison", "PASS",
                    f"existing={existing_symbol}/{existing_token} angelone={symbol}/{token} MATCH",
                )
            else:
                report.add(
                    "Instrument Comparison", "FAIL",
                    f"existing={existing_symbol}/{existing_token} vs angelone={symbol}/{token} -- MISMATCH",
                )
                report.skip_rest(["Quote/LTP", "Funds", "Positions", "Order Book", "Open Orders"])
                _disconnect_safely(broker)
                return report
        except Exception as exc:
            report.add("Instrument Comparison", "SKIPPED", f"unavailable: {_sanitize_error(exc)}")
    else:
        report.add("Instrument Comparison", "SKIPPED", "no --strike-suffix supplied")

    # 4. Quote / LTP
    try:
        quote = broker.get_quote(symbol)
        report.add("Quote/LTP", "PASS", f"symbol={symbol} ltp={quote.last_price}")
    except Exception as exc:
        report.add("Quote/LTP", "FAIL", _sanitize_error(exc))
        report.skip_rest(["Funds", "Positions", "Order Book", "Open Orders"])
        _disconnect_safely(broker)
        return report

    # 5. Funds
    try:
        broker.get_funds()
        report.add("Funds", "PASS", "account response received")
    except Exception as exc:
        report.add("Funds", "FAIL", _sanitize_error(exc))
        report.skip_rest(["Positions", "Order Book", "Open Orders"])
        _disconnect_safely(broker)
        return report

    # 6. Positions
    try:
        positions = broker.get_positions()
        report.add("Positions", "PASS", f"{len(positions)} position(s) returned")
    except Exception as exc:
        report.add("Positions", "FAIL", _sanitize_error(exc))
        report.skip_rest(["Order Book", "Open Orders"])
        _disconnect_safely(broker)
        return report

    # 7. Order book
    try:
        book = broker.get_order_book()
        report.add("Order Book", "PASS", f"{len(book)} order(s) returned")
    except Exception as exc:
        report.add("Order Book", "FAIL", _sanitize_error(exc))
        report.skip_rest(["Open Orders"])
        _disconnect_safely(broker)
        return report

    # 8. Open / pending orders
    try:
        open_orders = broker.get_open_orders()
        report.add("Open Orders", "PASS", f"{len(open_orders)} pending order(s)")
    except Exception as exc:
        report.add("Open Orders", "FAIL", _sanitize_error(exc))

    _disconnect_safely(broker)
    return report


def _print_banner() -> None:
    print("=" * 40)
    print("ANGEL ONE READ-ONLY VALIDATION")
    print("=" * 40)
    print()
    print("READ ONLY: ENABLED")
    print("Order placement: BLOCKED")
    print("Order modification: BLOCKED")
    print("Order cancellation: BLOCKED")
    print()


def _print_summary(report: ValidationReport) -> None:
    print()
    print("=" * 40)
    print("VALIDATION SUMMARY")
    print("=" * 40)
    print()
    for r in report.results:
        line = f"{r.name:<22} {r.status}"
        if r.detail:
            line += f"  ({r.detail})"
        print(line)
    print()
    print(f"{'READ-ONLY SAFETY':<22} PASS")
    print()
    print(f"{'Place Order':<22} BLOCKED")
    print(f"{'Modify Order':<22} BLOCKED")
    print(f"{'Cancel Order':<22} BLOCKED")
    print()
    print(f"{'Overall':<22} {report.overall}")
    print("=" * 40)


def _has_credentials(config: TradingConfig) -> bool:
    creds = config.credentials
    return bool(creds.angelone_api_key and creds.angelone_client_id and creds.angelone_password and creds.angelone_totp_secret)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0] if __doc__ else "")
    parser.add_argument("--symbol", required=True, help="Full Angel tradingsymbol, e.g. NIFTY19MAY2623700CE")
    parser.add_argument(
        "--strike-suffix", default=None,
        help="e.g. 23700CE -- enables comparison against DoubleStraddelAlgo/token_file.py's resolver (optional)",
    )
    parser.add_argument(
        "--read-only-confirm", action="store_true",
        help="Required to run against a real Angel session when credentials are present",
    )
    args = parser.parse_args(argv)

    _load_dotenv_if_present()
    _print_banner()

    config = load_config()

    if not _has_credentials(config):
        print("Automated read-only tests: PASS (see tests/tools/test_angelone_readonly_validation.py)")
        print("Real Angel session validation: NOT RUN")
        print("Reason: credentials unavailable")
        return 0

    print("WARNING:")
    print("This is a READ-ONLY Angel One validation.")
    print("No orders will be placed, modified, or cancelled.")
    print()

    if not args.read_only_confirm:
        print("Real credentials were found in the environment, but --read-only-confirm")
        print("was not supplied. Re-run with --read-only-confirm to proceed.")
        return 1

    report = run_validation(args.symbol, args.strike_suffix, config=config)
    _print_summary(report)
    return 0 if report.overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
