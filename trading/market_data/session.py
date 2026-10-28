"""
Phase 3 -- ICICI Breeze authentication / session handling.

Breeze uses: API key + secret key + a **daily** session token the user
generates by logging in at
``https://api.icicidirect.com/apiuser/login?api_key=<url-encoded key>``
(web flow + OTP). There is no supported programmatic login, so this
module does NOT attempt one (rule 14). It resolves whichever session
token is currently available, validates it, and reports state.

Resolution precedence (highest first):
  1. runtime override  -- set via POST /api/market/session this process
  2. AWS Secrets Manager -- if BREEZE_SECRET_ID is set (read lazily)
  3. environment       -- BREEZE_* (or legacy ICICI_BREEZE_*)

Security (rules 10/11): the key / secret / token are never logged and
never returned by an API. Only booleans, a source label and an
irreversible fingerprint leave this module.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from trading.core.config import Settings, load_settings
from trading.market_data.providers import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    create_market_data_provider,
)
from trading.market_data.status import (
    FEED_STATUS,
    SessionCheck,
    SessionState,
    fingerprint,
)

logger = logging.getLogger("trading.market_data.session")


@dataclass(frozen=True)
class BreezeCredentials:
    api_key: str
    secret_key: str
    session_token: str
    source: str  # "runtime" | "secrets_manager" | "env" | "unset"

    @property
    def has_app_creds(self) -> bool:
        return bool(self.api_key and self.secret_key)

    @property
    def is_complete(self) -> bool:
        return bool(self.api_key and self.secret_key and self.session_token)

    def redacted(self) -> dict:
        return {
            "source": self.source,
            "api_key_set": bool(self.api_key),
            "secret_key_set": bool(self.secret_key),
            "session_token_set": bool(self.session_token),
            "session_token_fingerprint": fingerprint(self.session_token),
        }


SecretsLoader = Callable[[str, str], dict]  # (secret_id, region) -> {"api_key","secret_key","session_token"}
ProviderFactory = Callable[..., Any]


MarketDataCredentials = BreezeCredentials


def _default_secrets_loader(secret_id: str, region: str) -> dict:  # pragma: no cover - needs AWS
    import boto3

    client = boto3.client("secretsmanager", region_name=region or None)
    resp = client.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString") or "{}"
    data = json.loads(raw)
    return {
        "api_key": data.get("api_key") or data.get("ZERODHA_API_KEY") or data.get("BREEZE_API_KEY") or "",
        "secret_key": data.get("secret_key") or data.get("api_secret") or data.get("ZERODHA_API_SECRET") or data.get("BREEZE_SECRET_KEY") or "",
        "session_token": data.get("session_token") or data.get("access_token") or data.get("ZERODHA_ACCESS_TOKEN") or data.get("BREEZE_SESSION_TOKEN") or "",
    }


class BreezeSessionManager:
    """Resolves + validates the market data session (Zerodha Kite or ICICI Breeze).
    Thread-safe. One instance per process (see ``get_session_manager``)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider_factory: ProviderFactory | None = None,
        secrets_loader: SecretsLoader | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._provider_factory = provider_factory or create_market_data_provider
        self._secrets_loader = secrets_loader or _default_secrets_loader
        self._lock = threading.RLock()
        self._runtime: dict[str, str] = {}  # api_key / secret_key / session_token overrides
        self._last_check: SessionCheck | None = None

    # --- credential resolution ------------------------------------
    def set_credentials(
        self, *, session_token: str, api_key: str | None = None, secret_key: str | None = None
    ) -> None:
        """Runtime override, from POST /api/market/session. Persists only
        for the life of this process -- for restart-survival also update
        the Secrets Manager secret / env (documented in the runbook)."""
        with self._lock:
            self._runtime["session_token"] = session_token.strip()
            if api_key:
                self._runtime["api_key"] = api_key.strip()
            if secret_key:
                self._runtime["secret_key"] = secret_key.strip()
        logger.info(
            "market_data.session updated via API (fingerprint=%s)", fingerprint(session_token.strip())
        )

    def clear_runtime(self) -> None:
        with self._lock:
            self._runtime.clear()

    def credentials(self) -> BreezeCredentials:
        with self._lock:
            s = self._settings
            provider = (s.market_data_provider or "zerodha").lower()
            is_zerodha = provider in ("zerodha", "kite", "zerodha_kite")

            if is_zerodha:
                api_key = s.zerodha_api_key
                secret_key = s.zerodha_api_secret
                session_token = s.zerodha_access_token
                secret_id = s.zerodha_secret_id
            else:
                api_key = s.breeze_api_key
                secret_key = s.breeze_secret_key
                session_token = s.breeze_session_token
                secret_id = s.breeze_secret_id

            source = "env" if (api_key or secret_key or session_token) else "unset"

            if secret_id:
                try:
                    sm = self._secrets_loader(secret_id, s.aws_region)
                    api_key = sm.get("api_key") or api_key
                    secret_key = sm.get("secret_key") or secret_key
                    session_token = sm.get("session_token") or session_token
                    source = "secrets_manager"
                except Exception as exc:  # noqa: BLE001 - never crash resolution on AWS
                    logger.warning("market_data.session secrets-manager read failed: %s", type(exc).__name__)

            rt = self._runtime
            if rt.get("api_key"):
                api_key = rt["api_key"]
            if rt.get("secret_key"):
                secret_key = rt["secret_key"]
            if rt.get("session_token"):
                session_token = rt["session_token"]
                source = "runtime"

            return BreezeCredentials(
                api_key=api_key or "", secret_key=secret_key or "",
                session_token=session_token or "", source=source,
            )

    def can_auto_login(self) -> bool:
        provider = (self._settings.market_data_provider or "zerodha").lower()
        if provider not in ("zerodha", "kite", "zerodha_kite"):
            return False
        s = self._settings
        creds = self.credentials()
        user_id = self._runtime.get("user_id") or s.zerodha_user_id
        password = self._runtime.get("password") or s.zerodha_password
        totp_key = self._runtime.get("totp_key") or s.zerodha_totp_key
        return bool(creds.api_key and creds.secret_key and user_id and password and totp_key)

    def auto_login(
        self,
        *,
        user_id: str | None = None,
        password: str | None = None,
        totp_key: str | None = None,
    ) -> SessionCheck:
        """Perform automated headless login to Zerodha Kite and establish session."""
        now = datetime.now(timezone.utc)
        provider = (self._settings.market_data_provider or "zerodha").lower()
        if provider not in ("zerodha", "kite", "zerodha_kite"):
            return SessionCheck(SessionState.ERROR, now, "Auto-login is only supported for Zerodha Kite")

        s = self._settings
        creds = self.credentials()
        uid = (user_id or self._runtime.get("user_id") or s.zerodha_user_id or "").strip()
        pwd = password or self._runtime.get("password") or s.zerodha_password or ""
        totp = (totp_key or self._runtime.get("totp_key") or s.zerodha_totp_key or "").strip()

        if not (creds.api_key and creds.secret_key and uid and pwd and totp):
            return SessionCheck(
                SessionState.SESSION_REQUIRED,
                now,
                "Auto-login requires ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID, ZERODHA_PASSWORD, and ZERODHA_TOTP_KEY.",
            )

        try:
            from trading.market_data.zerodha_auth import login_zerodha_headless

            access_token = login_zerodha_headless(
                api_key=creds.api_key,
                api_secret=creds.secret_key,
                user_id=uid,
                password=pwd,
                totp_key=totp,
            )
            self.set_credentials(session_token=access_token)
            logger.info("zerodha.auto_login succeeded (token fingerprint=%s)", fingerprint(access_token))
            return self._probe(self.credentials(), now)
        except Exception as exc:  # noqa: BLE001
            detail = _sanitize(str(exc))
            logger.warning("zerodha.auto_login failed: %s", detail)
            return SessionCheck(
                SessionState.SESSION_REQUIRED,
                now,
                f"Zerodha auto-login failed: {detail}",
            )

    # --- validation --------------------------------------------
    def check(self, *, publish: bool = True) -> SessionCheck:
        """Attempt to establish a session with the resolved
        credentials and classify the outcome. Never raises."""
        creds = self.credentials()
        now = datetime.now(timezone.utc)
        provider = (self._settings.market_data_provider or "zerodha").lower()
        is_zerodha = provider in ("zerodha", "kite", "zerodha_kite")

        if not creds.has_app_creds:
            msg = (
                "ZERODHA_API_KEY is not set"
                if is_zerodha
                else "BREEZE_API_KEY / BREEZE_SECRET_KEY are not set"
            )
            result = SessionCheck(SessionState.NOT_CONFIGURED, now, msg)
        elif not creds.session_token:
            if is_zerodha and self.can_auto_login():
                logger.info("Zerodha access_token missing. Attempting automated daily login...")
                result = self.auto_login()
            else:
                msg = (
                    "No Zerodha access token. Generate today's access token and POST it to /api/market/session."
                    if is_zerodha
                    else "No Breeze session token. Generate today's token and POST it to /api/market/session."
                )
                result = SessionCheck(SessionState.SESSION_REQUIRED, now, msg)
        else:
            result = self._probe(creds, now)
            # If token expired and we can auto-login, try auto-login once
            if result.state == SessionState.SESSION_REQUIRED and is_zerodha and self.can_auto_login():
                logger.info("Zerodha session expired. Attempting automated daily re-login...")
                result = self.auto_login()

        with self._lock:
            self._last_check = result
        if publish:
            self._publish(creds, result)
        _log_state(result)
        return result

    def _probe(self, creds: BreezeCredentials, now: datetime) -> SessionCheck:
        provider_name = (self._settings.market_data_provider or "zerodha").lower()
        is_zerodha = provider_name in ("zerodha", "kite", "zerodha_kite")
        try:
            provider = self._provider_factory(
                self._settings.market_data_provider,
                api_key=creds.api_key, api_secret=creds.secret_key,
                session_token=creds.session_token,
            )
        except Exception as exc:  # noqa: BLE001
            return SessionCheck(SessionState.ERROR, now, f"provider init failed ({type(exc).__name__})")
        try:
            provider.connect()
        except ProviderAuthError:
            prov_name = "Zerodha" if is_zerodha else "Breeze"
            token_name = "access token" if is_zerodha else "session token"
            return SessionCheck(
                SessionState.SESSION_REQUIRED, now,
                f"{prov_name} rejected the session ({token_name} missing, invalid or expired). "
                "Provision today's token via POST /api/market/session.",
            )
        except ProviderConnectionError as exc:
            return SessionCheck(SessionState.ERROR, now, _sanitize(str(exc)))
        except ProviderError as exc:
            return SessionCheck(SessionState.ERROR, now, _sanitize(str(exc)))
        except Exception as exc:  # noqa: BLE001
            return SessionCheck(SessionState.ERROR, now, f"unexpected ({type(exc).__name__})")
        else:
            try:
                provider.disconnect()
            except Exception:  # noqa: BLE001
                pass
            prov_label = "Zerodha Kite" if is_zerodha else "Breeze"
            return SessionCheck(SessionState.VALID, now, f"{prov_label} session established")

    # --- status --------------------------------------------
    @property
    def last_check(self) -> SessionCheck | None:
        with self._lock:
            return self._last_check

    def state(self) -> SessionState:
        lc = self.last_check
        return lc.state if lc else SessionState.UNKNOWN

    def _publish(self, creds: BreezeCredentials, result: SessionCheck) -> None:
        FEED_STATUS.update(
            provider=self._settings.market_data_provider,
            enabled=self._settings.market_data_enabled,
            session_state=result.state,
            credentials_source=creds.source,
            api_key_set=bool(creds.api_key),
            secret_key_set=bool(creds.secret_key),
            session_token_set=bool(creds.session_token),
            session_token_fingerprint=fingerprint(creds.session_token),
            last_session_check=result.checked_at,
            last_error=result.detail if result.state in (SessionState.ERROR, SessionState.SESSION_REQUIRED) else None,
        )


def _sanitize(msg: str) -> str:
    """Belt-and-braces for API-facing error text: run it through the
    central log redactor, then cap the length."""
    try:
        from trading.common.logger import redact_text

        msg = redact_text(msg)
    except Exception:  # noqa: BLE001
        pass
    return msg[:240]


def _log_state(result: SessionCheck) -> None:
    if result.state == SessionState.VALID:
        logger.info("breeze.session state=VALID")
    elif result.state == SessionState.SESSION_REQUIRED:
        logger.warning("breeze.session_invalid state=SESSION_REQUIRED detail=%s", result.detail)
    elif result.state == SessionState.ERROR:
        logger.error("breeze.session state=ERROR detail=%s", result.detail)
    else:
        logger.info("breeze.session state=%s", result.state.value)


# --- process singleton -----------------------------------------------------
_manager: BreezeSessionManager | None = None
_manager_lock = threading.Lock()


def get_session_manager() -> BreezeSessionManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = BreezeSessionManager()
    return _manager


def set_session_manager(mgr: BreezeSessionManager | None) -> None:
    """Test hook."""
    global _manager
    with _manager_lock:
        _manager = mgr
