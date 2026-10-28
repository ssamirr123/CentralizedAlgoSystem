"""
Automated TOTP-based authentication for Zerodha Kite Connect.

Automates the complete Kite login flow using the `kiteconnect` SDK and `pyotp`:
  1. POST https://kite.zerodha.com/api/login with user_id + password -> request_id
  2. Generate 6-digit TOTP code via pyotp and POST https://kite.zerodha.com/api/twofa -> session cookies
  3. GET kite.login_url() + skip_session=true -> 302 redirect with request_token
  4. Call kite.generate_session(request_token, api_secret) -> access_token

Provides clear, actionable error messages for every failure stage.
All secrets (password, TOTP key, API secret, tokens) are kept strictly
in-memory and never logged or leaked.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Callable

logger = logging.getLogger("trading.market_data.zerodha_auth")


class AutoLoginError(RuntimeError):
    """Raised when automated Zerodha login fails, with specific stage details."""


def _generate_totp_builtin(secret: str) -> str:
    """Standard-library RFC 6238 TOTP fallback in case pyotp is not installed."""
    import base64
    import hashlib
    import hmac
    import struct
    import time

    key_str = secret.replace(" ", "").replace("-", "").strip().upper()
    padding = (8 - len(key_str) % 8) % 8
    key_bytes = base64.b32decode(key_str + "=" * padding)
    interval = int(time.time()) // 30
    counter = struct.pack(">Q", interval)
    mac = hmac.new(key_bytes, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def _compute_totp(totp_key: str, custom_generator: Callable[[str], str] | None = None) -> str:
    """Generate 6-digit TOTP via pyotp (with built-in RFC 6238 fallback)."""
    clean_totp_key = totp_key.replace(" ", "").replace("-", "").strip().upper()
    if custom_generator is not None:
        return custom_generator(clean_totp_key)

    try:
        import pyotp  # noqa: PLC0415

        return pyotp.TOTP(clean_totp_key).now()
    except ImportError:
        # Fallback to standard library RFC 6238 implementation
        try:
            return _generate_totp_builtin(clean_totp_key)
        except Exception as exc:  # noqa: BLE001
            raise AutoLoginError(
                f"Failed to generate TOTP code: {exc}. Please verify that ZERODHA_TOTP_KEY is a valid Base32 key."
            ) from None
    except Exception as exc:  # noqa: BLE001
        raise AutoLoginError(
            f"Failed to generate TOTP code via pyotp: {exc}. Please verify that ZERODHA_TOTP_KEY is a valid Base32 key."
        ) from None


def login_zerodha_headless(
    *,
    api_key: str,
    api_secret: str,
    user_id: str,
    password: str,
    totp_key: str,
    session_factory: Callable[[], Any] | None = None,
    kite_factory: Callable[[str], Any] | None = None,
    totp_generator: Callable[[str], str] | None = None,
) -> str:
    """Automate the Kite login flow to obtain a request_token, then call generate_session
    on the KiteConnect client to get and set the access_token.

    Returns the fresh ``access_token`` string.
    Raises ``AutoLoginError`` with a clear, descriptive message on any failure.
    """
    # 0. Validate required fields
    missing = [
        label
        for label, val in [
            ("api_key", api_key),
            ("api_secret", api_secret),
            ("user_id", user_id),
            ("password", password),
            ("totp_key", totp_key),
        ]
        if not val or not str(val).strip()
    ]
    if missing:
        raise AutoLoginError(
            f"Auto-login aborted: missing required parameter(s): {', '.join(missing)}. "
            "Please configure ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID, "
            "ZERODHA_PASSWORD, and ZERODHA_TOTP_KEY in your environment or trading/.env."
        )

    # 1. Initialize KiteConnect client
    if kite_factory is not None:
        kite = kite_factory(api_key.strip())
    else:
        try:
            from kiteconnect import KiteConnect  # noqa: PLC0415
        except ImportError as exc:
            raise AutoLoginError(
                "The 'kiteconnect' package is required for Zerodha authentication. "
                "Please run `pip install kiteconnect`."
            ) from exc
        kite = KiteConnect(api_key=api_key.strip())

    # 2. Initialize HTTP session
    if session_factory is not None:
        session = session_factory()
    else:
        try:
            import requests  # noqa: PLC0415
        except ImportError as exc:
            raise AutoLoginError("The 'requests' library is required for auto-login. Run `pip install requests`.") from exc
        session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://kite.zerodha.com/",
    }

    # Optional: seed session cookies
    try:
        session.get("https://kite.zerodha.com/", headers=headers, timeout=10)
    except Exception:  # noqa: BLE001
        pass

    # 3. Step 1: POST /api/login (User ID & Password)
    try:
        login_resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id.strip(), "password": password},
            headers=headers,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        raise AutoLoginError(
            f"Step 1 failed: network connection to Zerodha login endpoint timed out or failed ({type(exc).__name__})."
        ) from None

    if login_resp.status_code == 429:
        raise AutoLoginError(
            "Step 1 failed: Zerodha rate limit exceeded (HTTP 429). Please wait a few minutes before retrying."
        )
    if login_resp.status_code == 403:
        raise AutoLoginError(
            "Step 1 failed: Zerodha rejected login request (HTTP 403 Forbidden). "
            "Zerodha may be presenting a CAPTCHA challenge or blocking requests from this IP. "
            "Please log in manually once via browser to unblock."
        )

    try:
        login_data = login_resp.json()
    except Exception:  # noqa: BLE001
        raise AutoLoginError(
            f"Step 1 failed: unexpected non-JSON response from Zerodha login (HTTP {login_resp.status_code})."
        ) from None

    if login_data.get("status") != "success":
        err_msg = login_data.get("message") or "Credentials rejected by Zerodha"
        err_type = login_data.get("error_type")
        detail = f"{err_type}: {err_msg}" if err_type else err_msg
        raise AutoLoginError(
            f"Step 1 failed (User ID/Password rejected): {detail}. "
            f"Please verify ZERODHA_USER_ID ('{user_id.strip()}') and ZERODHA_PASSWORD."
        )

    request_id = login_data.get("data", {}).get("request_id")
    if not request_id:
        raise AutoLoginError(
            "Step 1 failed: Zerodha login reported success but no 'request_id' was returned in the payload."
        )

    # 4. Step 2: Generate TOTP code via pyotp and POST /api/twofa
    totp_value = _compute_totp(totp_key, custom_generator=totp_generator)

    try:
        twofa_resp = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": user_id.strip(),
                "request_id": request_id,
                "twofa_value": totp_value,
                "twofa_type": "totp",
                "skip_session": "true",
            },
            headers=headers,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        raise AutoLoginError(
            f"Step 2 failed: network connection during Zerodha 2FA verification failed ({type(exc).__name__})."
        ) from None

    try:
        twofa_data = twofa_resp.json()
    except Exception:  # noqa: BLE001
        raise AutoLoginError(
            f"Step 2 failed: unexpected non-JSON response from Zerodha 2FA (HTTP {twofa_resp.status_code})."
        ) from None

    if twofa_data.get("status") != "success":
        err_msg = twofa_data.get("message") or "TOTP rejected"
        err_type = twofa_data.get("error_type")
        detail = f"{err_type}: {err_msg}" if err_type else err_msg
        raise AutoLoginError(
            f"Step 2 failed (2FA TOTP rejected): {detail}. "
            "Please verify that ZERODHA_TOTP_KEY is your active Zerodha 2FA secret and your system clock is accurate."
        )

    # 5. Step 3: GET connect login URL to obtain request_token via redirect
    try:
        connect_url = kite.login_url()
    except Exception:  # noqa: BLE001
        connect_url = f"https://kite.zerodha.com/connect/login?api_key={api_key.strip()}&v=3"

    # The session is already authenticated by steps 1-2; skip_session tells Kite
    # not to show its interactive login/continue page and redirect straight to
    # the app's redirect URL with the request_token.
    if "skip_session=" not in connect_url:
        connect_url += ("&" if "?" in connect_url else "?") + "skip_session=true"

    request_token: str | None = None
    target_url = connect_url

    for _ in range(5):
        try:
            connect_resp = session.get(target_url, headers=headers, allow_redirects=False, timeout=15)
        except Exception as exc:  # noqa: BLE001
            # Check if exception occurred because redirect went to a localhost URL without an HTTP server
            failed_url = getattr(getattr(exc, "request", None), "url", None) or target_url
            if failed_url and "request_token=" in failed_url:
                parsed_fail = urllib.parse.urlparse(failed_url)
                params_fail = urllib.parse.parse_qs(parsed_fail.query)
                if params_fail.get("request_token"):
                    request_token = params_fail["request_token"][0]
                    break
            raise AutoLoginError(
                f"Step 3 failed: network connection during Kite OAuth redirect failed ({type(exc).__name__})."
            ) from None

        location = connect_resp.headers.get("Location") or getattr(connect_resp, "url", "")
        if not location:
            status_code = getattr(connect_resp, "status_code", 0)
            raise AutoLoginError(
                f"Step 3 failed: Zerodha returned HTTP {status_code} without a redirect Location. "
                "Ensure your Kite Connect app has a registered Redirect URL (e.g. 'https://127.0.0.1/') in https://developers.kite.trade."
            )

        # Inspect redirect location
        parsed_loc = urllib.parse.urlparse(location)
        loc_params = urllib.parse.parse_qs(parsed_loc.query)

        # Check for OAuth rejection in query params
        if loc_params.get("status") == ["error"] or (not loc_params.get("request_token") and loc_params.get("message")):
            err_msg = loc_params.get("message", ["OAuth authorization rejected"])[0]
            raise AutoLoginError(
                f"Step 3 failed (OAuth authorization rejected): {err_msg}. "
                f"Please ensure that ZERODHA_USER_ID ('{user_id.strip()}') is enabled for this Kite Connect app "
                f"and that ZERODHA_API_KEY is correct."
            )

        if loc_params.get("request_token"):
            request_token = loc_params["request_token"][0]
            break

        # Follow intermediate redirect if 3xx
        if connect_resp.status_code in (301, 302, 303, 307, 308):
            target_url = urllib.parse.urljoin(target_url, location)
        else:
            raise AutoLoginError(
                f"Step 3 failed: received HTTP {connect_resp.status_code} (a web page) instead of the OAuth redirect. "
                "Zerodha is likely asking for a one-time manual step. Open "
                "https://kite.zerodha.com/connect/login?v=3&api_key=<your API key> in a browser, log in, "
                "and authorize the app if prompted, then retry auto-login. Also confirm that the Kite Connect "
                "app is active and has a registered Redirect URL in https://developers.kite.trade."
            )

    if not request_token:
        raise AutoLoginError(
            "Step 3 failed: could not extract 'request_token' from OAuth redirect. "
            "Ensure the 'Redirect URL' in your Kite Connect app settings (https://developers.kite.trade) is configured "
            "(e.g. set to 'https://127.0.0.1/')."
        )

    # 6. Step 4: Exchange request_token for access_token via kiteconnect
    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret.strip())
        access_token = session_data.get("access_token")
        if not access_token:
            raise AutoLoginError(
                "Step 4 failed: KiteConnect.generate_session succeeded but no 'access_token' was found in the response."
            )
        access_token_str = str(access_token).strip()
        # Set access token on the client
        if hasattr(kite, "set_access_token"):
            kite.set_access_token(access_token_str)
        return access_token_str
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, AutoLoginError):
            raise
        exc_type = type(exc).__name__
        raise AutoLoginError(
            f"Step 4 failed (generate_session): {exc} ({exc_type}). "
            "Please verify that ZERODHA_API_SECRET matches the API Secret for your Kite Connect app."
        ) from None
