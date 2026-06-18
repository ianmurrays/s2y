"""Planning: read both services + the local map, produce a :class:`SyncPlan`.

This performs **no writes** (not to services, not to the DB) so ``--dry-run`` is
exactly "build a plan and stop". The engine's ``apply_plan`` is what mutates.

For v1 the flow is one-way Spotify -> YouTube Music under the ``union`` policy:
every source track resolves to a YT video (cached, matched, or quarantined) and we
emit an "add" for anything not already on the YT side.
"""

from __future__ import annotations

import hashlib
import logging

from ..config import Config
from ..store.repository import Repository
from . import matching, policy
from .events import (
    CollectionSkipped,
    CollectionStarted,
    EventSink,
    PhaseFinished,
    PhaseStarted,
    TrackMatched,
    null_sink,
)
from .models import (
    CollectionKind,
    CollectionSig,
    MatchMethod,
    MatchOutcome,
    MatchResult,
    OpKind,
    RemoteAlbum,
    SongInfo,
    SyncOp,
    SyncPlan,
    YtCandidate,
)
from .ports import SpotifySource, YtMusicTarget

log = logging.getLogger(__name__)


def signature(ids: list[str]) -> str:
    """Order-independent fingerprint of a set of ids (for change detection)."""
    joined = "\n".join(sorted(i for i in ids if i))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _resolve(source: SongInfo, repo: Repository, ytmusic: YtMusicTarget) -> MatchResult | None:
    """Resolve one source track. Returns ``None`` to mean "skip" (pending review)."""
    uri = source.spotify_uri
    if uri:
        row = repo.get_track_by_spotify_uri(uri)
        if row and row.yt_video_id:
            cand = YtCandidate(
                video_id=row.yt_video_id,
                title=row.canon_title,
                artist=row.canon_artist,
                duration_s=None if row.duration_ms is None else row.duration_ms // 1000,
            )
            return MatchResult(
                source, MatchOutcome.AUTO, cand, MatchMethod.CACHED,
                row.match_confidence or 1.0,
            )
        pending = repo.get_quarantine_by_source("spotify", uri)
        if pending is not None and not pending["resolved"]:
            return None  # already awaiting human review; don't re-match every run
    return matching.match(source, ytmusic)


def _plan_track_collection(
    plan: SyncPlan,
    *,
    name: str,
    kind: CollectionKind,
    key: str,
    spotify_playlist_id: str | None,
    sig: str,
    sources: list[SongInfo],
    present: set[str],
    op_kind: OpKind,
    emit_create: bool,
    repo: Repository,
    ytmusic: YtMusicTarget,
    on_event: EventSink,
) -> None:
    desired: list[tuple[str, SongInfo]] = []
    total = len(sources)
    log.info("%s: matching %d track(s)…", name, total)
    for i, src in enumerate(sources, 1):
        # Logged BEFORE the (possibly slow/networked) resolve, so a stuck track is
        # identifiable: the log shows "matching k/N: X" and then goes quiet on X.
        log.info("matching %d/%d: %s — %s", i, total, src.title, src.artist)
        res = _resolve(src, repo, ytmusic)
        if res is None:
            plan.skipped += 1
            continue
        on_event(TrackMatched(res))
        if res.outcome is MatchOutcome.AUTO and res.video_id:
            plan.matches.append(res)
            desired.append((res.video_id, src))
        else:
            plan.quarantine.append(res)

    to_add = policy.additions([vid for vid, _ in desired], present)
    vid_source: dict[str, SongInfo] = {}
    for vid, src in desired:
        vid_source.setdefault(vid, src)

    if to_add and emit_create and kind is CollectionKind.PLAYLIST:
        plan.ops.append(SyncOp(OpKind.CREATE_PLAYLIST, name, kind, collection_key=key))
    for vid in to_add:
        plan.ops.append(
            SyncOp(op_kind, name, kind, collection_key=key,
                   source=vid_source.get(vid), video_id=vid)
        )

    plan.collections.append(
        CollectionSig(key=key, name=name, kind=kind,
                      spotify_playlist_id=spotify_playlist_id, signature=sig)
    )


def build_plan(
    *,
    spotify: SpotifySource,
    ytmusic: YtMusicTarget,
    repo: Repository,
    config: Config,
    on_event: EventSink = null_sink,
) -> SyncPlan:
    plan = SyncPlan()
    on_event(PhaseStarted("plan"))
    log.info(
        "Building plan (playlists=%s, liked_songs=%s, liked_albums=%s)",
        config.collections.sync_playlists,
        config.collections.sync_liked_songs,
        config.collections.sync_liked_albums,
    )

    if config.collections.sync_playlists:
        _plan_playlists(plan, spotify, ytmusic, repo, config, on_event)
    if config.collections.sync_liked_songs:
        _plan_liked_songs(plan, spotify, ytmusic, repo, on_event)
    if config.collections.sync_liked_albums:
        _plan_liked_albums(plan, spotify, ytmusic, repo, on_event)

    plan.stats = {
        "ops": len(plan.ops),
        "quarantine": len(plan.quarantine),
        "matched": len(plan.matches),
        "skipped": plan.skipped,
    }
    log.info(
        "Plan ready: %d actions, %d need review, %d unchanged",
        len(plan.ops), len(plan.quarantine), plan.skipped,
    )
    on_event(PhaseFinished("plan"))
    return plan


def _plan_playlists(plan, spotify, ytmusic, repo, config, on_event) -> None:
    selected = set(config.collections.selected_playlists)
    for pl in spotify.list_playlists():
        if selected and pl.id not in selected:
            continue
        row = repo.get_collection_by_spotify_id(pl.id)
        if row and pl.snapshot_id and row.spotify_snapshot_id == pl.snapshot_id:
            log.info("Playlist %r unchanged since last run — skipping", pl.name)
            on_event(CollectionSkipped(pl.name, CollectionKind.PLAYLIST))
            plan.skipped += pl.count
            continue
        tracks = spotify.playlist_tracks(pl.id)
        on_event(CollectionStarted(pl.name, CollectionKind.PLAYLIST, len(tracks)))
        present = (
            ytmusic.playlist_video_ids(row.yt_playlist_id)
            if row and row.yt_playlist_id
            else set()
        )
        _plan_track_collection(
            plan, name=pl.name, kind=CollectionKind.PLAYLIST, key=pl.id,
            spotify_playlist_id=pl.id, sig=pl.snapshot_id or "",
            sources=tracks, present=present, op_kind=OpKind.ADD_PLAYLIST_ITEM,
            emit_create=(row is None or not row.yt_playlist_id),
            repo=repo, ytmusic=ytmusic, on_event=on_event,
        )


def _plan_liked_songs(plan, spotify, ytmusic, repo, on_event) -> None:
    liked = spotify.liked_songs()
    sig = signature([s.spotify_uri or "" for s in liked])
    row = repo.get_collection_by_kind("liked_songs")
    if row and row.spotify_snapshot_id == sig:
        log.info("Liked Songs unchanged since last run — skipping")
        on_event(CollectionSkipped("Liked Songs", CollectionKind.LIKED_SONGS))
        plan.skipped += len(liked)
        return
    on_event(CollectionStarted("Liked Songs", CollectionKind.LIKED_SONGS, len(liked)))
    _plan_track_collection(
        plan, name="Liked Songs", kind=CollectionKind.LIKED_SONGS, key="liked_songs",
        spotify_playlist_id=None, sig=sig, sources=liked,
        present=ytmusic.liked_video_ids(), op_kind=OpKind.LIKE_SONG,
        emit_create=False, repo=repo, ytmusic=ytmusic, on_event=on_event,
    )


def _plan_liked_albums(plan, spotify, ytmusic, repo, on_event) -> None:
    albums: list[RemoteAlbum] = spotify.liked_albums()
    sig = signature([a.id for a in albums])
    row = repo.get_collection_by_kind("liked_albums")
    if row and row.spotify_snapshot_id == sig:
        log.info("Liked Albums unchanged since last run — skipping")
        on_event(CollectionSkipped("Liked Albums", CollectionKind.LIKED_ALBUMS))
        plan.skipped += len(albums)
        return
    on_event(CollectionStarted("Liked Albums", CollectionKind.LIKED_ALBUMS, len(albums)))
    present = ytmusic.library_album_keys()
    for alb in albums:
        akey = (matching.normalize_artist(alb.artist), matching.normalize_title(alb.name))
        if akey in present:
            plan.skipped += 1
            continue
        plan.ops.append(
            SyncOp(
                OpKind.ADD_LIBRARY_ALBUM, "Liked Albums", CollectionKind.LIKED_ALBUMS,
                collection_key="liked_albums",
                source=SongInfo(title=alb.name, artist=alb.artist),
                note=alb.id,
            )
        )
    plan.collections.append(
        CollectionSig(key="liked_albums", name="Liked Albums",
                      kind=CollectionKind.LIKED_ALBUMS, spotify_playlist_id=None, signature=sig)
    )
