"""
Zerodha **Kite Connect** market-data provider.

Read-only: quotes, option chain, historical candles, and a streaming
websocket feed (KiteTicker). No order methods (see ``base.py``).

SDK: ``pip install kiteconnect``. Imported lazily inside ``connect()``
so this module imports fine without the dependency (CI, unit tests that
inject a fake client, code review).

Security (rules 10/11): the API key / secret / access token are never
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
from trading.market_data.symbols import Exchange, Instrument, InstrumentType, option_instrument

logger = logging.getLogger("trading.market_data.zerodha_kite")

# Internal index symbol -> Kite tradingsymbol
_INDEX_KITE_SYMBOLS: dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "INDIA_VIX": "NSE:INDIA VIX",
    "SENSEX": "BSE:SENSEX",
}

# Known standard NSE/BSE index instrument tokens on Kite
_INDEX_TOKENS: dict[str, int] = {
    "NIFTY": 256265,
    "BANKNIFTY": 260105,
    "FINNIFTY": 257801,
    "INDIA_VIX": 264969,
    "SENSEX": 265,
}

_TOKEN_TO_INDEX: dict[int, str] = {v: k for k, v in _INDEX_TOKENS.items()}

# Supported historical candle intervals in Kite Connect
_KITE_INTERVALS: dict[str, str] = {
    "1minute": "minute",
    "5minute": "5minute",
    "15minute": "15minute",
    "30minute": "30minute",
    "60minute": "60minute",
    "1day": "day",
}

BACKTEST_SUPPORTED_INTERVALS: frozenset[str] = frozenset(_KITE_INTERVALS.keys())


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
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
    s = str(v).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.000Z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(s, fmt)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


class ZerodhaKiteProvider(MarketDataProvider):
    name = "zerodha"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str = "",
        session_token: str = "",  # In Kite Connect, this is the daily access_token
        client_factory: Callable[[str], Any] | None = None,
        ticker_factory: Callable[[str, str], Any] | None = None,
        instruments_loader: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        self._access_token = session_token or ""
        self._client_factory = client_factory
        self._ticker_factory = ticker_factory
        self._instruments_loader = instruments_loader

        self._client: Any = None
        self._ticker: Any = None
        self._connected = False
        self._ws_connected = False
        self._on_tick: TickCallback | None = None
        self._resolver: Callable[[dict], Instrument | None] | None = None

        self._subscribed_tokens: set[int] = set()
        self._token_to_instrument: dict[int, Instrument] = {}
        self._instruments_cache: dict[str, list[Instrument]] = {}

    # --- lifecycle ---------------------------------------------------
    def connect(self) -> None:
        if not (self._api_key and self._access_token):
            raise ProviderAuthError(
                "ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN are not set. "
                "Provision today's Kite access token (via POST /api/market/session or env)."
            )

        if self._client_factory is not None:
            self._client = self._client_factory(self._api_key)
        else:
            try:
                from kiteconnect import KiteConnect  # noqa: PLC0415
            except ImportError as exc:
                raise ProviderConnectionError(
                    "kiteconnect is not installed; run `pip install kiteconnect`"
                ) from exc
            self._client = KiteConnect(api_key=self._api_key)

        try:
            self._client.set_access_token(self._access_token)
            # Verify session validity by requesting profile
            self._client.profile()
        except Exception as exc:  # noqa: BLE001
            exc_name = type(exc).__name__
            if "TokenException" in exc_name or "PermissionException" in exc_name:
                raise ProviderAuthError(
                    f"Zerodha session validation failed ({exc_name}). "
                    "The daily access token is missing or expired."
                ) from None
            if "NetworkException" in exc_name or "Connection" in exc_name:
                raise ProviderConnectionError(
                    f"Zerodha connection error ({exc_name})"
                ) from None
            raise ProviderAuthError(
                f"Zerodha session check failed ({exc_name})."
            ) from None

        self._connected = True
        logger.info("zerodha.connected provider=%s", self.name)

    def disconnect(self) -> None:
        if self._ws_connected and self._ticker is not None:
            try:
                self._ticker.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws_connected = False
        self._connected = False
        self._subscribed_tokens.clear()
        self._token_to_instrument.clear()
        self._on_tick = None
        logger.info("zerodha.disconnected provider=%s", self.name)

    def is_connected(self) -> bool:
        return self._connected

    def _require_client(self) -> Any:
        if not self._connected or self._client is None:
            raise ProviderConnectionError("Zerodha provider is not connected; call connect() first")
        return self._client

    # --- snapshot reads ----------------------------------------------
    def get_index_quote(self, instrument: Instrument) -> IndexQuote:
        if not instrument.is_index:
            raise ValueError(f"get_index_quote requires an INDEX instrument, got {instrument.instrument_type}")
        kite_sym = _INDEX_KITE_SYMBOLS.get(instrument.internal_symbol)
        if not kite_sym:
            raise ValueError(f"No Zerodha mapping for index {instrument.internal_symbol!r}")

        try:
            raw = self._require_client().quote([kite_sym])
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Zerodha quote failed ({type(exc).__name__})") from exc

        data = raw.get(kite_sym)
        if not isinstance(data, dict):
            raise ProviderDataError(f"Unexpected Zerodha quote payload for {instrument.internal_symbol}")

        ohlc = data.get("ohlc") or {}
        return IndexQuote.build(
            symbol=instrument.internal_symbol,
            ltp=_f(data.get("last_price")),
            open=_f(ohlc.get("open")),
            high=_f(ohlc.get("high")),
            low=_f(ohlc.get("low")),
            prev_close=_f(ohlc.get("close")),
            volume=_i(data.get("volume")),
            provider_timestamp=_dt(data.get("timestamp")),
            provider=self.name,
        )

    def get_option_quote(self, instrument: Instrument) -> OptionQuote:
        if not instrument.is_option:
            raise ValueError(f"get_option_quote requires an OPTION instrument, got {instrument.instrument_type}")

        # In Kite, quote accepts either integer instrument_token or "NFO:TRADINGSYMBOL"
        token = instrument.provider_token
        query_key = int(token) if token and token.isdigit() else f"NFO:{instrument.internal_symbol}"
        dict_key = str(query_key)

        try:
            raw = self._require_client().quote([query_key])
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Zerodha option quote failed ({type(exc).__name__})") from exc

        data = raw.get(query_key) or raw.get(dict_key)
        if not isinstance(data, dict):
            raise ProviderDataError(f"Unexpected Zerodha quote payload for {instrument.internal_symbol}")

        return self._norm_option_data(data, instrument)

    def get_option_chain(
        self,
        underlying: str,
        expiry: date,
        *,
        right: str | None = None,
    ) -> OptionChain:
        under = underlying.upper()
        all_contracts = self.get_option_instruments(under)
        contracts = [
            c for c in all_contracts
            if c.expiry == expiry and (right is None or c.option_type == right.upper())
        ]

        if not contracts:
            return OptionChain(
                underlying=under,
                expiry=expiry,
                spot=None,
                atm_strike=None,
                generated_at=datetime.now(timezone.utc),
                rows=[],
                provider=self.name,
            )

        # Batch query Kite quote API (up to 250 contracts per call)
        query_tokens = [int(c.provider_token) for c in contracts if c.provider_token and c.provider_token.isdigit()]
        quotes_map: dict[str, dict] = {}
        client = self._require_client()

        chunk_size = 200
        for i in range(0, len(query_tokens), chunk_size):
            chunk = query_tokens[i : i + chunk_size]
            try:
                resp = client.quote(chunk)
                if isinstance(resp, dict):
                    for k, v in resp.items():
                        quotes_map[str(k)] = v
            except Exception as exc:  # noqa: BLE001
                raise ProviderConnectionError(f"Zerodha batch quote failed ({type(exc).__name__})") from exc

        # Also get spot
        spot: float | None = None
        kite_idx_sym = _INDEX_KITE_SYMBOLS.get(under)
        if kite_idx_sym:
            try:
                idx_resp = client.quote([kite_idx_sym])
                if isinstance(idx_resp, dict) and kite_idx_sym in idx_resp:
                    spot = _f(idx_resp[kite_idx_sym].get("last_price"))
            except Exception:  # noqa: BLE001
                pass

        # Build rows grouped by strike
        by_strike: dict[float, dict[str, OptionQuote]] = {}
        for c in contracts:
            q_data = quotes_map.get(str(c.provider_token)) or quotes_map.get(c.provider_token or "")
            q = self._norm_option_data(q_data, c) if q_data else None
            if c.strike is not None and c.option_type:
                by_strike.setdefault(c.strike, {})[c.option_type] = q

        sorted_strikes = sorted(by_strike.keys())
        atm = min(sorted_strikes, key=lambda s: abs(s - spot)) if (sorted_strikes and spot is not None) else None

        rows = [
            OptionChainRow(
                strike=s,
                call=by_strike[s].get("CE"),
                put=by_strike[s].get("PE"),
            )
            for s in sorted_strikes
        ]

        return OptionChain(
            underlying=under,
            expiry=expiry,
            spot=spot,
            atm_strike=atm,
            generated_at=datetime.now(timezone.utc),
            rows=rows,
            provider=self.name,
        )

    def get_historical_candles(
        self,
        instrument: Instrument,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        token: int | None = None
        if instrument.provider_token and instrument.provider_token.isdigit():
            token = int(instrument.provider_token)
        elif instrument.is_index:
            token = _INDEX_TOKENS.get(instrument.internal_symbol)

        if token is None:
            raise ValueError(f"Cannot resolve Zerodha instrument token for {instrument.internal_symbol}")

        kite_interval = _KITE_INTERVALS.get(interval)
        if not kite_interval:
            raise ValueError(
                f"Unsupported interval {interval!r} for Zerodha. "
                f"Supported: {', '.join(sorted(BACKTEST_SUPPORTED_INTERVALS))}"
            )

        client = self._require_client()
        try:
            raw_candles = client.historical_data(
                instrument_token=token,
                from_date=start,
                to_date=end,
                interval=kite_interval,
                continuous=False,
                oi=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Zerodha historical_data failed ({type(exc).__name__})") from exc

        if not isinstance(raw_candles, list):
            raise ProviderDataError(f"Unexpected historical data shape for {instrument.internal_symbol}")

        candles: list[Candle] = []
        for r in raw_candles:
            if not isinstance(r, dict):
                continue
            dt_val = _dt(r.get("date"))
            if dt_val is None:
                continue
            candles.append(
                Candle(
                    symbol=instrument.internal_symbol,
                    interval=interval,
                    timestamp=dt_val,
                    open=float(r.get("open", 0.0)),
                    high=float(r.get("high", 0.0)),
                    low=float(r.get("low", 0.0)),
                    close=float(r.get("close", 0.0)),
                    volume=_i(r.get("volume")),
                    oi=_i(r.get("oi")),
                )
            )
        return candles

    # --- instrument master -------------------------------------------
    def get_option_instruments(self, underlying: str) -> list[Instrument]:
        under = underlying.upper()
        if under in self._instruments_cache:
            return self._instruments_cache[under]

        if self._instruments_loader is not None:
            raw_list = self._instruments_loader("NFO")
        else:
            client = self._require_client()
            try:
                raw_list = client.instruments("NFO")
            except Exception as exc:  # noqa: BLE001
                raise ProviderConnectionError(f"Zerodha instruments fetch failed ({type(exc).__name__})") from exc

        results: list[Instrument] = []
        for r in raw_list:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "").strip().upper()
            if name != under:
                continue
            ot = str(r.get("instrument_type") or "").strip().upper()
            if ot not in ("CE", "PE"):
                continue

            exp = _parse_date(r.get("expiry"))
            strike = _f(r.get("strike"))
            token = r.get("instrument_token")
            if exp is None or strike is None or token is None:
                continue

            results.append(
                option_instrument(
                    underlying=under,
                    expiry=exp,
                    strike=strike,
                    option_type=ot,
                    exchange=Exchange.NFO,
                    lot_size=_i(r.get("lot_size")),
                    tick_size=_f(r.get("tick_size")),
                    provider=self.name,
                    provider_token=str(token),
                )
            )

        self._instruments_cache[under] = results
        return results

    # --- streaming --------------------------------------------------
    def subscribe(
        self,
        instruments: Sequence[Instrument],
        on_tick: TickCallback,
        *,
        resolver: Callable[[dict], Instrument | None] | None = None,
    ) -> None:
        self._on_tick = on_tick
        self._resolver = resolver

        tokens_to_sub: list[int] = []
        for inst in instruments:
            token: int | None = None
            if inst.provider_token and inst.provider_token.isdigit():
                token = int(inst.provider_token)
            elif inst.is_index:
                token = _INDEX_TOKENS.get(inst.internal_symbol)

            if token is not None:
                self._token_to_instrument[token] = inst
                self._subscribed_tokens.add(token)
                tokens_to_sub.append(token)

        if not self._ws_connected:
            self._start_ticker()
        elif self._ticker and tokens_to_sub:
            try:
                self._ticker.subscribe(tokens_to_sub)
                self._ticker.set_mode(self._ticker.MODE_FULL, tokens_to_sub)
            except Exception as exc:  # noqa: BLE001
                logger.warning("zerodha.ws subscribe failed: %s", type(exc).__name__)

    def unsubscribe(self, instruments: Sequence[Instrument]) -> None:
        tokens_to_unsub: list[int] = []
        for inst in instruments:
            token: int | None = None
            if inst.provider_token and inst.provider_token.isdigit():
                token = int(inst.provider_token)
            elif inst.is_index:
                token = _INDEX_TOKENS.get(inst.internal_symbol)

            if token is not None:
                self._subscribed_tokens.discard(token)
                self._token_to_instrument.pop(token, None)
                tokens_to_unsub.append(token)

        if self._ws_connected and self._ticker and tokens_to_unsub:
            try:
                self._ticker.unsubscribe(tokens_to_unsub)
            except Exception as exc:  # noqa: BLE001
                logger.warning("zerodha.ws unsubscribe failed: %s", type(exc).__name__)

    def _start_ticker(self) -> None:
        if self._ticker_factory is not None:
            self._ticker = self._ticker_factory(self._api_key, self._access_token)
        else:
            try:
                from kiteconnect import KiteTicker  # noqa: PLC0415
            except ImportError as exc:
                raise ProviderConnectionError(
                    "kiteconnect is not installed; run `pip install kiteconnect`"
                ) from exc
            self._ticker = KiteTicker(self._api_key, self._access_token)

        def on_ticks(ws, ticks):
            for t in ticks:
                self._handle_tick(t)

        def on_connect(ws, response):
            self._ws_connected = True
            tokens = list(self._subscribed_tokens)
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
            logger.info("zerodha.ws connected tokens=%d", len(tokens))

        def on_close(ws, code, reason):
            self._ws_connected = False
            logger.info("zerodha.ws closed code=%s reason=%s", code, reason)

        def on_error(ws, code, reason):
            logger.warning("zerodha.ws error code=%s reason=%s", code, reason)

        self._ticker.on_ticks = on_ticks
        self._ticker.on_connect = on_connect
        self._ticker.on_close = on_close
        self._ticker.on_error = on_error

        try:
            self._ticker.connect(threaded=True)
            self._ws_connected = True
        except Exception as exc:  # noqa: BLE001
            raise ProviderConnectionError(f"Zerodha KiteTicker connect failed ({type(exc).__name__})") from exc

    def _handle_tick(self, tick: Any) -> None:
        if self._on_tick is None or not isinstance(tick, dict):
            return

        try:
            token = tick.get("instrument_token")
            inst = self._token_to_instrument.get(token) if token else None

            if inst is None and self._resolver is not None:
                inst = self._resolver(tick)

            if inst is None and token in _TOKEN_TO_INDEX:
                from trading.market_data.symbols import index_instrument

                inst = index_instrument(_TOKEN_TO_INDEX[token])

            if inst is None:
                return

            ltp = _f(tick.get("last_price"))
            ohlc = tick.get("ohlc") or {}
            open_val = _f(ohlc.get("open"))
            high_val = _f(ohlc.get("high"))
            low_val = _f(ohlc.get("low"))
            prev_close = _f(ohlc.get("close"))
            volume = _i(tick.get("volume_traded")) or _i(tick.get("volume"))
            timestamp = _dt(tick.get("last_trade_time")) or _dt(tick.get("timestamp"))

            if inst.is_index:
                quote = IndexQuote.build(
                    symbol=inst.internal_symbol,
                    ltp=ltp,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    prev_close=prev_close,
                    volume=volume,
                    provider_timestamp=timestamp,
                    provider=self.name,
                )
                self._on_tick(quote)
            elif inst.is_option and inst.expiry and inst.strike:
                depth = tick.get("depth") or {}
                buys = depth.get("buy") or []
                sells = depth.get("sell") or []
                bid = _f(buys[0].get("price")) if buys else None
                ask = _f(sells[0].get("price")) if sells else None
                vwap = _f(tick.get("average_traded_price")) or _f(tick.get("average_price"))
                oi = _i(tick.get("oi"))

                quote = OptionQuote.build(
                    underlying=inst.underlying or "",
                    expiry=inst.expiry,
                    strike=float(inst.strike),
                    option_type=inst.option_type or "CE",
                    ltp=ltp,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    prev_close=prev_close,
                    volume=volume,
                    oi=oi,
                    bid=bid,
                    ask=ask,
                    vwap=vwap,
                    provider_timestamp=timestamp,
                    provider=self.name,
                )
                self._on_tick(quote)
        except Exception as exc:  # noqa: BLE001
            logger.debug("zerodha.tick_normalize_failed err=%s", type(exc).__name__)

    def _norm_option_data(self, data: dict, inst: Instrument) -> OptionQuote:
        ohlc = data.get("ohlc") or {}
        depth = data.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []
        bid = _f(buys[0].get("price")) if buys else None
        ask = _f(sells[0].get("price")) if sells else None

        return OptionQuote.build(
            underlying=inst.underlying or "",
            expiry=inst.expiry or date.today(),
            strike=float(inst.strike or 0.0),
            option_type=inst.option_type or "CE",
            ltp=_f(data.get("last_price")),
            open=_f(ohlc.get("open")),
            high=_f(ohlc.get("high")),
            low=_f(ohlc.get("low")),
            prev_close=_f(ohlc.get("close")),
            volume=_i(data.get("volume")),
            oi=_i(data.get("oi")),
            bid=bid,
            ask=ask,
            vwap=_f(data.get("average_price")),
            provider_timestamp=_dt(data.get("timestamp")),
            provider=self.name,
        )
