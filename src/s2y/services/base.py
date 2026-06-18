"""Shared service-layer types.

Re-exports the engine's ports here too, so callers can import either from
``s2y.core.ports`` (the definition) or ``s2y.services.base`` (the adapters' home).
"""

from __future__ import annotations

import requests
from ytmusicapi.exceptions import YTMusicServerError

from ..core.ports import SpotifySource, YtMusicTarget

__all__ = [
    "ServiceError",
    "SpotifySource",
    "YtMusicTarget",
    "TimeoutSession",
    "first_artist",
    "is_transient",
]


class ServiceError(Exception):
    """A service call failed in a way the UI should surface (auth, network)."""


def is_transient(exc: Exception) -> bool:
    """True for errors worth retrying; False for deterministic failures.

    Retry rate limits (429), server (5xx) and backend errors, and network
    timeouts/connection drops. Everything else — auth (401/403), not-found,
    bad-request, and unparseable "please sign in" responses — will just fail
    again, so we fail fast instead of sleeping through the whole backoff.
    """
    status = getattr(exc, "http_status", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    # ytmusicapi flags backend faults (often 5xx) with no status attribute.
    return isinstance(exc, YTMusicServerError)


class TimeoutSession(requests.Session):
    """A ``requests.Session`` that applies a default timeout to every request.

    ``ytmusicapi`` issues requests without an explicit timeout, so a dead socket
    can hang forever. Passing this as ``requests_session`` puts a hard ceiling on
    every call (a caller-supplied ``timeout`` still wins).
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.timeout = timeout

    def request(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


def first_artist(artists: list[dict] | None) -> str:
    """Primary artist name from a Spotify/YTMusic ``artists`` list (``""`` if none)."""
    if not artists:
        return ""
    name = (artists[0] or {}).get("name")
    return name or ""
