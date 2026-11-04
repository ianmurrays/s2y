"""Ports: the service interfaces the engine depends on (hexagonal architecture).

The engine is written against these Protocols, never against ``spotipy`` /
``ytmusicapi`` directly. The concrete adapters in ``s2y.services`` implement them.
This keeps ``core`` free of any network dependency and makes the whole engine
testable with in-memory fakes.

Roles are by *direction*, not by service: a sync reads its **source** and writes
its **target**. In a forward (Spotify → YouTube Music) sync Spotify is the source
and YouTube Music the target; reverse swaps them. So both concrete adapters
implement **both** :class:`MusicSource` and :class:`MusicTarget`. ``MusicTarget``
includes the three search methods :class:`s2y.core.matching.MatchSearch` needs, so
the target adapter can be passed straight to the matcher.

``SpotifySource``/``YtMusicTarget`` are kept as aliases for the forward-era import
names.
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


class MusicSource(Protocol):
    """The read side of a sync: list collections and their tracks with metadata."""

    def verify(self) -> str:
        """Return the authenticated display name, or raise if auth is bad."""

    def list_playlists(self) -> list[RemotePlaylist]: ...
    def playlist_tracks(self, playlist_id: str) -> list[SongInfo]: ...
    def liked_songs(self) -> list[SongInfo]: ...
    def liked_albums(self) -> list[RemoteAlbum]: ...


class MusicTarget(Protocol):
    """The write side of a sync: current-state reads, catalogue search, and writes.

    The ``present_*`` reads return the ids/keys already on the target (used for
    idempotent applies). For YouTube those ids are ``videoId`` strings; for Spotify
    they are track ``uri`` strings — opaque to the planner either way.
    """

    def verify(self) -> str: ...

    # ---- current-state reads (for idempotent applies) ---------------------- #
    # Tracks carry title/artist/duration so the planner can also recognize a song
    # already present under a *different* release id.
    def find_playlist_id(self, name: str) -> str | None: ...  # no side effects
    def present_playlist_candidates(self, playlist_id: str) -> list[YtCandidate]: ...
    def present_liked_candidates(self) -> list[YtCandidate]: ...
    def present_album_keys(self) -> set[tuple[str, str]]: ...  # (norm artist, norm title)

    # ---- catalogue search (consumed by the matcher) ------------------------ #
    def search_songs(self, query: str, *, limit: int = 6) -> list[YtCandidate]: ...
    def search_videos(self, query: str, *, limit: int = 4) -> list[YtCandidate]: ...
    def album_candidates(
        self, artist: str, album: str, *, limit_albums: int = 3
    ) -> list[YtCandidate]: ...

    # ---- writes ------------------------------------------------------------ #
    def get_or_create_playlist(self, name: str) -> str:
        """Return the target playlist id for ``name``, creating it (private) if absent."""

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> dict[str, str | None]:
        """Add items (duplicates skipped). Returns ``{id: set_video_id|None}``.

        ``set_video_id`` is YouTube's per-membership handle (needed to remove later);
        Spotify has no equivalent and returns ``None``.
        """

    def like_song(self, video_id: str) -> None: ...

    def like_album(self, artist: str, album: str) -> str | None:
        """Find the album and add it to the library. Returns its id, or None."""


# Forward-era import names, kept as aliases so existing call sites keep working.
SpotifySource = MusicSource
YtMusicTarget = MusicTarget


class Classifier(Protocol):
    """An LLM judge for quarantined tracks (implemented by ``services.claude_classify``).

    Given a batch of :class:`ClassifyItem` (each a source track plus its candidates),
    returns one structured verdict per item, **positionally aligned** with the input
    (``len(out) == len(items)``). Batching lets the adapter judge a whole quarantine in
    a single model call. Implementations must never raise: any failure (binary missing,
    timeout, bad output) becomes an :attr:`ClassifyDecision.UNCERTAIN` verdict so the
    affected tracks simply stay quarantined.
    """

    def classify_batch(self, items: tuple[ClassifyItem, ...]) -> tuple[ClassifyVerdict, ...]: ...
