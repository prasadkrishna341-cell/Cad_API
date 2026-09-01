"""Kite Connect login and access-token caching.

Kite access tokens are valid until roughly 6:00 AM IST the morning after they
are issued, so they must be regenerated once per trading day.  This module
caches the token in the state directory (which `.gitignore` excludes) and
refuses to hand back a stale one.

The login handshake is:

    1. open `kite.login_url()` in a browser and log in
    2. Kite redirects to your app's redirect URL with `?request_token=...`
    3. exchange that request_token + api_secret for an access_token

Step 2 can be captured automatically by a throwaway local HTTP server (when
your redirect URL points at 127.0.0.1), or you can paste the URL back in.
"""

from __future__ import annotations

import json
import logging
import re
import stat
import webbrowser
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .config import IST, ConfigError, Settings

log = logging.getLogger(__name__)

# Kite invalidates access tokens at ~6 AM IST the day after they are issued.
TOKEN_EXPIRY_TIME = dtime(6, 0)


class AuthError(RuntimeError):
    """Raised when we cannot obtain a usable access token."""


def _now_ist() -> datetime:
    return datetime.now(IST)


def token_expiry(issued_at: datetime) -> datetime:
    """The moment a token issued at `issued_at` stops working."""
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=IST)
    issued_ist = issued_at.astimezone(IST)
    expiry = datetime.combine(issued_ist.date(), TOKEN_EXPIRY_TIME, tzinfo=IST)
    if issued_ist >= expiry:
        # Issued after 6 AM -> good until 6 AM tomorrow.
        expiry += timedelta(days=1)
    return expiry


def extract_request_token(text: str) -> str:
    """Pull a request_token out of a full redirect URL, a query string, or a bare token."""
    text = text.strip()
    if not text:
        raise AuthError("No request token supplied")
    if "request_token" in text:
        query = urlparse(text).query or text.lstrip("?")
        values = parse_qs(query).get("request_token")
        if values and values[0]:
            return values[0]
        match = re.search(r"request_token[=:]\s*([A-Za-z0-9]+)", text)
        if match:
            return match.group(1)
        raise AuthError(f"Could not find a request_token in {text!r}")
    if re.fullmatch(r"[A-Za-z0-9]+", text):
        return text
    raise AuthError(f"Could not find a request_token in {text!r}")


class TokenStore:
    """Reads/writes the cached access token, with expiry awareness."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.token_file

    def load(self) -> Optional[str]:
        """Return a still-valid cached access token, or None."""
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text())
            token = payload["access_token"]
            issued_at = datetime.fromisoformat(payload["issued_at"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("Ignoring unreadable token cache %s: %s", self.path, exc)
            return None

        if payload.get("api_key") and payload["api_key"] != self.settings.api_key:
            log.warning("Cached token belongs to a different API key; ignoring")
            return None

        expiry = token_expiry(issued_at)
        if _now_ist() >= expiry:
            log.info("Cached access token expired at %s; a fresh login is needed", expiry)
            return None
        log.debug("Using cached access token (valid until %s)", expiry)
        return token

    def save(self, access_token: str, extra: Optional[dict] = None) -> None:
        self.settings.ensure_dirs()
        payload = {
            "access_token": access_token,
            "api_key": self.settings.api_key,
            "issued_at": _now_ist().isoformat(),
            **{k: v for k, v in (extra or {}).items() if k in ("user_id", "user_name", "email")},
        }
        self.path.write_text(json.dumps(payload, indent=2))
        # Token grants full account access — keep it owner-readable only.
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        log.info("Access token cached at %s (valid until %s)",
                 self.path, token_expiry(datetime.fromisoformat(payload["issued_at"])))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _capture_request_token_via_server(redirect_url: str, timeout: int = 180) -> Optional[str]:
    """Run a one-shot HTTP server on the redirect URL's port to catch the token."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    parsed = urlparse(redirect_url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return None

    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            params = parse_qs(urlparse(self.path).query)
            token = (params.get("request_token") or [""])[0]
            if token:
                captured["token"] = token
                body = b"<h2>Login captured.</h2><p>You can close this tab.</p>"
            else:
                body = b"<h2>No request_token in the redirect.</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence stdlib request logging
            pass

    try:
        server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    except OSError as exc:
        log.warning("Could not bind %s to catch the redirect: %s", redirect_url, exc)
        return None

    server.timeout = timeout
    with server:
        print(f"Waiting up to {timeout}s for the Kite redirect on {redirect_url} ...")
        server.handle_request()
    return captured.get("token")


def interactive_login(settings: Settings, open_browser: bool = True) -> str:
    """Run the full login handshake and return a fresh access token."""
    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import KiteException

    settings.require_credentials()
    kite = KiteConnect(api_key=settings.api_key)
    url = kite.login_url()

    print("\n1. Open this URL and log in to Kite:\n")
    print(f"   {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # headless box, no browser — perfectly fine
            pass

    request_token = _capture_request_token_via_server(settings.redirect_url)
    if not request_token:
        print("2. After logging in you land on your redirect URL. Paste it here")
        print("   (or just the request_token):\n")
        request_token = extract_request_token(input("   > "))

    try:
        session = kite.generate_session(request_token, api_secret=settings.api_secret)
    except KiteException as exc:
        raise AuthError(
            f"Kite rejected the login: {exc}. Request tokens are single-use and "
            "expire within minutes — generate a fresh one and retry."
        ) from exc

    access_token = session["access_token"]
    TokenStore(settings).save(access_token, session)
    print(f"\nLogged in as {session.get('user_name', session.get('user_id', '?'))}.")
    return access_token


def get_access_token(settings: Settings, allow_interactive: bool = True) -> str:
    """Return a valid access token, logging in only if the cache is stale."""
    settings.require_credentials()
    cached = TokenStore(settings).load()
    if cached:
        return cached
    if not allow_interactive:
        raise AuthError(
            "No valid access token cached. Run `python -m kitealgo.cli login` "
            "to generate one (tokens expire at 6 AM IST daily)."
        )
    return interactive_login(settings)


def build_kite_client(settings: Settings, allow_interactive: bool = True):
    """Return an authenticated `KiteConnect` client."""
    from kiteconnect import KiteConnect

    token = get_access_token(settings, allow_interactive=allow_interactive)
    kite = KiteConnect(api_key=settings.api_key)
    kite.set_access_token(token)
    return kite
