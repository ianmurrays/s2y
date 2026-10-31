"""The one way a quarantined track becomes a verified mapping.

Both the TUI review screen (a human pressing "accept") and the LLM classifier
(``claude`` accepting a candidate) resolve a quarantine the same way: write a
*verified* identity-cache row, mark the quarantine resolved, and invalidate the
collection signatures so change-detection re-examines the collection on the next
run. That last step is easy to forget and load-bearing — without it the resolved
track is cached but its collection is skipped, so it never actually syncs. Keeping
the sequence here means there is exactly one tested path, never a divergent copy.

The direction is derived from the quarantine row's ``source_side`` so neither caller
needs to know it: a ``"spotify"`` source resolves into ``track_map`` (target =
videoId); a ``"yt"`` source resolves into ``reverse_map`` (target = Spotify uri) plus
a guarded ``track_map`` link for membership/forward reuse.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .matching import normalize_artist, normalize_title
from .models import Side

if TYPE_CHECKING:  # types only; no runtime import of the store layer needed here
    from ..store.repository import Repository, TrackMapRow

log = logging.getLogger(__name__)


def resolve_quarantine_match(
    repo: Repository,
    *,
    quarantine_id: int,
    source_side: str,
    source_uri: str | None,
    target_id: str,
    title: str,
    artist: str,
    match_method: str = "manual",
    match_confidence: float = 1.0,
    verified: bool = True,
) -> TrackMapRow:
    """Resolve one quarantined track to ``target_id`` atomically.

    ``source_uri`` is the quarantined track's id on its source service, ``target_id``
    the accepted candidate's id on the other service. ``match_method`` distinguishes
    the source — ``"manual"`` for a human accept (confidence 1.0), ``"claude"`` for the
    LLM judge (its own confidence). Runs in a single transaction so a failure rolls the
    whole thing back. Returns the ``track_map`` row the quarantine now points at.
    """
    canon_title = normalize_title(title)
    canon_artist = normalize_artist(artist)
    spotify_uri: str | None
    yt_video_id: str | None
    with repo.transaction():
        if source_side == Side.SPOTIFY:
            spotify_uri, yt_video_id = source_uri, target_id
        else:
            # Reverse: the source is a YouTube video, the accepted candidate a Spotify uri.
            spotify_uri, yt_video_id = target_id, source_uri
            repo.upsert_reverse(
                yt_video_id=source_uri or "",
                spotify_uri=target_id,
                canon_title=canon_title,
                canon_artist=canon_artist,
                match_method=match_method,
                match_confidence=match_confidence,
                verified=verified,
            )
        track = link_track_map(
            repo,
            spotify_uri=spotify_uri,
            yt_video_id=yt_video_id,
            canon_title=canon_title,
            canon_artist=canon_artist,
            match_method=match_method,
            match_confidence=match_confidence,
            verified=verified,
        )
        repo.resolve_quarantine(quarantine_id, track.id)
        repo.invalidate_signatures()
    return track


def link_track_map(
    repo: Repository,
    *,
    spotify_uri: str | None,
    yt_video_id: str | None,
    canon_title: str,
    canon_artist: str,
    duration_ms: int | None = None,
    match_method: str,
    match_confidence: float,
    verified: bool,
) -> TrackMapRow:
    """Upsert the ``track_map`` row a mapping points at, without clobbering an existing
    forward mapping that already targets a different videoId.

    The single home of the reverse no-clobber guard, shared by the engine's AUTO
    reverse-cache write and the quarantine accept path. ``upsert_track`` is
    ``ON CONFLICT(spotify_uri)``, so writing a reverse pair whose Spotify uri already
    maps to a *different* videoId would silently break the forward cache and could later
    double-add the song; in that case we skip and return the existing row (reverse
    idempotency already lives in ``reverse_map``; membership attaches by uri)."""
    if spotify_uri is not None:
        existing = repo.get_track_by_spotify_uri(spotify_uri)
        if existing is not None and existing.yt_video_id and existing.yt_video_id != yt_video_id:
            log.debug(
                "link_track_map: no-clobber — %s already maps to videoId %s, keeping it "
                "(not linking reverse videoId %s); membership attaches by uri",
                spotify_uri,
                existing.yt_video_id,
                yt_video_id,
            )
            return existing
    return repo.upsert_track(
        spotify_uri=spotify_uri,
        yt_video_id=yt_video_id,
        canon_title=canon_title,
        canon_artist=canon_artist,
        duration_ms=duration_ms,
        match_method=match_method,
        match_confidence=match_confidence,
        verified=verified,
    )
