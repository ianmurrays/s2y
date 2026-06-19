"""The one way a quarantined track becomes a verified mapping.

Both the TUI review screen (a human pressing "accept") and the LLM classifier
(``claude`` accepting a candidate) resolve a quarantine the same way: upsert a
*verified* ``track_map`` row, mark the quarantine resolved, and invalidate the
collection signatures so change-detection re-examines the collection on the next
run. That last step is easy to forget and load-bearing — without it the resolved
track is cached but its collection is skipped, so it never actually syncs. Keeping
the sequence here means there is exactly one tested path, never a divergent copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .matching import normalize_artist, normalize_title

if TYPE_CHECKING:  # types only; no runtime import of the store layer needed here
    from ..store.repository import Repository, TrackMapRow


def resolve_quarantine_match(
    repo: Repository,
    *,
    quarantine_id: int,
    spotify_uri: str | None,
    yt_video_id: str,
    title: str,
    artist: str,
    match_method: str = "manual",
    match_confidence: float = 1.0,
    verified: bool = True,
) -> TrackMapRow:
    """Resolve one quarantined track to ``yt_video_id`` atomically.

    ``match_method`` distinguishes the source — ``"manual"`` for a human accept
    (confidence 1.0), ``"claude"`` for the LLM judge (its own confidence). The
    title/artist are canonicalized the same way the matcher caches them. Runs in a
    single transaction so a failure rolls the whole thing back.
    """
    with repo.transaction():
        track = repo.upsert_track(
            spotify_uri=spotify_uri,
            yt_video_id=yt_video_id,
            canon_title=normalize_title(title),
            canon_artist=normalize_artist(artist),
            match_method=match_method,
            match_confidence=match_confidence,
            verified=verified,
        )
        repo.resolve_quarantine(quarantine_id, track.id)
        repo.invalidate_signatures()
    return track
