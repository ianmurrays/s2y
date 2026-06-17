"""Spotify adapter over ``spotipy`` implementing :class:`SpotifySource`.

Read-only in v1. The ``spotipy.Spotify`` instance is injected (auth is built in
``s2y.auth.spotify_pkce``) so this class is easy to test with a fake client.
Pagination follows ``next`` links; every network call is wrapped in retry/backoff.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from ..core.models import RemoteAlbum, RemotePlaylist, SongInfo
from .base import first_artist
from .ratelimit import SleepFn, with_retry

# Spotify API page caps.
_PLAYLIST_PAGE = 50
_ITEMS_PAGE = 100
_SAVED_PAGE = 50


class SpotifyClient:
    def __init__(self, sp: Any, *, sleep: SleepFn = time.sleep) -> None:
        self._sp = sp
        self._sleep = sleep

    def _call(self, fn):
        return with_retry(fn, sleep=self._sleep)

    def _paginate(self, first_page: dict) -> Iterator[dict]:
        page = first_page
        while page:
            for item in page.get("items", []) or []:
                yield item
            if page.get("next"):
                current = page
                page = self._call(lambda p=current: self._sp.next(p))
            else:
                page = None

    # ----- SpotifySource ---------------------------------------------------- #
    def verify(self) -> str:
        me = self._call(self._sp.current_user)
        return me.get("display_name") or me.get("id") or "Spotify user"

    def list_playlists(self) -> list[RemotePlaylist]:
        first = self._call(lambda: self._sp.current_user_playlists(limit=_PLAYLIST_PAGE))
        out: list[RemotePlaylist] = []
        for pl in self._paginate(first):
            if not pl or not pl.get("id"):
                continue
            out.append(
                RemotePlaylist(
                    id=pl["id"],
                    name=pl.get("name") or "Untitled",
                    count=(pl.get("tracks") or {}).get("total", 0),
                    snapshot_id=pl.get("snapshot_id"),
                )
            )
        return out

    def playlist_tracks(self, playlist_id: str) -> list[SongInfo]:
        first = self._call(
            lambda: self._sp.playlist_items(
                playlist_id, limit=_ITEMS_PAGE, additional_types=("track",)
            )
        )
        return [s for s in (self._song(i) for i in self._paginate(first)) if s]

    def liked_songs(self) -> list[SongInfo]:
        first = self._call(lambda: self._sp.current_user_saved_tracks(limit=_SAVED_PAGE))
        return [s for s in (self._song(i) for i in self._paginate(first)) if s]

    def liked_albums(self) -> list[RemoteAlbum]:
        first = self._call(lambda: self._sp.current_user_saved_albums(limit=_SAVED_PAGE))
        out: list[RemoteAlbum] = []
        for item in self._paginate(first):
            alb = (item or {}).get("album") or {}
            if not alb.get("id"):
                continue
            out.append(
                RemoteAlbum(
                    id=alb["id"],
                    name=alb.get("name") or "",
                    artist=first_artist(alb.get("artists")),
                )
            )
        return out

    # ----- helpers ---------------------------------------------------------- #
    @staticmethod
    def _song(item: dict | None) -> SongInfo | None:
        track = (item or {}).get("track")
        if not track:
            return None
        # Local files and podcast episodes can't be resolved on YT — skip them.
        if track.get("is_local") or track.get("type") not in (None, "track"):
            return None
        name = track.get("name")
        if not name:
            return None
        return SongInfo(
            title=name,
            artist=first_artist(track.get("artists")),
            album=(track.get("album") or {}).get("name", "") or "",
            duration_ms=track.get("duration_ms"),
            isrc=(track.get("external_ids") or {}).get("isrc"),
            spotify_uri=track.get("uri"),
            added_at=(item or {}).get("added_at"),
        )
