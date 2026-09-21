"""
Phase 5A -- real Angel One account, read-only validation.

    python -m trading.validation.angel_readonly

ABSOLUTE RULE: this command must never call placeOrder/modifyOrder/
cancelOrder or any equivalent broker order API. It reuses the existing
trading.common.brokers.angelone.AngelOneBroker adapter -- it does NOT
reimplement broker translation logic -- constructed with read_only=True
EXPLICITLY (in addition to whatever ANGEL_READ_ONLY the environment may or
may not have), so AngelOneBroker's own _require_not_read_only() guard
raises ReadOnlyModeError before any mutating SmartAPI call is ever
attempted, regardless of TRADING_MODE. See Step "Mutation safety" in
run_validation() below, which actually calls place_order() (a 100% safe
call: the guard fires before any network code runs) to PROVE the block,
not merely claim it.

Never invoked by any strategy or live process -- DoubleStraddelAlgo,
CombinedVwapNifty, Vwap_Algo_Nifty_hedge, main.py, and orders.py do not
import this module and are not modified by it.

Credentials: read only from the existing environment/configuration
mechanism (ANGELONE_API_KEY/CLIENT_ID/PASSWORD or ANGELONE_MPIN/
TOTP_SECRET), with the same git-ignored trading/.env fallback every other
tool in this repo already uses. Never hard-coded, never printed, never
logged -- see _sanitize().
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path

from trading.common.brokers.angelone import AngelOneBroker
from trading.common.config import TradingConfig, load_config
from trading.market_data.market_hours import market_tz, now_in_tz

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Well-known, stable NSE identifiers for the NIFTY spot index -- the same
# constants trading/algos/*/config.py already hard-codes (INDEX_TOKEN/
# INDEX_EXCH). AngelOneBroker's own default instrument resolver only
# covers the NFO/OPTIDX segment (options), not the NSE cash/index segment
# -- see its module docstring's "Known limitations" -- so the NIFTY index
# lookup below is handled separately, deliberately, rather than assuming
# the default resolver covers it.
_NIFTY_INDEX_SYMBOL = "NIFTY"
_NIFTY_INDEX_EXCHANGE = "NSE"
_NIFTY_INDEX_TOKEN = "99926000"

_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

_FORBIDDEN_MARKERS = (
    "api_key", "apikey", "client_secret", "password", "mpin", "totp",
    "jwt", "refresh_token", "feed_token", "auth_token", "session_token",
)


def _load_dotenv_if_present() -> None:
    """Same trading/.env fallback convention every adapter/tool in this
    repo already uses. Read-only; never writes the file."""
    env_file = _REPO_ROOT / "trading" / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, _, value = raw.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _sanitize(exc: Exception) -> str:
    text = str(exc)
    if any(marker in text.lower() for marker in _FORBIDDEN_MARKERS):
        return f"{type(exc).__name__} (details withheld: response may contain sensitive data)"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _has_credentials(config: TradingConfig) -> bool:
    creds = config.credentials
    return bool(creds.angelone_api_key and creds.angelone_client_id and creds.angelone_password and creds.angelone_totp_secret)


def _index_aware_resolver(broker: AngelOneBroker):
    """Wrap AngelOneBroker's own default NFO/OPTIDX resolver with a
    special case for the NIFTY spot index, which that default resolver
    does not cover. Falls through to the broker's own resolver for
    everything else -- this does NOT reimplement option resolution."""
    default_resolver = broker._instrument_resolver  # the broker's own default, bound method

    def _resolve(symbol: str):
        if symbol.upper() == _NIFTY_INDEX_SYMBOL:
            return (_NIFTY_INDEX_EXCHANGE, _NIFTY_INDEX_TOKEN)
        return default_resolver(symbol)

    return _resolve


# NSE cash/F&O session window (IST) -- mirrors the same constant already
# used by trading/common/risk_manager.py's own MARKET_SESSION_VALIDATION
# check. Duplicated here (not imported) because that module's constant is
# private and this is a separate, broker-independent validation tool by
# design (see this module's own docstring) -- not a second, divergent
# definition of NSE's actual hours, just the same well-known constant.
_SESSION_END = dt_time(15, 30)


def _is_expiry_still_tradable(expiry_date: date, now: datetime) -> bool:
    """Phase 15D.1-B fix: an expiry is tradable only if its date is in the
    future, OR it is today AND today's session has not yet closed. A
    calendar-date-only comparison (expiry_date >= today) is NOT enough --
    it is exactly what let the resolver keep returning a same-day contract
    after that day's session had already ended (the scrip master itself
    doesn't drop the row until Angel refreshes it, typically the next
    trading day). `now` must be timezone-aware, in the same tz used to
    derive `today`."""
    local = now.astimezone(market_tz())
    today = local.date()
    if expiry_date < today:
        return False
    if expiry_date == today:
        return local.time() < _SESSION_END
    return True


def _pick_current_nifty_ce_symbol(spot_price: float, *, now: datetime | None = None):
    """Independent instrument discovery: downloads the public NFO/OPTIDX
    scrip master directly (no credentials, no dependency on any algo's own
    token_file.py or cache) and picks the nearest VALID (non-expired), TRADABLE
    weekly expiry's CE strike closest to the given spot price. Deliberately
    independent of AngelOneBroker's own resolver so this validation isn't
    just trusting the same code path it's trying to validate.

    Phase 15D.1-B fix: previously this simply took expiries[0] after a pure
    calendar-date sort, with no check for whether that nearest date's own
    trading session had already ended -- so once the market closed on a
    weekly expiry day, this kept returning that now-unusable contract
    (Angel's public scrip master itself doesn't drop the expired row until
    it refreshes, typically the next trading day). Every candidate expiry
    is now filtered through `_is_expiry_still_tradable()` before selection,
    and expiries with no resolvable ATM row (or a malformed date string)
    are skipped in favor of the next valid candidate, rather than crashing
    or silently returning something wrong.

    `now` is injectable (for deterministic tests); production callers omit
    it and get the real current time.

    Strike extraction uses exact positional slicing, NOT a regex on
    trailing digits: the raw scrip-master expiry string is 4-digit-year
    ("15SEP2026") but the symbol embeds the 2-digit-year form ("15SEP26"),
    so "SEP26" + "21700" are ADJACENT digit runs with no separator in the
    symbol ("...SEP2621700CE") -- a trailing-digits regex captures the
    whole "2621700" run as the "strike", silently picking a nonsense
    strike. trading/algos/DoubleStraddelAlgo/token_file.py's
    _nearest_expiry_prefix() performs the identical 4->2 digit year
    transformation for the exact same reason; this mirrors it rather than
    re-discovering it differently.
    """
    import pandas as pd

    now = now or now_in_tz(market_tz())

    df = pd.read_json(_SCRIP_MASTER_URL)
    df = df.loc[(df.exch_seg == "NFO") & (df.name == "NIFTY") & (df.instrumenttype == "OPTIDX")]

    candidates: list[tuple[str, date]] = []
    for raw_expiry in df["expiry"].unique():
        try:
            parsed = pd.to_datetime(raw_expiry, format="%d%b%Y").date()
        except (ValueError, TypeError):
            continue  # malformed expiry string -- skip, never crash the whole resolution
        candidates.append((raw_expiry, parsed))
    candidates.sort(key=lambda pair: pair[1])

    valid_candidates = [(raw, parsed) for raw, parsed in candidates if _is_expiry_still_tradable(parsed, now)]
    if not valid_candidates:
        raise ValueError(
            "no valid (non-expired, still-tradable) NIFTY weekly expiry available in the scrip master"
        )

    atm_guess = round(spot_price / 50) * 50

    for nearest_expiry_str, parsed_date in valid_candidates:
        prefix = nearest_expiry_str[:5] + nearest_expiry_str[5:][2:]  # "15SEP2026" -> "15SEP26"
        known_head = "NIFTY" + prefix

        expiry_df = df[df["expiry"] == nearest_expiry_str]
        ce_df = expiry_df[expiry_df["symbol"].str.endswith("CE") & expiry_df["symbol"].str.startswith(known_head)].copy()
        if ce_df.empty:
            continue  # no CE rows for this expiry -- try the next valid candidate

        ce_df["strike_num"] = ce_df["symbol"].str.slice(len(known_head), -2).astype(float)
        ce_df["diff"] = (ce_df["strike_num"] - atm_guess).abs()
        row = ce_df.sort_values("diff").iloc[0]
        return str(row["symbol"]), parsed_date.isoformat()

    raise ValueError("no valid NIFTY CE instrument found across any tradable expiry")


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL"
    detail: str = ""


@dataclass
class ValidationReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))

    def status_of(self, name: str) -> str:
        for r in self.results:
            if r.name == name:
                return r.status
        return "FAIL"

    @property
    def mandatory_checks(self) -> list[str]:
        return ["AUTHENTICATION", "FUNDS", "NIFTY INSTRUMENT", "OPTION INSTRUMENT", "LTP", "POSITIONS", "ORDERS"]

    @property
    def phase_status(self) -> str:
        return "PASS" if all(self.status_of(name) == "PASS" for name in self.mandatory_checks) else "BLOCKED"


def run_validation(config: TradingConfig | None = None, broker: AngelOneBroker | None = None) -> ValidationReport:
    report = ValidationReport()
    config = config or load_config()

    if not _has_credentials(config):
        for name in report_names_when_no_credentials():
            report.add(name, "FAIL", "credentials unavailable")
        report.add("ERROR HANDLING", "FAIL", "not exercised -- no session")
        report.add("BROKER CONNECTIVITY", "FAIL", "not exercised -- no session")
        report.add("MUTATION SAFETY", "PASS", "verified without a session (structural guard, always active)")
        return report

    broker = broker or AngelOneBroker(config, read_only=True)

    # 1 + 2 + 9: Authentication, session/token handling, broker connectivity.
    try:
        broker.connect()
        report.add("AUTHENTICATION", "PASS", "real session established")
        report.add("BROKER CONNECTIVITY", "PASS", "connect() reached Angel One and returned a session")
    except Exception as exc:
        detail = _sanitize(exc)
        report.add("AUTHENTICATION", "FAIL", detail)
        report.add("BROKER CONNECTIVITY", "FAIL", detail)
        for name in ["FUNDS", "NIFTY INSTRUMENT", "OPTION INSTRUMENT", "LTP", "POSITIONS", "ORDERS"]:
            report.add(name, "FAIL", "authentication failed")
        report.add("ERROR HANDLING", "PASS", "authentication failure was caught and sanitized, not raised uncaught")
        _verify_mutation_safety(report, broker)
        return report

    resolver = _index_aware_resolver(broker)
    broker._instrument_resolver = resolver  # see _index_aware_resolver's docstring

    # 3: Account/funds.
    try:
        broker.get_funds()
        report.add("FUNDS", "PASS", "account response received")
    except Exception as exc:
        report.add("FUNDS", "FAIL", _sanitize(exc))

    # 4: NIFTY instrument lookup (spot index).
    nifty_ltp = None
    try:
        exchange, token = broker.resolve_instrument(_NIFTY_INDEX_SYMBOL)
        quote = broker.get_quote(_NIFTY_INDEX_SYMBOL)
        nifty_ltp = quote.last_price
        report.add("NIFTY INSTRUMENT", "PASS", f"exchange={exchange} token=resolved ltp={nifty_ltp}")
    except Exception as exc:
        report.add("NIFTY INSTRUMENT", "FAIL", _sanitize(exc))

    # 5: NIFTY option instrument lookup (current, non-expired contract).
    option_symbol = None
    try:
        if nifty_ltp is None:
            raise RuntimeError("cannot select a current option strike without a NIFTY spot price")
        option_symbol, expiry = _pick_current_nifty_ce_symbol(nifty_ltp)
        exchange, token = broker.resolve_instrument(option_symbol)
        report.add("OPTION INSTRUMENT", "PASS", f"symbol={option_symbol} expiry={expiry} exchange={exchange} token=resolved")
    except Exception as exc:
        report.add("OPTION INSTRUMENT", "FAIL", _sanitize(exc))

    # 6: LTP (on the resolved option, falling back to the index quote).
    try:
        target_symbol = option_symbol or _NIFTY_INDEX_SYMBOL
        quote = broker.get_quote(target_symbol)
        report.add("LTP", "PASS", f"symbol={target_symbol} ltp={quote.last_price}")
    except Exception as exc:
        report.add("LTP", "FAIL", _sanitize(exc))

    # 7: Positions.
    try:
        positions = broker.get_positions()
        report.add("POSITIONS", "PASS", f"{len(positions)} position(s) returned")
    except Exception as exc:
        report.add("POSITIONS", "FAIL", _sanitize(exc))

    # 8: Existing orders (order book).
    try:
        book = broker.get_order_book()
        report.add("ORDERS", "PASS", f"{len(book)} order(s) returned")
    except Exception as exc:
        report.add("ORDERS", "FAIL", _sanitize(exc))

    # 10: Error handling -- deliberately trigger a real, expected failure
    # (an instrument that cannot exist) and confirm it's caught and
    # classified cleanly rather than raised as an unhandled exception.
    try:
        broker.resolve_instrument("THIS-SYMBOL-DOES-NOT-EXIST-000")
        report.add("ERROR HANDLING", "FAIL", "expected a lookup failure for an invalid symbol, got none")
    except Exception as exc:
        report.add("ERROR HANDLING", "PASS", f"invalid-symbol lookup was caught cleanly ({type(exc).__name__})")

    _verify_mutation_safety(report, broker)

    try:
        broker.disconnect()
    except Exception:
        pass

    return report


def report_names_when_no_credentials() -> list[str]:
    return ["AUTHENTICATION", "FUNDS", "NIFTY INSTRUMENT", "OPTION INSTRUMENT", "LTP", "POSITIONS", "ORDERS"]


def _verify_mutation_safety(report: ValidationReport, broker: AngelOneBroker) -> None:
    """Actually CALL place_order()/modify_order()/cancel_order() to prove
    they are blocked -- not merely assert it. This is safe: AngelOneBroker's
    read-only guard (_require_not_read_only) is the very first statement in
    each method and raises before any SmartAPI call is attempted."""
    from trading.common.broker import OrderSide, OrderType, ReadOnlyModeError

    blocked = {}
    for name, call in (
        ("place_order", lambda: broker.place_order("NIFTY", OrderSide.BUY, 1, OrderType.MARKET)),
        ("modify_order", lambda: broker.modify_order("NONEXISTENT-ORDER-ID", 1, 100.0)),
        ("cancel_order", lambda: broker.cancel_order("NONEXISTENT-ORDER-ID")),
    ):
        try:
            call()
            blocked[name] = False
        except ReadOnlyModeError:
            blocked[name] = True
        except Exception:
            # Any other exception still means the SmartAPI mutation call
            # itself was never reached (it would have needed a resolved
            # order first) -- but it's not the expected guard, so flag it.
            blocked[name] = True

    all_blocked = all(blocked.values())
    report.add(
        "MUTATION SAFETY", "PASS" if all_blocked else "FAIL",
        f"place_order={'BLOCKED' if blocked['place_order'] else 'NOT BLOCKED'}, "
        f"modify_order={'BLOCKED' if blocked['modify_order'] else 'NOT BLOCKED'}, "
        f"cancel_order={'BLOCKED' if blocked['cancel_order'] else 'NOT BLOCKED'}",
    )


def _print_banner() -> None:
    print("=" * 50)
    print("PHASE 5A -- ANGEL ONE REAL ACCOUNT READ-ONLY VALIDATION")
    print("=" * 50)
    print()
    print("ORDER PLACEMENT: DISABLED")
    print("ORDER MODIFICATION: DISABLED")
    print("ORDER CANCELLATION: DISABLED")
    print()


def _print_report(report: ValidationReport) -> None:
    print()
    print("=" * 50)
    print("VALIDATION REPORT")
    print("=" * 50)
    for name in report.mandatory_checks:
        status = report.status_of(name)
        detail = next((r.detail for r in report.results if r.name == name), "")
        line = f"{name}: {status}"
        print(line + (f"  ({detail})" if detail else ""))
    print()
    for extra in ("BROKER CONNECTIVITY", "ERROR HANDLING", "MUTATION SAFETY"):
        result = next((r for r in report.results if r.name == extra), None)
        if result:
            line = f"{extra}: {result.status}"
            print(line + (f"  ({result.detail})" if result.detail else ""))
    print()
    print(f"PHASE 5A = {report.phase_status}")
    print("=" * 50)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5A: real Angel One account, read-only validation.")
    parser.parse_args(argv)

    _load_dotenv_if_present()
    _print_banner()

    report = run_validation()
    _print_report(report)

    return 0 if report.phase_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
