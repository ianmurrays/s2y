"""Ports: the service interfaces the engine depends on (hexagonal architecture).

The engine is written against these Protocols, never against ``spotipy`` /
``ytmusicapi`` directly. The concrete adapters in ``s2y.services`` implement them.
This keeps ``core`` free of any network dependency and makes the whole engine
testable with in-memory fakes.

``YtMusicTarget`` intentionally includes the three search methods that
:class:`s2y.core.matching.MatchSearch` needs, so the YouTube Music adapter can be
passed straight to the matcher.
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    ClassifyItem,
    ClassifyVerdict,
    RemoteAlbum,
    RemotePlaylist,
    SongInfo,
    YtCandidate,
)


class SpotifySource(Protocol):
    """Read side of Spotify (v1 reads only; write methods arrive in Phase 2)."""

    def verify(self) -> str:
        """Return the authenticated display name, or raise if auth is bad."""

    def list_playlists(self) -> list[RemotePlaylist]: ...
    def playlist_tracks(self, playlist_id: str) -> list[SongInfo]: ...
    def liked_songs(self) -> list[SongInfo]: ...
    def liked_albums(self) -> list[RemoteAlbum]: ...


class YtMusicTarget(Protocol):
    """YouTube Music: current-state reads, catalogue search, and writes."""

    def verify(self) -> str: ...

    # ---- current-state reads (for idempotent applies) ---------------------- #
    def list_playlists(self) -> list[RemotePlaylist]: ...
    def playlist_video_ids(self, playlist_id: str) -> set[str]: ...
    def liked_video_ids(self) -> set[str]: ...
    def library_album_keys(self) -> set[tuple[str, str]]: ...  # (norm artist, norm title)

    # ---- catalogue search (consumed by the matcher) ------------------------ #
    def search_songs(self, query: str, *, limit: int = 6) -> list[YtCandidate]: ...
    def search_videos(self, query: str, *, limit: int = 4) -> list[YtCandidate]: ...
    def album_candidates(
        self, artist: str, album: str, *, limit_albums: int = 3
    ) -> list[YtCandidate]: ...

    # ---- writes ------------------------------------------------------------ #
    def get_or_create_playlist(self, name: str) -> str:
        """Return the YT playlist id for ``name``, creating it (PRIVATE) if absent."""

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> dict[str, str | None]:
        """Add items (duplicates skipped). Returns ``{video_id: set_video_id|None}``."""

    def like_song(self, video_id: str) -> None: ...

    def like_album(self, artist: str, album: str) -> str | None:
        """Find the album and add it to the library. Returns its browseId, or None."""


class Classifier(Protocol):
    """An LLM judge for quarantined tracks (implemented by ``services.claude_classify``).

    Given a batch of :class:`ClassifyItem` (each a source track plus its YouTube
    Music candidates), returns one structured verdict per item, **positionally
    aligned** with the input (``len(out) == len(items)``). Batching lets the adapter
    judge a whole quarantine in a single model call. Implementations must never
    raise: any failure (binary missing, timeout, bad output) becomes an
    :attr:`ClassifyDecision.UNCERTAIN` verdict so the affected tracks simply stay
    quarantined.
    """

    def classify_batch(self, items: tuple[ClassifyItem, ...]) -> tuple[ClassifyVerdict, ...]: ...
