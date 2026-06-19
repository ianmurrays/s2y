"""Spotify auth via ``spotipy.SpotifyPKCE``.

PKCE means we only need a Client ID (no secret), which is right for a local app.

We drive the flow ourselves instead of relying on spotipy's auto-loopback server,
because that only triggers for an ``http://127.0.0.1`` redirect. To support an
HTTPS redirect (e.g. a Tailscale tunnel), we: build the authorize URL, show/open
it, and run our own catcher on the local callback port. The browser hits that port
directly (loopback) or the tunnel forwards the HTTPS request to it — same code
path. The PKCE ``code_verifier`` lives on the auth-manager instance, so the
``get_authorize_url`` and ``get_access_token`` calls must (and do) share one.
"""

from __future__ import annotations

import errno
import logging
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyPKCE

from ..config import Config, Paths, secure_existing

log = logging.getLogger(__name__)

# Read + write scopes for playlists and Liked Songs (Liked Albums use the same
# user-library scopes). Request all six up front so re-auth isn't needed later.
SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
    "user-library-modify",
)

_SUCCESS_HTML = (
    b"<html><body style='font-family:sans-serif'>"
    b"<h2>s2y is authorized \xe2\x9c\x93</h2>"
    b"<p>You can close this tab and return to the terminal.</p>"
    b"</body></html>"
)


def build_auth_manager(config: Config, paths: Paths) -> SpotifyPKCE:
    return SpotifyPKCE(
        client_id=config.spotify.client_id,
        redirect_uri=config.spotify.effective_redirect_uri,
        scope=" ".join(SCOPES),
        open_browser=False,  # we own the browser + callback capture
        cache_handler=CacheFileHandler(cache_path=str(paths.spotify_token)),
    )


def build_client(config: Config, paths: Paths) -> spotipy.Spotify:
    """A ready spotipy client (uses the cached token; refreshes as needed).

    A finite ``requests_timeout`` keeps a stuck socket from hanging the worker;
    ``retries=0``/``status_retries=0`` hand all retry/backoff to our own
    ``with_retry`` layer so we don't double-retry (and stay observable).
    """
    return spotipy.Spotify(
        auth_manager=build_auth_manager(config, paths),
        requests_timeout=config.sync.request_timeout,
        retries=0,
        status_retries=0,
    )


def authorize(
    config: Config,
    paths: Paths,
    *,
    open_browser: bool = True,
    on_url: Callable[[str], object] | None = None,
    timeout: float = 300.0,
) -> spotipy.Spotify:
    """Run the PKCE flow and return a ready client.

    ``on_url`` (if given) is called with the authorization URL so the UI can show
    it — useful when the browser is on a different device than s2y.
    """
    auth = build_auth_manager(config, paths)

    # Reuse a cached token if we have one (refreshes silently, never prompts).
    if auth.cache_handler.get_cached_token():
        auth.get_access_token(check_cache=True)
        return spotipy.Spotify(auth_manager=auth)

    url = auth.get_authorize_url()  # also generates + stores the PKCE verifier
    if on_url is not None:
        on_url(url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless box; the displayed URL still works
            log.debug("could not open a browser; user must open the URL manually")

    code = _capture_code(config.spotify.redirect_port, timeout=timeout)
    auth.get_access_token(code)  # same instance -> verifier matches; caches token
    secure_existing(paths.spotify_token)
    return spotipy.Spotify(auth_manager=auth)


def has_cached_token(config: Config, paths: Paths) -> bool:
    if not config.spotify.client_id or not paths.spotify_token.exists():
        return False
    auth = build_auth_manager(config, paths)
    return bool(auth.cache_handler.get_cached_token())


def _capture_code(port: int, *, timeout: float = 300.0) -> str:
    """Catch the OAuth redirect on ``127.0.0.1:port`` and return the auth code.

    Works for a direct loopback redirect and for a tunnel (e.g. Tailscale serve)
    that forwards an HTTPS request's path+query to this local port. Ignores stray
    requests (favicon) and waits up to ``timeout`` seconds for one carrying a
    ``code`` or ``error``.
    """
    result: dict[str, str | None] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server API
            params = parse_qs(urlparse(self.path).query)
            codes = params.get("code")
            errors = params.get("error")
            code = codes[0] if codes else None
            error = errors[0] if errors else None
            if code or error:
                result["code"] = code
                result["error"] = error
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML if (code or error) else b"s2y: waiting...")

        def log_message(self, *_args) -> None:  # silence stdout (would corrupt the TUI)
            pass

    class _Server(HTTPServer):
        allow_reuse_address = True  # set before bind (server_bind reads it)
        timeout = 1.0

    try:
        server = _Server(("127.0.0.1", port), _Handler)
    except OSError as exc:
        raise _bind_error(port, exc) from exc

    deadline = time.monotonic() + timeout
    try:
        while "code" not in result and "error" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if result.get("error"):
        raise RuntimeError(f"Spotify returned an authorization error: {result['error']}")
    if not result.get("code"):
        raise RuntimeError(
            "Timed out waiting for the Spotify redirect. Check that your redirect URI "
            f"and tunnel forward to this machine's callback port ({port})."
        )
    return result["code"]  # type: ignore[return-value]


def _bind_error(port: int, exc: OSError) -> RuntimeError:
    """Turn a raw bind OSError into an actionable message."""
    if exc.errno in (errno.EACCES, errno.EPERM):
        return RuntimeError(
            f"Permission denied opening the callback listener on port {port}. Ports "
            "below 1024 need root — set the Local callback port to a high port (e.g. "
            "8888) and forward your tunnel/redirect to that port, not 443."
        )
    if exc.errno == errno.EADDRINUSE:
        return RuntimeError(
            f"Port {port} is already in use — pick a different Local callback port."
        )
    return RuntimeError(f"Could not open the callback listener on port {port}: {exc}")
