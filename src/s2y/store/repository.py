"""Typed access to the s2y database.

The engine talks to persistence only through this class — never raw SQL. Methods
are small and composable; the engine groups related writes with
:meth:`Repository.transaction` so a snapshot update lands atomically with the ops
it reflects (the echo-loop kill switch from the design).

Timestamps come from an injectable ``clock`` so tests are deterministic.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Row types (persistence shapes; distinct from the pure core models)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrackMapRow:
    id: int
    isrc: str | None
    canon_title: str
    canon_artist: str
    duration_ms: int | None
    spotify_uri: str | None
    yt_video_id: str | None
    match_method: str
    match_confidence: float
    verified: bool
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(r: sqlite3.Row) -> TrackMapRow:
        return TrackMapRow(
            id=r["id"],
            isrc=r["isrc"],
            canon_title=r["canon_title"],
            canon_artist=r["canon_artist"],
            duration_ms=r["duration_ms"],
            spotify_uri=r["spotify_uri"],
            yt_video_id=r["yt_video_id"],
            match_method=r["match_method"],
            match_confidence=r["match_confidence"],
            verified=bool(r["verified"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )


@dataclass(frozen=True)
class ReverseMapRow:
    """A row of the reverse identity cache (YouTube Music -> Spotify).

    The twin of :class:`TrackMapRow`, but anchored on ``yt_video_id`` (the reverse
    source). Kept in its own table so reverse writes never clobber a forward mapping.
    """

    yt_video_id: str
    spotify_uri: str
    canon_title: str
    canon_artist: str
    match_method: str
    match_confidence: float
    verified: bool
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(r: sqlite3.Row) -> ReverseMapRow:
        return ReverseMapRow(
            yt_video_id=r["yt_video_id"],
            spotify_uri=r["spotify_uri"],
            canon_title=r["canon_title"],
            canon_artist=r["canon_artist"],
            match_method=r["match_method"],
            match_confidence=r["match_confidence"],
            verified=bool(r["verified"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )


@dataclass(frozen=True)
class CachedMatch:
    """A direction-normalized cache hit: the resolved *target* id plus the canon
    metadata the planner needs to build a cached :class:`MatchResult`.

    ``target_id`` is a YouTube ``videoId`` (forward) or a Spotify ``uri`` (reverse).
    """

    target_id: str
    canon_title: str
    canon_artist: str
    duration_ms: int | None
    match_confidence: float


@dataclass(frozen=True)
class PlaylistMapRow:
    id: int
    name: str
    kind: str
    spotify_playlist_id: str | None
    spotify_snapshot_id: str | None
    yt_playlist_id: str | None
    yt_content_fingerprint: str | None
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(r: sqlite3.Row) -> PlaylistMapRow:
        return PlaylistMapRow(
            id=r["id"],
            name=r["name"],
            kind=r["kind"],
            spotify_playlist_id=r["spotify_playlist_id"],
            spotify_snapshot_id=r["spotify_snapshot_id"],
            yt_playlist_id=r["yt_playlist_id"],
            yt_content_fingerprint=r["yt_content_fingerprint"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )


class Repository:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self.conn = conn
        self._clock = clock

    # ----- transactions ----------------------------------------------------- #
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes atomically. Rolls back on any exception.

        Safe to nest only one level (we don't use SAVEPOINTs); callers should not
        open a transaction inside another.
        """
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ----- track_map -------------------------------------------------------- #
    def get_track_by_spotify_uri(self, spotify_uri: str) -> TrackMapRow | None:
        row = self.conn.execute(
            "SELECT * FROM track_map WHERE spotify_uri = ?", (spotify_uri,)
        ).fetchone()
        return TrackMapRow.from_row(row) if row else None

    def get_track_by_video_id(self, yt_video_id: str) -> TrackMapRow | None:
        """Best row for a video id (verified first, then highest confidence)."""
        row = self.conn.execute(
            """
            SELECT * FROM track_map WHERE yt_video_id = ?
            ORDER BY verified DESC, match_confidence DESC, id ASC LIMIT 1
            """,
            (yt_video_id,),
        ).fetchone()
        return TrackMapRow.from_row(row) if row else None

    def get_track(self, track_map_id: int) -> TrackMapRow | None:
        row = self.conn.execute("SELECT * FROM track_map WHERE id = ?", (track_map_id,)).fetchone()
        return TrackMapRow.from_row(row) if row else None

    def upsert_track(
        self,
        *,
        spotify_uri: str | None,
        yt_video_id: str | None,
        canon_title: str,
        canon_artist: str,
        isrc: str | None = None,
        duration_ms: int | None = None,
        match_method: str = "meta",
        match_confidence: float = 0.0,
        verified: bool = False,
    ) -> TrackMapRow:
        """Insert or update a mapping, keyed on ``spotify_uri``.

        On conflict we refresh the YT side and match metadata but never downgrade
        a ``verified`` row back to unverified.
        """
        now = self._clock()
        row = self.conn.execute(
            """
            INSERT INTO track_map (
                isrc, canon_title, canon_artist, duration_ms,
                spotify_uri, yt_video_id, match_method, match_confidence,
                verified, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spotify_uri) DO UPDATE SET
                yt_video_id      = excluded.yt_video_id,
                isrc             = COALESCE(excluded.isrc, track_map.isrc),
                duration_ms      = COALESCE(excluded.duration_ms, track_map.duration_ms),
                match_method     = excluded.match_method,
                match_confidence = excluded.match_confidence,
                verified         = MAX(track_map.verified, excluded.verified),
                updated_at       = excluded.updated_at
            RETURNING *
            """,
            (
                isrc,
                canon_title,
                canon_artist,
                duration_ms,
                spotify_uri,
                yt_video_id,
                match_method,
                match_confidence,
                int(verified),
                now,
                now,
            ),
        ).fetchone()
        return TrackMapRow.from_row(row)

    def set_track_resolution(
        self,
        track_map_id: int,
        *,
        yt_video_id: str,
        match_method: str = "manual",
        match_confidence: float = 1.0,
        verified: bool = True,
    ) -> None:
        """Record a human (or high-confidence) resolution for an existing row."""
        self.conn.execute(
            """
            UPDATE track_map SET
                yt_video_id = ?, match_method = ?, match_confidence = ?,
                verified = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                yt_video_id,
                match_method,
                match_confidence,
                int(verified),
                self._clock(),
                track_map_id,
            ),
        )

    # ----- reverse_map (YouTube Music -> Spotify identity cache) ------------ #
    def get_reverse_by_video_id(self, yt_video_id: str) -> ReverseMapRow | None:
        row = self.conn.execute(
            "SELECT * FROM reverse_map WHERE yt_video_id = ?", (yt_video_id,)
        ).fetchone()
        return ReverseMapRow.from_row(row) if row else None

    def upsert_reverse(
        self,
        *,
        yt_video_id: str,
        spotify_uri: str,
        canon_title: str,
        canon_artist: str,
        match_method: str = "meta",
        match_confidence: float = 0.0,
        verified: bool = False,
    ) -> ReverseMapRow:
        """Insert or update a reverse mapping, keyed on ``yt_video_id``.

        On conflict we refresh the Spotify side and match metadata but never
        downgrade a ``verified`` row back to unverified (mirrors ``upsert_track``).
        """
        now = self._clock()
        row = self.conn.execute(
            """
            INSERT INTO reverse_map (
                yt_video_id, spotify_uri, canon_title, canon_artist,
                match_method, match_confidence, verified, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(yt_video_id) DO UPDATE SET
                spotify_uri      = excluded.spotify_uri,
                canon_title      = excluded.canon_title,
                canon_artist     = excluded.canon_artist,
                match_method     = excluded.match_method,
                match_confidence = excluded.match_confidence,
                verified         = MAX(reverse_map.verified, excluded.verified),
                updated_at       = excluded.updated_at
            RETURNING *
            """,
            (
                yt_video_id,
                spotify_uri,
                canon_title,
                canon_artist,
                match_method,
                match_confidence,
                int(verified),
                now,
                now,
            ),
        ).fetchone()
        return ReverseMapRow.from_row(row)

    def get_cached_target(self, source_side: str, ref: str) -> CachedMatch | None:
        """Resolve a source id to its cached *target* via the right identity cache.

        Forward (``source_side == "spotify"``) reads ``track_map`` (target =
        ``yt_video_id``); reverse (``"yt"``) reads ``reverse_map`` (target =
        ``spotify_uri``). Returns ``None`` on a miss so the planner re-matches.
        """
        if source_side == "yt":
            r = self.get_reverse_by_video_id(ref)
            if r is None:
                return None
            return CachedMatch(
                r.spotify_uri, r.canon_title, r.canon_artist, None, r.match_confidence
            )
        t = self.get_track_by_spotify_uri(ref)
        if t is None or not t.yt_video_id:
            return None
        return CachedMatch(
            t.yt_video_id, t.canon_title, t.canon_artist, t.duration_ms, t.match_confidence
        )

    # ----- playlist_map (collections) -------------------------------------- #
    def get_collection_by_spotify_id(self, spotify_playlist_id: str) -> PlaylistMapRow | None:
        row = self.conn.execute(
            "SELECT * FROM playlist_map WHERE spotify_playlist_id = ?",
            (spotify_playlist_id,),
        ).fetchone()
        return PlaylistMapRow.from_row(row) if row else None

    def get_collection_by_yt_id(self, yt_playlist_id: str) -> PlaylistMapRow | None:
        """Fetch a playlist collection anchored on its YouTube Music id (reverse sync)."""
        row = self.conn.execute(
            "SELECT * FROM playlist_map WHERE yt_playlist_id = ? LIMIT 1",
            (yt_playlist_id,),
        ).fetchone()
        return PlaylistMapRow.from_row(row) if row else None

    def get_collection_by_kind(self, kind: str) -> PlaylistMapRow | None:
        """Fetch a singleton collection (liked_songs / liked_albums)."""
        row = self.conn.execute(
            "SELECT * FROM playlist_map WHERE kind = ? LIMIT 1", (kind,)
        ).fetchone()
        return PlaylistMapRow.from_row(row) if row else None

    def get_collection(self, collection_id: int) -> PlaylistMapRow | None:
        row = self.conn.execute(
            "SELECT * FROM playlist_map WHERE id = ?", (collection_id,)
        ).fetchone()
        return PlaylistMapRow.from_row(row) if row else None

    def list_collections(self) -> list[PlaylistMapRow]:
        rows = self.conn.execute("SELECT * FROM playlist_map ORDER BY kind, name").fetchall()
        return [PlaylistMapRow.from_row(r) for r in rows]

    def get_or_create_collection(
        self,
        *,
        name: str,
        kind: str,
        spotify_playlist_id: str | None = None,
        yt_playlist_id: str | None = None,
    ) -> PlaylistMapRow:
        """Look up (or create) a collection row.

        Playlists are anchored on the *source* service's id: ``spotify_playlist_id``
        for a forward sync, ``yt_playlist_id`` for a reverse one (the unset side stays
        NULL until the target playlist is created). Liked Songs/Albums are singletons
        keyed by ``kind`` and shared across both directions.
        """
        if kind == "playlist":
            if spotify_playlist_id is not None:
                existing = self.get_collection_by_spotify_id(spotify_playlist_id)
            elif yt_playlist_id is not None:
                existing = self.get_collection_by_yt_id(yt_playlist_id)
            else:
                raise ValueError(
                    "playlist collections require a spotify_playlist_id or yt_playlist_id"
                )
        else:
            existing = self.get_collection_by_kind(kind)
        if existing is not None:
            if existing.name != name:
                self.update_collection(existing.id, name=name)
                existing = self.get_collection(existing.id)
            return existing  # type: ignore[return-value]

        now = self._clock()
        row = self.conn.execute(
            """
            INSERT INTO playlist_map (
                name, kind, spotify_playlist_id, yt_playlist_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (name, kind, spotify_playlist_id, yt_playlist_id, now, now),
        ).fetchone()
        return PlaylistMapRow.from_row(row)

    def invalidate_signatures(self) -> None:
        """Clear all collection change-detect signatures so the next run
        re-examines every collection.

        Call this after a quarantine resolution: the resolved track is now cached
        in ``track_map`` / ``reverse_map``, but an unchanged source signature would
        otherwise skip the collection and the resolution would never be applied.
        Both per-direction signature columns are cleared — a resolution carries no
        direction context, and the unaffected direction just does a cheap cached
        re-examination on its next run.
        """
        self.conn.execute(
            "UPDATE playlist_map SET spotify_snapshot_id = NULL, yt_content_fingerprint = NULL"
        )

    _UPDATABLE_COLLECTION = {
        "name",
        "spotify_playlist_id",
        "spotify_snapshot_id",
        "yt_playlist_id",
        "yt_content_fingerprint",
    }

    def update_collection(self, collection_id: int, **fields) -> None:
        if not fields:
            return
        bad = set(fields) - self._UPDATABLE_COLLECTION
        if bad:
            raise ValueError(f"cannot update collection field(s): {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE playlist_map SET {cols}, updated_at = ? WHERE id = ?",
            (*fields.values(), self._clock(), collection_id),
        )

    # ----- playlist_membership --------------------------------------------- #
    def upsert_membership(
        self,
        playlist_map_id: int,
        track_map_id: int,
        *,
        present_spotify: bool | None = None,
        present_yt: bool | None = None,
        yt_set_video_id: str | None = None,
        spotify_added_at: str | None = None,
        yt_added_at: str | None = None,
    ) -> None:
        """Insert or merge a membership row. ``None`` flags leave existing values."""
        existing = self.conn.execute(
            "SELECT * FROM playlist_membership WHERE playlist_map_id = ? AND track_map_id = ?",
            (playlist_map_id, track_map_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO playlist_membership (
                    playlist_map_id, track_map_id, present_spotify, present_yt,
                    yt_set_video_id, spotify_added_at, yt_added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist_map_id,
                    track_map_id,
                    int(bool(present_spotify)),
                    int(bool(present_yt)),
                    yt_set_video_id,
                    spotify_added_at,
                    yt_added_at,
                ),
            )
            return
        self.conn.execute(
            """
            UPDATE playlist_membership SET
                present_spotify  = COALESCE(?, present_spotify),
                present_yt       = COALESCE(?, present_yt),
                yt_set_video_id  = COALESCE(?, yt_set_video_id),
                spotify_added_at = COALESCE(?, spotify_added_at),
                yt_added_at      = COALESCE(?, yt_added_at)
            WHERE playlist_map_id = ? AND track_map_id = ?
            """,
            (
                None if present_spotify is None else int(present_spotify),
                None if present_yt is None else int(present_yt),
                yt_set_video_id,
                spotify_added_at,
                yt_added_at,
                playlist_map_id,
                track_map_id,
            ),
        )

    def get_membership(self, playlist_map_id: int, track_map_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM playlist_membership WHERE playlist_map_id = ? AND track_map_id = ?",
            (playlist_map_id, track_map_id),
        ).fetchone()

    def yt_present_video_ids(self, playlist_map_id: int) -> set[str]:
        """Video ids already marked present on YT for a collection (apply-time dedup)."""
        rows = self.conn.execute(
            """
            SELECT t.yt_video_id AS vid
            FROM playlist_membership m JOIN track_map t ON t.id = m.track_map_id
            WHERE m.playlist_map_id = ? AND m.present_yt = 1 AND t.yt_video_id IS NOT NULL
            """,
            (playlist_map_id,),
        ).fetchall()
        return {r["vid"] for r in rows}

    def spotify_present_uris(self, playlist_map_id: int) -> set[str]:
        """Spotify uris already marked present on Spotify for a collection (reverse dedup)."""
        rows = self.conn.execute(
            """
            SELECT t.spotify_uri AS uri
            FROM playlist_membership m JOIN track_map t ON t.id = m.track_map_id
            WHERE m.playlist_map_id = ? AND m.present_spotify = 1 AND t.spotify_uri IS NOT NULL
            """,
            (playlist_map_id,),
        ).fetchall()
        return {r["uri"] for r in rows}

    def present_target_ids(self, target_side: str, playlist_map_id: int) -> set[str]:
        """Persisted ids already landed on the *target* service for a collection.

        Forward target = YouTube (``yt_video_id``); reverse target = Spotify
        (``spotify_uri``). The planner unions this durable ledger with a live read so
        an already-synced item is never re-proposed even if the live read degrades.
        """
        if target_side == "spotify":
            return self.spotify_present_uris(playlist_map_id)
        return self.yt_present_video_ids(playlist_map_id)

    # ----- snapshots -------------------------------------------------------- #
    def save_snapshot(self, side: str, playlist_map_id: int, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO snapshot (side, playlist_map_id, taken_at, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (side, playlist_map_id, self._clock(), json.dumps(payload)),
        )

    def get_latest_snapshot(self, side: str, playlist_map_id: int) -> dict | None:
        row = self.conn.execute(
            """
            SELECT payload_json FROM snapshot
            WHERE side = ? AND playlist_map_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (side, playlist_map_id),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    # ----- quarantine ------------------------------------------------------- #
    def add_quarantine(
        self,
        *,
        source_side: str,
        source_uri: str | None,
        title: str,
        artist: str,
        album: str | None,
        reason: str,
        best_candidate: dict | None = None,
    ) -> int:
        """Add (or bump the attempt count of) a quarantined track."""
        now = self._clock()
        cur = self.conn.execute(
            """
            INSERT INTO quarantine (
                source_side, source_uri, title, artist, album, reason,
                best_candidate_json, attempts, resolved, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(source_side, source_uri) DO UPDATE SET
                reason = excluded.reason,
                best_candidate_json = excluded.best_candidate_json,
                attempts = quarantine.attempts + 1,
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (
                source_side,
                source_uri,
                title,
                artist,
                album,
                reason,
                json.dumps(best_candidate) if best_candidate is not None else None,
                now,
                now,
            ),
        )
        return cur.fetchone()["id"]

    def list_unresolved_quarantine(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM quarantine WHERE resolved = 0 ORDER BY created_at",
        ).fetchall()

    def get_quarantine_by_source(self, source_side: str, source_uri: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM quarantine WHERE source_side = ? AND source_uri = ?",
            (source_side, source_uri),
        ).fetchone()

    def skip_quarantine(self, quarantine_id: int) -> None:
        """Mark a quarantined item resolved with no mapping (user chose to skip)."""
        self.conn.execute(
            "UPDATE quarantine SET resolved = 1, updated_at = ? WHERE id = ?",
            (self._clock(), quarantine_id),
        )

    def resolve_quarantine(self, quarantine_id: int, resolution_track_map_id: int) -> None:
        self.conn.execute(
            """
            UPDATE quarantine SET resolved = 1, resolution_track_map_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (resolution_track_map_id, self._clock(), quarantine_id),
        )

    def count_unresolved_quarantine(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM quarantine WHERE resolved = 0"
        ).fetchone()["n"]

    # ----- sync_run --------------------------------------------------------- #
    def start_run(self, mode: str, dry_run: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_run (started_at, mode, dry_run, status) "
            "VALUES (?, ?, ?, 'running') RETURNING id",
            (self._clock(), mode, int(dry_run)),
        )
        return cur.fetchone()["id"]

    def finish_run(self, run_id: int, status: str, stats: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE sync_run SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
            (self._clock(), status, json.dumps(stats or {}), run_id),
        )

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sync_run ORDER BY id DESC LIMIT 1").fetchone()
