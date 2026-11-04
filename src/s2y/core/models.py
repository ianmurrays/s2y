"""Core data types shared across the engine, services, store, and UI.

These are pure value objects — no behaviour that touches the network, the
filesystem, or Textual. The matching/normalization logic lives in
``s2y.core.matching``; the diff/plan logic in ``s2y.core.planner``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CollectionKind(StrEnum):
    """The logical kind of a thing we sync.

    A playlist is a named ordered set of tracks. Liked Songs and Liked Albums are
    *rating states* on each service rather than playlists, but we model them as
    pseudo-collections so the diff/plan machinery is uniform.
    """

    PLAYLIST = "playlist"
    LIKED_SONGS = "liked_songs"
    LIKED_ALBUMS = "liked_albums"


class Side(StrEnum):
    """A music service, as the stable string used across the store and engine."""

    SPOTIFY = "spotify"
    YT = "yt"


@dataclass(frozen=True)
class Direction:
    """Which way a sync flows, plus the few service-specific bindings the engine
    needs to stay honest about it.

    ``source`` is read; ``target`` is written. ``source_sig_col`` names the
    ``playlist_map`` column that holds this direction's change-detect signature
    (forward uses Spotify's ``snapshot_id``; reverse uses a content fingerprint of
    the YouTube id set, since YT has no snapshot token).
    """

    source: Side
    target: Side
    source_sig_col: str


FORWARD = Direction(Side.SPOTIFY, Side.YT, "spotify_snapshot_id")
REVERSE = Direction(Side.YT, Side.SPOTIFY, "yt_content_fingerprint")


class MatchMethod(StrEnum):
    """How a Spotify track was resolved to a YouTube Music ``videoId``."""

    CACHED = "cached"  # already in track_map; never re-matched
    ISRC = "isrc"
    META = "meta"  # normalized title+artist, duration-gated
    FUZZY = "fuzzy"
    MANUAL = "manual"  # a human resolved it in the review screen
    PRESENT = "present"  # already in the target collection by content; never cached


class MatchOutcome(StrEnum):
    """The disposition of a single match attempt."""

    AUTO = "auto"  # confidence cleared the auto-accept threshold
    QUARANTINE = "quarantine"  # ambiguous / low confidence -> human review
    NO_MATCH = "no_match"  # nothing plausible found


@dataclass(frozen=True)
class SongInfo:
    """A track as described by the *source* service (Spotify in v1).

    Mirrors the reference project's ``SongInfo`` namedtuple but carries the extra
    signal we use for accurate matching: duration and ISRC.
    """

    title: str
    artist: str
    album: str = ""
    duration_ms: int | None = None
    isrc: str | None = None
    spotify_uri: str | None = None
    # Set when this song's *source* is YouTube Music (reverse sync). A song always
    # carries its real id in its own service's field — never a foreign id in
    # ``spotify_uri`` — so the store boundary stays honest. See :class:`Direction`.
    yt_video_id: str | None = None
    added_at: str | None = None  # ISO-8601, as the source service reports it

    @property
    def duration_s(self) -> float | None:
        return None if self.duration_ms is None else self.duration_ms / 1000.0

    def source_ref(self, side: Side) -> str | None:
        """This song's id on ``side`` — its identifier when ``side`` is the source."""
        return self.spotify_uri if side == Side.SPOTIFY else self.yt_video_id


@dataclass(frozen=True)
class YtCandidate:
    """A YouTube Music search hit, normalized to the fields we compare on.

    ``raw`` keeps the original ytmusicapi dict for debugging/extra fields and is
    excluded from equality so candidates dedupe by ``video_id`` cleanly.

    ``video_id`` is an opaque match key to the pure matcher. In a reverse sync it
    actually carries a Spotify ``uri`` (the candidate is a Spotify track); the engine
    maps it to the right store column by :class:`Direction`, never writing it blindly.
    """

    video_id: str
    title: str
    artist: str
    album: str = ""
    duration_s: int | None = None
    is_song: bool = True  # True = "songs" filter result; False = "videos"
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class MatchResult:
    """Outcome of resolving one :class:`SongInfo` to YouTube Music."""

    source: SongInfo
    outcome: MatchOutcome
    best: YtCandidate | None
    method: MatchMethod
    confidence: float
    candidates: tuple[YtCandidate, ...] = ()  # alternatives for the review screen
    reason: str = ""

    @property
    def video_id(self) -> str | None:
        return self.best.video_id if self.best else None


class ClassifyDecision(StrEnum):
    """An LLM judge's verdict on a quarantined track (see :mod:`s2y.core.classify`)."""

    ACCEPT = "accept"  # one of the candidates IS this track
    REJECT = "reject"  # none of the candidates match
    UNCERTAIN = "uncertain"  # can't decide / parse fail / subprocess fail (safe default)


@dataclass(frozen=True)
class ClassifyVerdict:
    """The structured result of classifying one quarantined track.

    ``video_id`` is set only for :attr:`ClassifyDecision.ACCEPT` and is always one
    of the candidate ids that were shown to the model (validated by the parser, so
    a hallucinated id can never resolve a quarantine). ``reasoning`` is for logs/UI
    only and never drives control flow.
    """

    decision: ClassifyDecision
    video_id: str | None = None
    confidence: float = 0.0  # [0, 1], clamped by the parser
    reasoning: str = ""


@dataclass(frozen=True)
class ClassifyItem:
    """One unit of work for the batched LLM judge: a source track paired with the
    YouTube Music candidates it should be matched against.

    The :class:`s2y.core.ports.Classifier` takes a tuple of these and returns a
    positionally-aligned tuple of :class:`ClassifyVerdict` (one per item).
    """

    source: SongInfo
    candidates: tuple[YtCandidate, ...]


@dataclass(frozen=True)
class RemotePlaylist:
    """A playlist as listed by a service (Spotify or YouTube Music)."""

    id: str
    name: str
    count: int = 0
    snapshot_id: str | None = None  # Spotify's free change token; None on YT


@dataclass(frozen=True)
class RemoteAlbum:
    """A saved/liked album on the source service."""

    id: str
    name: str
    artist: str
    # Set only for a Spotify single: the planner likes these songs in place of saving
    # the release, which YouTube Music often doesn't have on its own.
    tracks: tuple[SongInfo, ...] = ()


class OpKind(StrEnum):
    """A single idempotent write against the destination service."""

    CREATE_PLAYLIST = "create_playlist"
    ADD_PLAYLIST_ITEM = "add_playlist_item"
    LIKE_SONG = "like_song"
    ADD_LIBRARY_ALBUM = "add_library_album"


@dataclass
class SyncOp:
    """One planned write. The planner emits these; the engine applies them.

    Ops are deliberately fine-grained and idempotent so an interrupted apply can
    resume safely and a re-run is a no-op.
    """

    kind: OpKind
    collection: str  # logical collection name (e.g. "Chill", "Liked Songs")
    collection_kind: CollectionKind
    collection_key: str = ""  # stable key for grouping (source playlist id / "liked_songs")
    source: SongInfo | None = None
    # The *target* id for ADD_PLAYLIST_ITEM / LIKE_SONG: a YouTube videoId (forward)
    # or a Spotify uri (reverse). Named ``video_id`` for forward-history reasons; the
    # engine routes it to the right target call by :class:`Direction`.
    video_id: str | None = None
    album_browse_id: str | None = None  # ADD_LIBRARY_ALBUM
    note: str = ""

    def describe(self) -> str:
        who = self.source.title if self.source else self.collection
        return f"{self.kind.value}: {who} -> {self.collection}"


@dataclass(frozen=True)
class CollectionSig:
    """A collection's source-side signature, persisted after a successful apply.

    Used for cheap change-detection: on the next run, an unchanged signature lets
    us skip the whole collection. For playlists this is Spotify's ``snapshot_id``;
    for Liked Songs/Albums it's a hash of the source id set.
    """

    key: str
    name: str
    kind: CollectionKind
    # The source playlist's id (forward: a Spotify id; reverse: a YouTube id). The
    # engine persists it into the matching ``playlist_map`` column by :class:`Direction`.
    spotify_playlist_id: str | None
    yt_playlist_id: str | None = None
    signature: str = ""


@dataclass
class SyncPlan:
    """The full result of planning: what to do, what needs review, what to skip.

    Producing a :class:`SyncPlan` performs **no writes** (to services or the DB).
    ``--dry-run`` simply prints it and stops; ``apply_plan`` is what persists.
    """

    ops: list[SyncOp] = field(default_factory=list)
    quarantine: list[MatchResult] = field(default_factory=list)
    matches: list[MatchResult] = field(default_factory=list)  # AUTO/CACHED, to cache
    present: int = 0  # tracks already on the target (exact id or same recording): no op
    degraded: list[str] = field(default_factory=list)  # skipped: target could not be read
    collections: list[CollectionSig] = field(default_factory=list)
    skipped: int = 0  # already-synced items that produced no op
    stats: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.ops and not self.quarantine
