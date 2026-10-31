"""YouTube Music adapter over ``ytmusicapi`` implementing :class:`YtMusicTarget`.

The ``YTMusic`` instance is injected (auth is built in ``s2y.auth.ytmusic_auth``).
This adapter is the only place that knows ``videoId``/``setVideoId``/``browseId``
shapes. Search results are normalized to :class:`YtCandidate` for the matcher.

Notes on the unofficial API:
* ``add_playlist_items`` returns ``setVideoId`` values for added items in
  ``playlistEditResults`` — captured here, needed for *removal* later (Phase 3).
* Likes are a rating state: ``rate_song(videoId, LIKE)``. Liked Music has no
  ``setVideoId`` (so un-likes use ``rate_song(INDIFFERENT)`` in Phase 3).
* Saving an album = ``rate_playlist(audioPlaylistId, LIKE)`` on the album.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ytmusicapi import LikeStatus

from ..core.matching import _similarity, normalize_artist, normalize_title
from ..core.models import RemoteAlbum, RemotePlaylist, SongInfo, YtCandidate
from .base import ServiceError, first_artist, is_transient
from .ratelimit import SleepFn, with_retry

log = logging.getLogger(__name__)

# A big page size to fetch a whole collection in one call.
_BIG = 5000
# The synthetic "Liked Music" playlist isn't a real user playlist.
_LIKED_MUSIC_IDS = {"LM", "LM2"}

# Markers that mean YouTube Music handed us a logged-out "please sign in" page
# (ytmusicapi then fails to parse it). Browser-header sessions expire, so this is
# the most common real failure — surface it as actionable instead of a parse dump.
_SIGNED_OUT_MARKERS = ("signinendpoint", "sign in to listen", "looking for what you")
_SIGNED_OUT_MSG = (
    "YouTube Music is signed out — it returned a 'Sign in' page instead of your "
    "library. Reconnect it: re-run setup (or Settings) and paste fresh request "
    "headers from a logged-in music.youtube.com tab. Browser-header sessions "
    "expire after a while; OAuth is longer-lived if you prefer."
)


def _looks_signed_out(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(marker in s for marker in _SIGNED_OUT_MARKERS)


class YTMusicClient:
    def __init__(self, yt: Any, *, sleep: SleepFn = time.sleep) -> None:
        self._yt = yt
        self._sleep = sleep
        self._name_to_id: dict[str, str] | None = None  # playlist-name cache

    def _call(self, fn, what: str = "YouTube Music request"):
        def _on_retry(exc: Exception, attempt: int, wait: float) -> None:
            log.warning("%s throttled/failed: %s — retry %d in %.0fs", what, exc, attempt, wait)

        try:
            return with_retry(fn, sleep=self._sleep, on_retry=_on_retry, retryable=is_transient)
        except ServiceError:
            raise
        except Exception as exc:
            if _looks_signed_out(exc):
                log.error("YouTube Music auth failure on %s: signed out", what)
                raise ServiceError(_SIGNED_OUT_MSG) from exc
            raise

    # ----- verify ----------------------------------------------------------- #
    def verify(self) -> str:
        self._call(lambda: self._yt.get_library_playlists(limit=1))
        return "YouTube Music"

    # ----- current-state reads --------------------------------------------- #
    def list_playlists(self) -> list[RemotePlaylist]:
        raw = self._call(lambda: self._yt.get_library_playlists(limit=_BIG)) or []
        out: list[RemotePlaylist] = []
        for pl in raw:
            pid = pl.get("playlistId")
            if not pid or pid in _LIKED_MUSIC_IDS:
                continue
            out.append(
                RemotePlaylist(
                    id=pid,
                    name=pl.get("title") or "Untitled",
                    count=_as_int(pl.get("count")),
                )
            )
        return out

    def present_playlist_candidates(self, playlist_id: str) -> list[YtCandidate]:
        """Present playlist tracks with metadata (for content-based dedup)."""
        log.info("YouTube Music: reading existing items of playlist %s…", playlist_id)
        data = (
            self._call(lambda: self._yt.get_playlist(playlist_id, limit=_BIG), "get playlist") or {}
        )
        return [c for c in (_candidate(t, is_song=True) for t in data.get("tracks") or []) if c]

    def present_liked_candidates(self) -> list[YtCandidate]:
        """Present Liked Music with metadata (for content-based dedup)."""
        # This is the (often slow) "what do I already like on YT" fetch that runs
        # right after the Spotify count is known — log around it so it doesn't look
        # like a hang.
        log.info("YouTube Music: reading your existing likes…")
        data = self._call(lambda: self._yt.get_liked_songs(limit=_BIG), "get liked songs") or {}
        return [c for c in (_candidate(t, is_song=True) for t in data.get("tracks") or []) if c]

    def present_album_keys(self) -> set[tuple[str, str]]:
        log.info("YouTube Music: reading your library albums…")
        raw = self._call(lambda: self._yt.get_library_albums(limit=_BIG), "library albums") or []
        keys: set[tuple[str, str]] = set()
        for a in raw:
            title = a.get("title") or ""
            artist = first_artist(a.get("artists"))
            keys.add((normalize_artist(artist), normalize_title(title)))
        return keys

    # ----- rich reads (source role: YouTube Music -> Spotify) --------------- #
    def playlist_tracks(self, playlist_id: str) -> list[SongInfo]:
        data = (
            self._call(lambda: self._yt.get_playlist(playlist_id, limit=_BIG), "get playlist") or {}
        )
        tracks = [s for s in (_song(t) for t in data.get("tracks", []) or []) if s]
        log.info("YouTube Music: playlist %s has %d tracks", playlist_id, len(tracks))
        return tracks

    def liked_songs(self) -> list[SongInfo]:
        log.info("YouTube Music: reading your liked songs…")
        data = self._call(lambda: self._yt.get_liked_songs(limit=_BIG), "get liked songs") or {}
        songs = [s for s in (_song(t) for t in data.get("tracks", []) or []) if s]
        log.info("YouTube Music: %d liked songs", len(songs))
        return songs

    def liked_albums(self) -> list[RemoteAlbum]:
        log.info("YouTube Music: reading your library albums…")
        raw = self._call(lambda: self._yt.get_library_albums(limit=_BIG), "library albums") or []
        out: list[RemoteAlbum] = []
        for a in raw:
            aid = a.get("browseId") or a.get("playlistId")
            if not aid:
                continue
            out.append(
                RemoteAlbum(
                    id=aid,
                    name=a.get("title") or "",
                    artist=first_artist(a.get("artists")),
                )
            )
        log.info("YouTube Music: %d library albums", len(out))
        return out

    # ----- catalogue search ------------------------------------------------- #
    def search_songs(self, query: str, *, limit: int = 6) -> list[YtCandidate]:
        log.debug("YouTube Music: search songs %r", query)
        raw = (
            self._call(
                lambda: self._yt.search(query, filter="songs", limit=limit),
                f"search songs {query!r}",
            )
            or []
        )
        return [c for c in (_candidate(r, is_song=True) for r in raw) if c]

    def search_videos(self, query: str, *, limit: int = 4) -> list[YtCandidate]:
        log.debug("YouTube Music: search videos %r", query)
        raw = (
            self._call(
                lambda: self._yt.search(query, filter="videos", limit=limit),
                f"search videos {query!r}",
            )
            or []
        )
        return [c for c in (_candidate(r, is_song=False) for r in raw) if c]

    def album_candidates(
        self, artist: str, album: str, *, limit_albums: int = 3
    ) -> list[YtCandidate]:
        log.debug("YouTube Music: album search %r by %r", album, artist)
        albums = (
            self._call(
                lambda: self._yt.search(f"{album} {artist}", filter="albums", limit=limit_albums),
                f"album search {album!r}",
            )
            or []
        )
        out: list[YtCandidate] = []
        for a in albums[:limit_albums]:
            browse_id = a.get("browseId")
            if not browse_id:
                continue
            try:
                full = self._call(lambda bid=browse_id: self._yt.get_album(bid))
            except Exception:
                continue
            a_title = full.get("title") or album
            a_artist = first_artist(full.get("artists")) or artist
            for t in full.get("tracks", []) or []:
                vid = t.get("videoId")
                if not vid:
                    continue
                out.append(
                    YtCandidate(
                        video_id=vid,
                        title=t.get("title") or "",
                        artist=first_artist(t.get("artists")) or a_artist,
                        album=a_title,
                        duration_s=t.get("duration_seconds"),
                        is_song=True,
                        raw=t,
                    )
                )
        return out

    # ----- writes ----------------------------------------------------------- #
    def _names(self) -> dict[str, str]:
        if self._name_to_id is None:
            self._name_to_id = {pl.name: pl.id for pl in self.list_playlists()}
        return self._name_to_id

    def find_playlist_id(self, name: str) -> str | None:
        return self._names().get(name)

    def get_or_create_playlist(self, name: str) -> str:
        cache = self._names()
        if name in cache:
            return cache[name]
        created = self._call(
            lambda: self._yt.create_playlist(
                name,
                "Synced by s2y",
                privacy_status="PRIVATE",
            )
        )
        pid = created if isinstance(created, str) else (created or {}).get("id", "")
        if not pid:
            raise RuntimeError(f"could not create YT playlist {name!r}: {created!r}")
        cache[name] = pid
        return pid

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> dict[str, str | None]:
        if not video_ids:
            return {}
        resp = self._call(
            lambda: self._yt.add_playlist_items(playlist_id, videoIds=video_ids, duplicates=False)
        )
        return _set_video_ids(resp, video_ids)

    def like_song(self, video_id: str) -> None:
        self._call(lambda: self._yt.rate_song(video_id, LikeStatus.LIKE))

    def like_album(self, artist: str, album: str) -> str | None:
        albums = (
            self._call(lambda: self._yt.search(f"{album} {artist}", filter="albums", limit=3)) or []
        )
        want = normalize_title(album)
        for a in albums[:3]:
            browse_id = a.get("browseId")
            if not browse_id:
                continue
            try:
                full = self._call(lambda bid=browse_id: self._yt.get_album(bid))
            except Exception:
                continue
            if _similarity(want, normalize_title(full.get("title") or "")) < 0.6:
                continue
            audio_pl = full.get("audioPlaylistId")
            if audio_pl:
                self._call(lambda apid=audio_pl: self._yt.rate_playlist(apid, LikeStatus.LIKE))
                return browse_id
        return None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _song(track: dict | None) -> SongInfo | None:
    """Parse a ytmusicapi playlist/liked track dict into a :class:`SongInfo`.

    Skips entries with no ``videoId`` or title (unavailable/region-locked tracks).
    The video id is the song's *source* identifier for a reverse sync.
    """
    if not track:
        return None
    vid = track.get("videoId")
    title = track.get("title")
    if not vid or not title:
        return None
    album = track.get("album")
    album_name = album.get("name", "") if isinstance(album, dict) else ""
    dur = track.get("duration_seconds")
    return SongInfo(
        title=title,
        artist=first_artist(track.get("artists")),
        album=album_name or "",
        duration_ms=None if dur is None else int(dur) * 1000,
        yt_video_id=vid,
    )


def _candidate(result: dict, *, is_song: bool) -> YtCandidate | None:
    vid = result.get("videoId")
    if not vid:
        return None
    album = result.get("album") or {}
    return YtCandidate(
        video_id=vid,
        title=result.get("title") or "",
        artist=first_artist(result.get("artists")),
        album=album.get("name", "") if isinstance(album, dict) else "",
        duration_s=result.get("duration_seconds"),
        is_song=is_song,
        raw=result,
    )


def _set_video_ids(resp: Any, video_ids: list[str]) -> dict[str, str | None]:
    """Map ``video_id -> set_video_id`` from an add_playlist_items response."""
    mapping: dict[str, str | None] = {vid: None for vid in video_ids}
    if isinstance(resp, dict):
        for r in resp.get("playlistEditResults", []) or []:
            # ytmusicapi maps each result through .get("playlistEditVideoAddedResultData"),
            # which yields None when that block is absent — skip those.
            if not isinstance(r, dict):
                continue
            vid = r.get("videoId")
            if vid:
                mapping[vid] = r.get("setVideoId")
    return mapping


def _as_int(value: Any) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0
