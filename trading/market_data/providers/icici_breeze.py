"""
ICICI Direct **Breeze** market-data provider.

Read-only: quotes, option chain, historical candles, and a streaming
websocket feed. No order methods (see ``base.py``).

All Breeze-specific detail is contained here:
  * ``_INDEX_CODES``          -- internal symbol -> (stock_code, exchange_code)
  * ``_unwrap`` / ``_norm_*`` -- response shape + field-name mapping

SDK: ``pip install breeze-connect``. Imported lazily inside ``connect()``
so this module imports fine without the dependency (CI, unit tests that
inject a fake client, code review).

Security (rules 10/11): the API key / secret / session token are never
logged and never placed in an exception message. Errors report the
*exception class name* only.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from typing import Any

from trading.market_data.providers.base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    TickCallback,
)
from trading.market_data.schemas import Candle, IndexQuote, OptionChain, OptionChainRow, OptionQuote
from trading.market_data.symbols import Exchange, Instrument, option_instrument

logger = logging.getLogger("trading.market_data.icici_breeze")

# internal symbol -> (Breeze stock_code, Breeze exchange_code).
# NOTE: verify these against the live Breeze account -- Bank Nifty in
# particular has historically been "CNXBAN". SENSEX requires BSE data
# entitlement on the account.
_INDEX_CODES: dict[str, tuple[str, str]] = {
    "NIFTY": ("NIFTY", "NSE"),
    "BANKNIFTY": ("CNXBAN", "NSE"),
    "INDIA_VIX": ("INDVIX", "NSE"),
    "SENSEX": ("BSESEN", "BSE"),
}

_RIGHT_TO_OT = {"call": "CE", "ce": "CE", "put": "PE", "pe": "PE"}
_OT_TO_RIGHT = {"CE": "call", "PE": "put"}


# --------------------------------------------------------------------------
# small value coercers -- Breeze returns numbers as strings, "" for blanks
# --------------------------------------------------------------------------
def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _dt(v: Any) -> datetime | None:
    """Parse a Breeze timestamp string; return tz-aware UTC or None."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S.000Z", "%Y-%m-%dT%H:%M:%S",
                "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(s, fmt)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    try:  # ISO 8601 with offset
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _breeze_date(d: date) -> str:
    """Breeze expects an ISO datetime for expiry/from/to args."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _strike_str(strike: float) -> str:
    """Breeze builds its contract key by string concatenation, so a strike
    must be "23950" not "23950.0" (the latter resolves to no feed)."""
    return f"{strike:g}"


class ICICIBreezeProvider(MarketDataProvider):
    name = "icici_breeze"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        session_token: str,
        client_factory: Callable[[str], Any] | None = None,
        master_loader: Callable[[], list[dict]] | None = None,
    ) -> None:
        """``client_factory(api_key) -> <breeze client>`` is a test seam. In
        production it is None and the real ``BreezeConnect`` is imported
        lazily in ``connect()``.

        ``master_loader() -> list[dict]`` yields normalized instrument-master
        rows (see ``_MASTER_ROW_KEYS``); None uses the default ICICI
        security-master download."""
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        self._session_token = session_token or ""
        self._client_factory = client_factory
        self._master_loader = master_loader
        self._client: Any = None
        self._connected = False
        self._ws_connected = False
        self._on_tick: TickCallback | None = None
        self._resolver: Callable[[dict], Instrument | None] | None = None
        self._subscribed: set[str] = set()

    # --- lifecycle ---------------------------------------------------
    def connect(self) -> None:
        if not (self._api_key and self._api_secret and self._session_token):
            raise ProviderAuthError(
                "BREEZE_API_KEY / BREEZE_SECRET_KEY / BREEZE_SESSION_TOKEN are not all set. "
                "Provision today's Breeze session (see Phase 3)."
            )
        if self._client_factory is not None:
            self._client = self._client_factory(self._api_key)
        else:
            try:
                from breeze_connect import BreezeConnect  # noqa: PLC0415  (lazy on purpose)
            except ImportError as exc:  # pragma: no cover - dep not installed in CI
                raise ProviderConnectionError(
                    "breeze-connect is not installed; run `pip install breeze-connect`"
                ) from exc
            self._client = BreezeConnect(api_key=self._api_key)

        try:
            self._client.generate_session(
                api_secret=self._api_secret, session_token=self._session_token
            )
        except Exception as exc:  # noqa: BLE001 - normalize, never leak the token
            raise ProviderAuthError(
                f"Breeze session generation failed ({type(exc).__name__}). "
                "The daily session token is likely missing or expired."
            ) from None
        self._connected = True
        logger.info("breeze.connected provider=%s", self.name)

    def disconnect(self) -> None:
        if self._ws_connected and self._client is not None:
            try:
                self._client.ws_disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._ws_connected = False
        self._connected = False
        self._subscribed.clear()
        self._on_tick = None
        logger.info("breeze.disconnected provider=%s", self.name)

    def is_connected(self) -> bool:
        return self._connected

    def _require_client(self) -> Any:
        if not self._connected or self._client is None:
            raise ProviderConnectionError("Breeze provider is not connected; call connect() first")
        return self._client

    # --- response handling -----------------------------------------
    @staticmethod
    def _unwrap(resp: Any) -> Any:
        """Breeze wraps payloads as {"Success": <data>, "Status": <int>, "Error": <str|None>}."""
        if isinstance(resp, dict):
            err = resp.get("Error")
            if err:
                text = str(err).lower()
                if "rate" in text or "throttl" in text or resp.get("Status") == 429:
                    raise ProviderRateLimitError(f"Breeze rate limited: {err}")
                raise ProviderDataError(f"Breeze error: {err}")
            if "Success" in resp:
                return resp["Success"]
            return resp
        return resp

    # --- snapshot reads --------------------------------------------
    def get_index_quote(self, instrument: Instrument) -> IndexQuote:
        if not instrument.is_index:
            raise ValueError(f"get_index_quote requires an INDEX instrument, got {instrument.instrument_type}")
        try:
            stock_code, exch = _INDEX_CODES[instrument.internal_symbol]
        except KeyError as exc:
            raise ValueError(f"No Breeze mapping for index {instrument.internal_symbol!r}") from exc

        try:
            raw = self._require_client().get_quotes(
                stock_code=stock_code, exchange_code=exch, product_type="cash",
                expiry_date="", right="", strike_price="",
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Breeze get_quotes failed ({type(exc).__name__})") from exc

        rows = self._unwrap(raw)
        row = rows[0] if isinstance(rows, list) and rows else rows
        if not isinstance(row, dict):
            raise ProviderDataError(f"Unexpected Breeze quote payload for {instrument.internal_symbol}")

        return IndexQuote.build(
            instrument.internal_symbol,
            ltp=_f(row.get("ltp")) if row.get("ltp") not in (None, "") else _f(row.get("last")) or _f(row.get("spot_price")),
            open=_f(row.get("open")),
            high=_f(row.get("high")),
            low=_f(row.get("low")),
            prev_close=_f(row.get("previous_close")) if row.get("previous_close") not in (None, "") else _f(row.get("close")),
            volume=_i(row.get("total_quantity_traded")) or _i(row.get("volume")),
            provider_timestamp=_dt(row.get("ltt")) or _dt(row.get("exchange_time")) or _dt(row.get("datetime")),
            provider=self.name,
        )

    def get_option_quote(self, instrument: Instrument) -> OptionQuote:
        if not instrument.is_option or instrument.expiry is None or instrument.strike is None:
            raise ValueError("get_option_quote requires a fully-specified OPTION instrument")
        right = _OT_TO_RIGHT.get((instrument.option_type or "").upper())
        if right is None:
            raise ValueError(f"Bad option_type {instrument.option_type!r}")
        exch = instrument.exchange.value if instrument.exchange in (Exchange.NFO, Exchange.BFO) else "NFO"

        try:
            raw = self._require_client().get_quotes(
                stock_code=(instrument.underlying or "").upper(), exchange_code=exch,
                product_type="options", expiry_date=_breeze_date(instrument.expiry),
                right=right, strike_price=_strike_str(instrument.strike),
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Breeze get_quotes(option) failed ({type(exc).__name__})") from exc

        rows = self._unwrap(raw)
        row = rows[0] if isinstance(rows, list) and rows else rows
        if not isinstance(row, dict):
            raise ProviderDataError("Unexpected Breeze option-quote payload")
        return self._norm_option_row(row, instrument.underlying or "", instrument.expiry, instrument.strike,
                                     instrument.option_type or "")

    def get_option_chain(
        self, underlying: str, expiry: date, *, right: str | None = None
    ) -> OptionChain:
        client = self._require_client()
        under = underlying.strip().upper()
        breeze_right = ""
        if right is not None:
            breeze_right = _OT_TO_RIGHT.get(right.upper(), "")

        try:
            raw = client.get_option_chain_quotes(
                stock_code=under, exchange_code="NFO", product_type="options",
                expiry_date=_breeze_date(expiry), right=breeze_right, strike_price="",
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Breeze get_option_chain_quotes failed ({type(exc).__name__})") from exc

        items = self._unwrap(raw)
        if not isinstance(items, list):
            raise ProviderDataError("Unexpected Breeze option-chain payload")

        by_strike: dict[float, dict[str, OptionQuote]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            ot = _RIGHT_TO_OT.get(str(it.get("right", "")).lower())
            strike = _f(it.get("strike_price"))
            if ot is None or strike is None:
                continue
            q = self._norm_option_row(it, under, expiry, strike, ot)
            by_strike.setdefault(strike, {})[ot] = q

        # spot + ATM
        spot: float | None = None
        try:
            spot = self.get_index_quote(_index_for_underlying(under)).ltp
        except Exception:  # noqa: BLE001 - spot is best-effort
            spot = None
        atm = _nearest(sorted(by_strike), spot) if by_strike else None

        rows = [
            OptionChainRow(strike=s, call=by_strike[s].get("CE"), put=by_strike[s].get("PE"))
            for s in sorted(by_strike)
        ]
        return OptionChain(
            underlying=under, expiry=expiry, spot=spot, atm_strike=atm,
            generated_at=datetime.now(timezone.utc), rows=rows, provider=self.name,
        )

    def get_historical_candles(
        self, instrument: Instrument, interval: str, start: datetime, end: datetime
    ) -> list[Candle]:
        client = self._require_client()
        kwargs: dict[str, Any] = {
            "interval": interval,
            "from_date": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "to_date": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if instrument.is_index:
            stock_code, exch = _INDEX_CODES[instrument.internal_symbol]
            kwargs.update(stock_code=stock_code, exchange_code=exch, product_type="cash")
        elif instrument.is_option:
            kwargs.update(
                stock_code=(instrument.underlying or "").upper(), exchange_code="NFO",
                product_type="options", expiry_date=_breeze_date(instrument.expiry),
                right=_OT_TO_RIGHT.get((instrument.option_type or "").upper(), ""),
                strike_price=_strike_str(instrument.strike),
            )
        else:
            raise ValueError(f"Unsupported instrument type for history: {instrument.instrument_type}")

        try:
            raw = client.get_historical_data_v2(**kwargs)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Breeze get_historical_data_v2 failed ({type(exc).__name__})") from exc

        items = self._unwrap(raw)
        if not isinstance(items, list):
            raise ProviderDataError("Unexpected Breeze historical payload")
        out: list[Candle] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ts = _dt(it.get("datetime")) or _dt(it.get("date"))
            o, h, low_, c = _f(it.get("open")), _f(it.get("high")), _f(it.get("low")), _f(it.get("close"))
            if ts is None or None in (o, h, low_, c):
                continue
            out.append(Candle(
                symbol=instrument.internal_symbol, interval=interval, timestamp=ts,
                open=o, high=h, low=low_, close=c,
                volume=_i(it.get("volume")), oi=_i(it.get("open_interest")) if it.get("open_interest") not in (None, "") else _i(it.get("oi")),
            ))
        return out

    # --- instrument master (Phase 5) -----------------------------
    def get_option_instruments(self, underlying: str) -> list[Instrument]:
        under = underlying.strip().upper()
        try:
            rows = (self._master_loader or _download_icici_security_master)()
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(
                f"Could not load the ICICI security master ({type(exc).__name__})"
            ) from exc

        out: list[Instrument] = []
        for row in rows:
            try:
                inst = _row_to_instrument(row, under)
            except (KeyError, ValueError, TypeError):
                continue
            if inst is not None:
                out.append(inst)
        return out

    # --- streaming ------------------------------------------------
    def subscribe(
        self,
        instruments: Sequence[Instrument],
        on_tick: TickCallback,
        *,
        resolver: Callable[[dict], Instrument | None] | None = None,
    ) -> None:
        client = self._require_client()
        self._on_tick = on_tick
        self._resolver = resolver
        if not self._ws_connected:
            try:
                client.ws_connect()
            except Exception as exc:  # noqa: BLE001
                raise ProviderConnectionError(f"Breeze ws_connect failed ({type(exc).__name__})") from exc
            client.on_ticks = self._handle_tick
            self._ws_connected = True
        for inst in instruments:
            try:
                client.subscribe_feeds(**self._feed_args(inst))
                self._subscribed.add(inst.internal_symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("breeze.subscribe_failed symbol=%s err=%s", inst.internal_symbol, type(exc).__name__)
        logger.info("market_data.subscription_started count=%d", len(self._subscribed))

    def unsubscribe(self, instruments: Sequence[Instrument]) -> None:
        if not self._ws_connected or self._client is None:
            return
        for inst in instruments:
            try:
                self._client.unsubscribe_feeds(**self._feed_args(inst))
            except Exception:  # noqa: BLE001
                pass
            self._subscribed.discard(inst.internal_symbol)
        logger.info("market_data.subscription_stopped remaining=%d", len(self._subscribed))

    def _feed_args(self, inst: Instrument) -> dict[str, Any]:
        if inst.is_index:
            stock_code, exch = _INDEX_CODES[inst.internal_symbol]
            return {"exchange_code": exch, "stock_code": stock_code, "product_type": "cash",
                    "get_exchange_quotes": True, "get_market_depth": False}
        return {
            "exchange_code": "NFO", "stock_code": (inst.underlying or "").upper(),
            # Streaming needs the security-master expiry format ("08-Sep-2026");
            # the ISO form _breeze_date() produces resolves to no feed (0 ticks).
            "product_type": "options", "expiry_date": inst.expiry.strftime("%d-%b-%Y"),
            "right": _OT_TO_RIGHT.get((inst.option_type or "").upper(), ""),
            "strike_price": _strike_str(inst.strike),
            "get_exchange_quotes": True, "get_market_depth": False,
        }

    def _handle_tick(self, tick: Any) -> None:
        if self._on_tick is None or not isinstance(tick, dict):
            return
        try:
            quote = self._norm_tick(tick)
        except Exception as exc:  # noqa: BLE001 - never let a bad tick kill the socket thread
            logger.debug("breeze.tick_normalize_failed err=%s", type(exc).__name__)
            return
        if quote is not None:
            self._on_tick(quote)

    # --- normalization -----------------------------------------
    def _norm_option_row(
        self, row: dict[str, Any], underlying: str, expiry: date, strike: float, option_type: str
    ) -> OptionQuote:
        return OptionQuote.build(
            underlying=underlying.upper(), expiry=expiry, strike=float(strike), option_type=option_type,
            ltp=_f(row.get("ltp")) or _f(row.get("last")),
            open=_f(row.get("open")),
            high=_f(row.get("high")),
            low=_f(row.get("low")),
            prev_close=_f(row.get("previous_close")) if row.get("previous_close") not in (None, "") else _f(row.get("close")),
            volume=_i(row.get("total_quantity_traded")) or _i(row.get("volume")),
            oi=_i(row.get("open_interest")) if row.get("open_interest") not in (None, "") else _i(row.get("oi")),
            oi_change=_i(row.get("oi_change")) if row.get("oi_change") not in (None, "") else _i(row.get("open_interest_change")),
            bid=_f(row.get("best_bid_price")) or _f(row.get("bid")),
            ask=_f(row.get("best_offer_price")) or _f(row.get("ask")),
            iv=_f(row.get("implied_volatility")) if row.get("implied_volatility") not in (None, "") else _f(row.get("iv")),
            vwap=_f(row.get("vwap")) if row.get("vwap") not in (None, "") else _f(row.get("average_price")),
            provider_timestamp=_dt(row.get("ltt")) or _dt(row.get("datetime")),
            provider=self.name,
        )

    def _norm_tick(self, tick: dict[str, Any]) -> IndexQuote | OptionQuote | None:
        """Breeze streams ticks keyed by a provider symbol like "4.1!NIFTY".
        We only re-key what we can resolve back to an internal symbol."""
        sym = str(tick.get("stock_code") or tick.get("symbol") or "").upper()
        internal = _tick_symbol_to_internal(sym)
        ltp = _f(tick.get("last")) or _f(tick.get("ltp")) or _f(tick.get("close"))
        prev_close = _f(tick.get("previous_close")) or _f(tick.get("prev_close"))
        if internal in _INDEX_CODES:
            return IndexQuote.build(
                internal, ltp=ltp,
                open=_f(tick.get("open")), high=_f(tick.get("high")), low=_f(tick.get("low")),
                prev_close=prev_close,
                volume=_i(tick.get("totalTradedQuantity")) or _i(tick.get("volume")),
                provider_timestamp=_dt(tick.get("ltt")) or _dt(tick.get("datetime")),
                provider=self.name,
            )
        # option tick: let the caller's resolver name the contract (the raw
        # payload shape varies; the instrument master knows it).
        if self._resolver is not None:
            inst = self._resolver(tick)
            if inst is not None and inst.is_option and inst.expiry and inst.strike:
                return OptionQuote.build(
                    underlying=inst.underlying or "", expiry=inst.expiry, strike=float(inst.strike),
                    option_type=inst.option_type or "CE",
                    ltp=ltp,
                    open=_f(tick.get("open")), high=_f(tick.get("high")), low=_f(tick.get("low")),
                    prev_close=prev_close,
                    volume=_i(tick.get("totalTradedQuantity")) or _i(tick.get("volume")),
                    oi=_i(tick.get("OI")) or _i(tick.get("oi")) or _i(tick.get("open_interest")),
                    bid=_f(tick.get("bPrice")) or _f(tick.get("best_bid_price")),
                    ask=_f(tick.get("sPrice")) or _f(tick.get("best_offer_price")),
                    provider_timestamp=_dt(tick.get("ltt")) or _dt(tick.get("datetime")),
                    provider=self.name,
                )
        return None


# --------------------------------------------------------------------------
# instrument-master helpers
# --------------------------------------------------------------------------
# ICICI publishes its own security master (NOT scraped from NSE/BSE).
_ICICI_SECURITY_MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"

# The normalized row shape ``master_loader`` must yield. The default
# downloader maps ICICI's raw columns onto these keys.
_MASTER_ROW_KEYS = (
    "underlying", "expiry", "strike", "option_type",
    "token", "lot_size", "tick_size", "exchange", "symbol",
)


def _row_to_instrument(row: dict, want_underlying: str) -> Instrument | None:
    under = str(row.get("underlying") or row.get("stock_code") or row.get("short_name") or "").strip().upper()
    if not under or under != want_underlying:
        return None
    ot = str(row.get("option_type") or row.get("right") or "").strip().upper()
    ot = {"CALL": "CE", "PUT": "PE", "CE": "CE", "PE": "PE"}.get(ot, "")
    if ot not in ("CE", "PE"):
        return None

    exp_raw = row.get("expiry") or row.get("expiry_date")
    expiry = _parse_expiry(exp_raw)
    strike = _f(row.get("strike") or row.get("strike_price"))
    if expiry is None or strike is None:
        return None

    exch_raw = str(row.get("exchange") or row.get("exchange_code") or "NFO").strip().upper()
    exchange = Exchange.BFO if exch_raw in ("BFO", "BSE") else Exchange.NFO

    return option_instrument(
        under, expiry, strike, ot,
        exchange=exchange,
        lot_size=_i(row.get("lot_size")),
        tick_size=_f(row.get("tick_size")),
        provider="icici_breeze",
        provider_token=(str(row["token"]).strip() if row.get("token") not in (None, "") else None),
    )


def _parse_expiry(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d%b%Y", "%Y-%m-%dT%H:%M:%S.000Z", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _download_icici_security_master() -> list[dict]:  # pragma: no cover - network
    """Download + unzip ICICI's published security master and yield
    normalized option rows for the F&O segments. Isolated + injectable so
    the unit suite never touches the network (Phase 20)."""
    import csv
    import io
    import urllib.request
    import zipfile

    req = urllib.request.Request(_ICICI_SECURITY_MASTER_URL, headers={"User-Agent": "cas-market-data/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()

    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            upper = name.upper()
            if "FOBSE" in upper or ("BFO" in upper and "FONSE" not in upper):
                exch = "BFO"
            elif "FONSE" in upper or "NFO" in upper:
                exch = "NFO"
            else:
                continue
            data = zf.read(name).decode("utf-8", errors="replace").splitlines()
            if not data:
                continue
            header = data[0]
            delim = "," if header.count(",") >= header.count("|") else "|"
            for raw in csv.DictReader(data, delimiter=delim):
                # ICICI headers are single-word CamelCase ("ExpiryDate",
                # "StrikePrice", "ShortName"): squash to a bare lowercase
                # key so both those and any spaced/underscored variants
                # collapse to the same name.
                r = {
                    "".join((k or "").split()).replace("_", "").lower():
                        (v.strip() if isinstance(v, str) else v)
                    for k, v in raw.items()
                    if k
                }
                ot = str(r.get("optiontype") or r.get("right") or "").strip().upper()
                if ot not in ("CE", "PE", "CALL", "PUT"):
                    continue  # futures / non-option rows
                strike = r.get("strikeprice") or r.get("strike")
                expiry = r.get("expirydate") or r.get("expiry")
                if not strike or not expiry:
                    continue
                rows.append({
                    "underlying": r.get("shortname") or r.get("symbol") or r.get("underlying") or "",
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": ot,
                    "token": r.get("token") or "",
                    "lot_size": r.get("lotsize") or r.get("minimumlotqty"),
                    "tick_size": r.get("ticksize"),
                    "exchange": exch,
                    "symbol": r.get("exchangecode") or r.get("companyname") or "",
                })
    return rows


# --------------------------------------------------------------------------
# module-level helpers
# --------------------------------------------------------------------------
def _index_for_underlying(underlying: str):
    from trading.market_data.symbols import index_instrument

    return index_instrument("NIFTY" if underlying.upper() in ("NIFTY", "NIFTY50") else underlying)


def _nearest(sorted_values: Sequence[float], target: float | None) -> float | None:
    if not sorted_values or target is None:
        return None
    return min(sorted_values, key=lambda v: abs(v - target))


def _tick_symbol_to_internal(sym: str) -> str:
    s = sym.split("!")[-1].upper()  # "4.1!NIFTY 50" -> "NIFTY 50"
    reverse = {v[0].upper(): k for k, v in _INDEX_CODES.items()}
    if s in reverse:
        return reverse[s]
    # Breeze streams NSE index ticks keyed by the feed-token display name
    # ("4.1!NIFTY 50"), not the isec stock_code we subscribe with ("NIFTY").
    # SENSEX happens to stream as "1.1!SENSEX" which already matches below.
    aliases = {
        "NIFTY": "NIFTY", "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY", "CNXNIF": "NIFTY",
        "CNXBAN": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY",
        "INDVIX": "INDIA_VIX", "INDIA VIX": "INDIA_VIX", "INDIAVIX": "INDIA_VIX", "NIFVIX": "INDIA_VIX",
        "BSESEN": "SENSEX", "SENSEX": "SENSEX",
    }
    return aliases.get(s, s)
