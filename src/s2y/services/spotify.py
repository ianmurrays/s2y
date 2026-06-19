"""Spotify adapter over ``spotipy`` implementing :class:`SpotifySource`.

Read-only in v1. The ``spotipy.Spotify`` instance is injected (auth is built in
``s2y.auth.spotify_pkce``) so this class is easy to test with a fake client.
Pagination follows ``next`` links; every network call is wrapped in retry/backoff.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

from ..core.models import RemoteAlbum, RemotePlaylist, SongInfo
from .base import first_artist, is_transient
from .ratelimit import SleepFn, with_retry

log = logging.getLogger(__name__)

# Spotify API page caps.
_PLAYLIST_PAGE = 50
_ITEMS_PAGE = 100
_SAVED_PAGE = 50


class SpotifyClient:
    def __init__(self, sp: Any, *, sleep: SleepFn = time.sleep) -> None:
        self._sp = sp
        self._sleep = sleep

    def _call(self, fn, what: str = "Spotify request"):
        def _on_retry(exc: Exception, attempt: int, wait: float) -> None:
            log.warning("%s throttled/failed: %s — retry %d in %.0fs", what, exc, attempt, wait)

        return with_retry(fn, sleep=self._sleep, on_retry=_on_retry, retryable=is_transient)

    def _paginate(self, first_page: dict, *, what: str = "page") -> Iterator[dict]:
        page: dict | None = first_page
        pages = 0
        while page:
            pages += 1
            log.debug("Spotify %s: page %d", what, pages)
            yield from page.get("items", []) or []
            if page.get("next"):
                current = page
                page = self._call(lambda p=current: self._sp.next(p), f"{what} next page")
            else:
                page = None

    # ----- SpotifySource ---------------------------------------------------- #
    def verify(self) -> str:
        me = self._call(self._sp.current_user, "current user")
        return me.get("display_name") or me.get("id") or "Spotify user"

    def list_playlists(self) -> list[RemotePlaylist]:
        first = self._call(
            lambda: self._sp.current_user_playlists(limit=_PLAYLIST_PAGE), "list playlists"
        )
        out: list[RemotePlaylist] = []
        for pl in self._paginate(first, what="playlists"):
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
        log.info("Spotify: %d playlists", len(out))
        return out

    def playlist_tracks(self, playlist_id: str) -> list[SongInfo]:
        first = self._call(
            lambda: self._sp.playlist_items(
                playlist_id, limit=_ITEMS_PAGE, additional_types=("track",)
            ),
            "playlist tracks",
        )
        tracks = [s for s in (self._song(i) for i in self._paginate(first, what="tracks")) if s]
        log.info("Spotify: playlist %s has %d tracks", playlist_id, len(tracks))
        return tracks

    def liked_songs(self) -> list[SongInfo]:
        log.info("Spotify: reading your Liked Songs…")
        first = self._call(
            lambda: self._sp.current_user_saved_tracks(limit=_SAVED_PAGE), "liked songs"
        )
        songs = [s for s in (self._song(i) for i in self._paginate(first, what="liked songs")) if s]
        log.info("Spotify: %d Liked Songs", len(songs))
        return songs

    def liked_albums(self) -> list[RemoteAlbum]:
        log.info("Spotify: reading your Liked Albums…")
        first = self._call(
            lambda: self._sp.current_user_saved_albums(limit=_SAVED_PAGE), "liked albums"
        )
        out: list[RemoteAlbum] = []
        for item in self._paginate(first, what="liked albums"):
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
        log.info("Spotify: %d Liked Albums", len(out))
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
