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


def _default_secrets_loader(secret_id: str, region: str) -> dict:  # pragma: no cover - needs AWS
    import boto3

    client = boto3.client("secretsmanager", region_name=region or None)
    resp = client.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString") or "{}"
    data = json.loads(raw)
    return {
        "api_key": data.get("api_key") or data.get("BREEZE_API_KEY") or "",
        "secret_key": data.get("secret_key") or data.get("BREEZE_SECRET_KEY") or "",
        "session_token": data.get("session_token") or data.get("BREEZE_SESSION_TOKEN") or "",
    }


class BreezeSessionManager:
    """Resolves + validates the Breeze session. Thread-safe. One instance
    per process (see ``get_session_manager``)."""

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
            api_key = s.breeze_api_key
            secret_key = s.breeze_secret_key
            session_token = s.breeze_session_token
            source = "env" if (api_key or secret_key or session_token) else "unset"

            if s.breeze_secret_id:
                try:
                    sm = self._secrets_loader(s.breeze_secret_id, s.aws_region)
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

    # --- validation --------------------------------------------
    def check(self, *, publish: bool = True) -> SessionCheck:
        """Attempt to establish a Breeze session with the resolved
        credentials and classify the outcome. Never raises."""
        creds = self.credentials()
        now = datetime.now(timezone.utc)

        if not creds.has_app_creds:
            result = SessionCheck(SessionState.NOT_CONFIGURED, now,
                                  "BREEZE_API_KEY / BREEZE_SECRET_KEY are not set")
        elif not creds.session_token:
            result = SessionCheck(SessionState.SESSION_REQUIRED, now,
                                  "No Breeze session token. Generate today's token and POST it to "
                                  "/api/market/session.")
        else:
            result = self._probe(creds, now)

        with self._lock:
            self._last_check = result
        if publish:
            self._publish(creds, result)
        _log_state(result)
        return result

    def _probe(self, creds: BreezeCredentials, now: datetime) -> SessionCheck:
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
            # Deliberately do NOT forward the provider's exception text --
            # a fixed, safe message so a session token can never ride out
            # in `detail` -> FEED_STATUS -> the API.
            return SessionCheck(
                SessionState.SESSION_REQUIRED, now,
                "Breeze rejected the session (token missing, invalid or expired). "
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
            return SessionCheck(SessionState.VALID, now, "Breeze session established")

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
