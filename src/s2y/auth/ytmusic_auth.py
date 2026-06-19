"""YouTube Music auth via ``ytmusicapi``.

Two modes:

* **browser** (default): the user pastes request headers captured from
  ``music.youtube.com`` DevTools. We accept BOTH Firefox "Copy Request Headers"
  and Chrome "Copy as cURL" and normalize internally (the idea from PR #249), so
  the user doesn't have to care which browser they used.
* **oauth** (opt-in): a Google Cloud "TVs and Limited Input devices" client
  (client_id + client_secret), required by Google since Nov 2024. Flakier, so
  it's not the default.
"""

from __future__ import annotations

import re

import ytmusicapi
from ytmusicapi import OAuthCredentials, YTMusic

from ..config import Config, Paths, secure_existing
from ..services.base import TimeoutSession

_HEADER_RE = re.compile(r"(?:-H|--header)\s+(['\"])(.*?)\1", re.DOTALL)
_COOKIE_RE = re.compile(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", re.DOTALL)


def normalize_headers(raw: str) -> str:
    """Return raw ``name: value`` header lines from either Firefox or Chrome input.

    Firefox "Copy Request Headers" already yields that format and is returned
    as-is. Chrome "Copy as cURL" is parsed into the same format.
    """
    text = raw.strip()
    if _looks_like_curl(text):
        return _headers_from_curl(text)
    return text


def _looks_like_curl(text: str) -> bool:
    head = text.lstrip()
    return head.startswith("curl") or " -H " in text or "--header" in text or "--cookie" in text


def _headers_from_curl(text: str) -> str:
    # Drop shell line-continuations so multi-line cURL pastes parse cleanly.
    text = text.replace("\\\n", " ").replace("^\n", " ")
    headers: dict[str, str] = {}
    for _, value in _HEADER_RE.findall(text):
        if ":" in value:
            key, val = value.split(":", 1)
            headers[key.strip()] = val.strip()
    cookies = _COOKIE_RE.findall(text)
    if cookies and "Cookie" not in headers and "cookie" not in headers:
        headers["Cookie"] = cookies[0][1].strip()
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def setup_browser(config: Config, paths: Paths, raw_headers: str) -> None:
    """Write ``ytmusic-browser.json`` from pasted headers (Firefox or Chrome)."""
    normalized = normalize_headers(raw_headers)
    ytmusicapi.setup(filepath=str(paths.ytmusic_browser), headers_raw=normalized)
    secure_existing(paths.ytmusic_browser)


def setup_oauth(config: Config, paths: Paths, *, open_browser: bool = False) -> None:
    """Run the device-code OAuth flow and write ``ytmusic-oauth.json``."""
    ytmusicapi.setup_oauth(
        client_id=config.ytmusic.oauth_client_id,
        client_secret=config.ytmusic.oauth_client_secret,
        filepath=str(paths.ytmusic_oauth),
        open_browser=open_browser,
    )
    secure_existing(paths.ytmusic_oauth)


def build_client(config: Config, paths: Paths) -> YTMusic:
    # A session with a default per-request timeout so a dead socket can't hang
    # the worker forever (ytmusicapi sets no timeout of its own).
    session = TimeoutSession(config.sync.request_timeout)
    if config.ytmusic.auth_mode == "oauth":
        creds = OAuthCredentials(
            client_id=config.ytmusic.oauth_client_id,
            client_secret=config.ytmusic.oauth_client_secret,
        )
        return YTMusic(str(paths.ytmusic_oauth), oauth_credentials=creds, requests_session=session)
    return YTMusic(str(paths.ytmusic_browser), requests_session=session)


def has_credentials(config: Config, paths: Paths) -> bool:
    target = paths.ytmusic_oauth if config.ytmusic.auth_mode == "oauth" else paths.ytmusic_browser
    return target.exists()
