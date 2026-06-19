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

from ..core.matching import _similarity, normalize_artist, normalize_title
from ..core.models import RemoteAlbum, RemotePlaylist, SongInfo, YtCandidate
from .base import first_artist, is_transient
from .ratelimit import SleepFn, chunked, with_retry

log = logging.getLogger(__name__)

# Spotify API page caps.
_PLAYLIST_PAGE = 50
_ITEMS_PAGE = 100
_SAVED_PAGE = 50
_ADD_CHUNK = 100  # max uris per playlist_add_items call


class SpotifyClient:
    def __init__(self, sp: Any, *, sleep: SleepFn = time.sleep) -> None:
        self._sp = sp
        self._sleep = sleep
        # Target-role caches (reverse sync): user id, playlist name->id, and the
        # live uri set per playlist so add_playlist_items can dedup internally
        # (Spotify's API, unlike YouTube's, does NOT skip duplicate adds).
        self._user_id: str | None = None
        self._name_to_id: dict[str, str] | None = None
        self._present: dict[str, set[str]] = {}

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

    # ----- target role: current-state reads (reverse-sync dedup) ----------- #
    def present_playlist_ids(self, playlist_id: str) -> set[str]:
        first = self._call(
            lambda: self._sp.playlist_items(
                playlist_id, limit=_ITEMS_PAGE, additional_types=("track",)
            ),
            "playlist items",
        )
        uris: set[str] = set()
        for item in self._paginate(first, what="playlist items"):
            track = (item or {}).get("track") or {}
            uri = track.get("uri")
            if uri and not track.get("is_local") and track.get("type") in (None, "track"):
                uris.add(uri)
        return uris

    def present_liked_ids(self) -> set[str]:
        log.info("Spotify: reading your existing Liked Songs…")
        first = self._call(
            lambda: self._sp.current_user_saved_tracks(limit=_SAVED_PAGE), "liked songs"
        )
        uris: set[str] = set()
        for item in self._paginate(first, what="liked songs"):
            uri = ((item or {}).get("track") or {}).get("uri")
            if uri:
                uris.add(uri)
        return uris

    def present_album_keys(self) -> set[tuple[str, str]]:
        log.info("Spotify: reading your saved albums…")
        first = self._call(
            lambda: self._sp.current_user_saved_albums(limit=_SAVED_PAGE), "saved albums"
        )
        keys: set[tuple[str, str]] = set()
        for item in self._paginate(first, what="saved albums"):
            alb = (item or {}).get("album") or {}
            keys.add(
                (
                    normalize_artist(first_artist(alb.get("artists"))),
                    normalize_title(alb.get("name") or ""),
                )
            )
        return keys

    # ----- target role: catalogue search (consumed by the matcher) --------- #
    def search_songs(self, query: str, *, limit: int = 6) -> list[YtCandidate]:
        log.debug("Spotify: search tracks %r", query)
        res = (
            self._call(lambda: self._sp.search(q=query, type="track", limit=limit), "search tracks")
            or {}
        )
        items = (res.get("tracks") or {}).get("items") or []
        return [c for c in (self._candidate(t) for t in items) if c]

    def search_videos(self, query: str, *, limit: int = 4) -> list[YtCandidate]:
        # Spotify has no "videos" axis; the matcher's cascade tolerates an empty list.
        return []

    def album_candidates(
        self, artist: str, album: str, *, limit_albums: int = 3
    ) -> list[YtCandidate]:
        log.debug("Spotify: album search %r by %r", album, artist)
        res = (
            self._call(
                lambda: self._sp.search(q=f"{album} {artist}", type="album", limit=limit_albums),
                "album search",
            )
            or {}
        )
        albums = (res.get("albums") or {}).get("items") or []
        out: list[YtCandidate] = []
        for alb in albums[:limit_albums]:
            aid = alb.get("id")
            if not aid:
                continue
            a_name = alb.get("name") or album
            a_artist = first_artist(alb.get("artists")) or artist
            try:
                page = self._call(
                    lambda aid=aid: self._sp.album_tracks(aid, limit=_ITEMS_PAGE), "album tracks"
                )
            except Exception:
                continue
            for t in self._paginate(page, what="album tracks"):
                uri = (t or {}).get("uri")
                if not uri:
                    continue
                dur = t.get("duration_ms")
                out.append(
                    YtCandidate(
                        video_id=uri,
                        title=t.get("name") or "",
                        artist=first_artist(t.get("artists")) or a_artist,
                        album=a_name,
                        duration_s=None if dur is None else dur // 1000,
                        is_song=True,
                        raw=t,
                    )
                )
        return out

    # ----- target role: writes --------------------------------------------- #
    def _me(self) -> str:
        if self._user_id is None:
            me = self._call(self._sp.current_user, "current user")
            self._user_id = me.get("id") or ""
        return self._user_id

    def _names(self) -> dict[str, str]:
        if self._name_to_id is None:
            self._name_to_id = {pl.name: pl.id for pl in self.list_playlists()}
        return self._name_to_id

    def get_or_create_playlist(self, name: str) -> str:
        cache = self._names()
        if name in cache:
            return cache[name]
        created = self._call(
            lambda: self._sp.user_playlist_create(
                self._me(), name, public=False, description="Synced by s2y"
            ),
            "create playlist",
        )
        pid = created.get("id") if isinstance(created, dict) else None
        if not pid:
            raise RuntimeError(f"could not create Spotify playlist {name!r}: {created!r}")
        cache[name] = pid
        return pid

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> dict[str, str | None]:
        """Add track uris, skipping any already in the playlist.

        Spotify's ``playlist_add_items`` does NOT dedupe (unlike YouTube's
        ``duplicates=False``), so we filter against the live playlist contents — read
        once and cached, then kept current as we add — to stay idempotent on re-runs.
        """
        if not video_ids:
            return {}
        present = self._present.get(playlist_id)
        if present is None:
            present = self.present_playlist_ids(playlist_id)
            self._present[playlist_id] = present
        new = [u for u in video_ids if u not in present]
        for batch in chunked(new, _ADD_CHUNK):
            self._call(
                lambda b=batch: self._sp.playlist_add_items(playlist_id, b), "add playlist items"
            )
            present.update(batch)
        # Spotify has no per-membership handle (YouTube's setVideoId); always None.
        return {u: None for u in video_ids}

    def like_song(self, video_id: str) -> None:
        self._call(lambda: self._sp.current_user_saved_tracks_add([video_id]), "like song")

    def like_album(self, artist: str, album: str) -> str | None:
        res = (
            self._call(
                lambda: self._sp.search(q=f"{album} {artist}", type="album", limit=3),
                "album search",
            )
            or {}
        )
        albums = (res.get("albums") or {}).get("items") or []
        want = normalize_title(album)
        for alb in albums[:3]:
            aid = alb.get("id")
            if not aid:
                continue
            if _similarity(want, normalize_title(alb.get("name") or "")) < 0.6:
                continue
            self._call(lambda aid=aid: self._sp.current_user_saved_albums_add([aid]), "save album")
            return aid
        return None

    # ----- helpers ---------------------------------------------------------- #
    @staticmethod
    def _candidate(track: dict | None) -> YtCandidate | None:
        """Parse a Spotify search track into a :class:`YtCandidate` (id = track uri)."""
        if not track:
            return None
        uri = track.get("uri")
        if not uri:
            return None
        dur = track.get("duration_ms")
        return YtCandidate(
            video_id=uri,
            title=track.get("name") or "",
            artist=first_artist(track.get("artists")),
            album=(track.get("album") or {}).get("name", "") or "",
            duration_s=None if dur is None else dur // 1000,
            is_song=True,
            raw=track,
        )

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
