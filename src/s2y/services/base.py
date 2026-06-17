"""Shared service-layer types.

Re-exports the engine's ports here too, so callers can import either from
``s2y.core.ports`` (the definition) or ``s2y.services.base`` (the adapters' home).
"""

from __future__ import annotations

from ..core.ports import SpotifySource, YtMusicTarget

__all__ = ["ServiceError", "SpotifySource", "YtMusicTarget", "first_artist"]


class ServiceError(Exception):
    """A service call failed in a way the UI should surface (auth, network)."""


def first_artist(artists: list[dict] | None) -> str:
    """Primary artist name from a Spotify/YTMusic ``artists`` list (``""`` if none)."""
    if not artists:
        return ""
    name = (artists[0] or {}).get("name")
    return name or ""
