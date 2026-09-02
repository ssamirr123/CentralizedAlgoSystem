"""
Session + feed state, shared between the Breeze session manager (Phase 3),
the scheduler (Phase 4) and the API (Phase 3/14).

Nothing here ever carries a credential or a session token -- only booleans,
a source label, and an irreversible fingerprint.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class SessionState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"      # no API key / secret at all
    SESSION_REQUIRED = "SESSION_REQUIRED"  # key+secret present; token missing / expired / rejected
    VALID = "VALID"
    ERROR = "ERROR"                        # check failed for a non-auth reason (network, provider outage)
    UNKNOWN = "UNKNOWN"                    # not checked yet this process


class FeedState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SESSION_REQUIRED = "SESSION_REQUIRED"
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    STALE = "STALE"
    ERROR = "ERROR"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


def fingerprint(secret: str) -> str | None:
    """Short, irreversible tag so an operator can confirm *which* token is
    loaded without the value ever being exposed."""
    if not secret:
        return None
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class SessionCheck:
    state: SessionState
    checked_at: datetime
    detail: str | None = None  # human-readable, already sanitized (never a token)


@dataclass
class MarketFeedStatus:
    """Mutable process-wide snapshot the API reads. Guarded by a lock so
    the scheduler / worker threads can update it safely."""

    session_state: SessionState = SessionState.UNKNOWN
    feed_state: FeedState = FeedState.STOPPED
    provider: str = "icici_breeze"
    enabled: bool = False
    credentials_source: str = "unset"
    api_key_set: bool = False
    secret_key_set: bool = False
    session_token_set: bool = False
    session_token_fingerprint: str | None = None
    last_session_check: datetime | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_tick_at: datetime | None = None
    symbols_live: int = 0
    option_contracts_subscribed: int = 0
    reconnect_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                if not hasattr(self, k):
                    raise AttributeError(f"MarketFeedStatus has no field {k!r}")
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "provider": self.provider,
                "enabled": self.enabled,
                "session_state": self.session_state.value,
                "feed_state": self.feed_state.value,
                "credentials": {
                    "source": self.credentials_source,
                    "api_key_set": self.api_key_set,
                    "secret_key_set": self.secret_key_set,
                    "session_token_set": self.session_token_set,
                    "session_token_fingerprint": self.session_token_fingerprint,
                },
                "last_session_check": _iso(self.last_session_check),
                "last_error": self.last_error,
                "started_at": _iso(self.started_at),
                "stopped_at": _iso(self.stopped_at),
                "last_tick_at": _iso(self.last_tick_at),
                "symbols_live": self.symbols_live,
                "option_contracts_subscribed": self.option_contracts_subscribed,
                "reconnect_count": self.reconnect_count,
            }


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# Process-wide singleton. The session manager, scheduler and worker all
# mutate this; the API only reads snapshot().
FEED_STATUS = MarketFeedStatus()
