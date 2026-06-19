"""Planning: read the source service + the local map, produce a :class:`SyncPlan`.

This performs **no writes** (not to services, not to the DB) so ``--dry-run`` is
exactly "build a plan and stop". The engine's ``apply_plan`` is what mutates.

The flow is parameterized by :class:`Direction`: every *source* track resolves to a
*target* id (cached, matched, or quarantined) and we emit an "add" for anything not
already on the target side, under the ``union`` policy (adds only). Forward syncs
Spotify -> YouTube Music; reverse syncs YouTube Music -> Spotify; the machinery is
identical, only the source/target adapters and the per-direction signature column
differ.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from ..config import Config
from ..store.repository import Repository
from . import matching, policy
from .events import (
    CollectionSkipped,
    CollectionStarted,
    DedupDegraded,
    EventSink,
    PhaseFinished,
    PhaseStarted,
    TrackMatched,
    null_sink,
)
from .models import (
    FORWARD,
    CollectionKind,
    CollectionSig,
    Direction,
    MatchMethod,
    MatchOutcome,
    MatchResult,
    OpKind,
    RemoteAlbum,
    Side,
    SongInfo,
    SyncOp,
    SyncPlan,
    YtCandidate,
)
from .ports import MusicSource, MusicTarget

log = logging.getLogger(__name__)

# A never-set sentinel so callers (CLI, tests) that don't pass a stop signal
# behave exactly as before — ``is_set()`` is always False.
_NEVER_STOP = threading.Event()


def signature(ids: list[str]) -> str:
    """Order-independent fingerprint of a set of ids (for change detection)."""
    joined = "\n".join(sorted(i for i in ids if i))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _live_dedup(read, name: str, on_event: EventSink) -> set[str]:
    """Run a live target-side dedup read, degrading to an empty set (NOT an abort)
    when it fails.

    A still/again signed-out session is the most common mid-sync failure; without
    this guard its ``ServiceError`` would propagate out of ``build_plan`` and take
    down the *entire* re-run — the exact "I reset it but it starts over" symptom.
    Falling back to an empty live set is safe: dedup unions it with the persisted
    membership ledger, and re-proposed adds are no-ops (``add_playlist_items`` skips
    duplicates; ``like_song`` is idempotent). Core must not import the services
    layer's ``ServiceError``, so we catch broadly around just this one port call."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 — a broken target read must not abort the plan
        log.warning("live dedup read failed for %r: %s — using persisted state", name, exc)
        on_event(DedupDegraded(name, str(exc)))
        return set()


def _resolve(
    source: SongInfo, repo: Repository, target: MusicTarget, direction: Direction
) -> MatchResult | None:
    """Resolve one source track. Returns ``None`` to mean "skip" (pending review)."""
    ref = source.source_ref(direction.source)
    if ref:
        cached = repo.get_cached_target(direction.source.value, ref)
        if cached:
            cand = YtCandidate(
                video_id=cached.target_id,
                title=cached.canon_title,
                artist=cached.canon_artist,
                duration_s=None if cached.duration_ms is None else cached.duration_ms // 1000,
            )
            return MatchResult(
                source,
                MatchOutcome.AUTO,
                cand,
                MatchMethod.CACHED,
                cached.match_confidence or 1.0,
            )
        pending = repo.get_quarantine_by_source(direction.source.value, ref)
        if pending is not None and not pending["resolved"]:
            return None  # already awaiting human review; don't re-match every run
    return matching.match(source, target)


def _plan_track_collection(
    plan: SyncPlan,
    *,
    name: str,
    kind: CollectionKind,
    key: str,
    source_playlist_id: str | None,
    sig: str,
    sources: list[SongInfo],
    present: set[str],
    op_kind: OpKind,
    emit_create: bool,
    repo: Repository,
    target: MusicTarget,
    direction: Direction,
    on_event: EventSink,
    stop: threading.Event = _NEVER_STOP,
) -> None:
    desired: list[tuple[str, SongInfo]] = []
    total = len(sources)
    log.info("%s: matching %d track(s)…", name, total)
    for i, src in enumerate(sources, 1):
        if stop.is_set():
            # The UI asked us to wind down (quit / left the screen). build_plan does
            # no writes, so a partial plan is harmless — the worker is already
            # cancelled and the plan discarded; we just need to return fast.
            log.info("%s: stop requested — abandoning match at %d/%d", name, i, total)
            return
        # Logged BEFORE the (possibly slow/networked) resolve, so a stuck track is
        # identifiable: the log shows "matching k/N: X" and then goes quiet on X.
        log.info("matching %d/%d: %s — %s", i, total, src.title, src.artist)
        res = _resolve(src, repo, target, direction)
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
            SyncOp(
                op_kind, name, kind, collection_key=key, source=vid_source.get(vid), video_id=vid
            )
        )

    sp_id = source_playlist_id if direction.source == Side.SPOTIFY else None
    yt_id = source_playlist_id if direction.source == Side.YT else None
    plan.collections.append(
        CollectionSig(
            key=key,
            name=name,
            kind=kind,
            spotify_playlist_id=sp_id,
            yt_playlist_id=yt_id,
            signature=sig,
        )
    )


def build_plan(
    *,
    source: MusicSource,
    target: MusicTarget,
    repo: Repository,
    config: Config,
    direction: Direction = FORWARD,
    on_event: EventSink = null_sink,
    stop: threading.Event = _NEVER_STOP,
) -> SyncPlan:
    plan = SyncPlan()
    on_event(PhaseStarted("plan"))
    log.info(
        "Building plan %s->%s (playlists=%s, liked_songs=%s, liked_albums=%s)",
        direction.source.value,
        direction.target.value,
        config.collections.sync_playlists,
        config.collections.sync_liked_songs,
        config.collections.sync_liked_albums,
    )

    if config.collections.sync_playlists and not stop.is_set():
        _plan_playlists(plan, source, target, repo, config, direction, on_event, stop)
    if config.collections.sync_liked_songs and not stop.is_set():
        _plan_liked_songs(plan, source, target, repo, direction, on_event, stop)
    if config.collections.sync_liked_albums and not stop.is_set():
        _plan_liked_albums(plan, source, target, repo, direction, on_event, stop)

    plan.stats = {
        "ops": len(plan.ops),
        "quarantine": len(plan.quarantine),
        "matched": len(plan.matches),
        "skipped": plan.skipped,
    }
    log.info(
        "Plan ready: %d actions, %d need review, %d unchanged",
        len(plan.ops),
        len(plan.quarantine),
        plan.skipped,
    )
    on_event(PhaseFinished("plan"))
    return plan


def _selected_playlists(config: Config, direction: Direction) -> set[str]:
    if direction.source == Side.SPOTIFY:
        return set(config.collections.selected_playlists)
    return set(config.collections.selected_yt_playlists)


def _target_playlist_id(row, direction: Direction) -> str | None:
    """The id of the already-created *target* playlist for a collection row, if any."""
    if row is None:
        return None
    return row.yt_playlist_id if direction.target == Side.YT else row.spotify_playlist_id


def _plan_playlists(plan, source, target, repo, config, direction, on_event, stop=_NEVER_STOP):
    selected = _selected_playlists(config, direction)
    for pl in source.list_playlists():
        if stop.is_set():
            return
        if selected and pl.id not in selected:
            continue
        row = (
            repo.get_collection_by_spotify_id(pl.id)
            if direction.source == Side.SPOTIFY
            else repo.get_collection_by_yt_id(pl.id)
        )

        # Change-detection: forward uses Spotify's free snapshot_id (no track fetch
        # needed to decide skipping); reverse has no snapshot token, so it fetches the
        # tracks and fingerprints their ids. Either way the stored value lives in
        # ``direction.source_sig_col``.
        tracks: list[SongInfo] | None = None
        if direction.source == Side.SPOTIFY and pl.snapshot_id:
            sig = pl.snapshot_id
        else:
            tracks = source.playlist_tracks(pl.id)
            sig = signature([t.source_ref(direction.source) or "" for t in tracks])
        stored = getattr(row, direction.source_sig_col) if row else None
        if row and sig and stored == sig:
            log.info("Playlist %r unchanged since last run — skipping", pl.name)
            on_event(CollectionSkipped(pl.name, CollectionKind.PLAYLIST))
            plan.skipped += len(tracks) if tracks is not None else pl.count
            continue

        if tracks is None:
            tracks = source.playlist_tracks(pl.id)
        on_event(CollectionStarted(pl.name, CollectionKind.PLAYLIST, len(tracks)))
        # Dedup against what we durably recorded as landed (membership) UNIONed with a
        # live read of the target playlist. The live read is guarded so a signed-out
        # session degrades to the persisted set instead of aborting the whole plan.
        persisted = repo.present_target_ids(direction.target.value, row.id) if row else set()
        target_pl_id = _target_playlist_id(row, direction)
        live = (
            _live_dedup(
                lambda tid=target_pl_id: target.present_playlist_ids(tid),
                pl.name,
                on_event,
            )
            if target_pl_id
            else set()
        )
        _plan_track_collection(
            plan,
            name=pl.name,
            kind=CollectionKind.PLAYLIST,
            key=pl.id,
            source_playlist_id=pl.id,
            sig=sig,
            sources=tracks,
            present=persisted | live,
            op_kind=OpKind.ADD_PLAYLIST_ITEM,
            emit_create=(target_pl_id is None),
            repo=repo,
            target=target,
            direction=direction,
            on_event=on_event,
            stop=stop,
        )


def _plan_liked_songs(plan, source, target, repo, direction, on_event, stop=_NEVER_STOP):
    liked = source.liked_songs()
    sig = signature([s.source_ref(direction.source) or "" for s in liked])
    row = repo.get_collection_by_kind("liked_songs")
    if row and getattr(row, direction.source_sig_col) == sig:
        log.info("Liked Songs unchanged since last run — skipping")
        on_event(CollectionSkipped("Liked Songs", CollectionKind.LIKED_SONGS))
        plan.skipped += len(liked)
        return
    on_event(CollectionStarted("Liked Songs", CollectionKind.LIKED_SONGS, len(liked)))
    persisted = repo.present_target_ids(direction.target.value, row.id) if row else set()
    live = _live_dedup(lambda: target.present_liked_ids(), "Liked Songs", on_event)
    _plan_track_collection(
        plan,
        name="Liked Songs",
        kind=CollectionKind.LIKED_SONGS,
        key="liked_songs",
        source_playlist_id=None,
        sig=sig,
        sources=liked,
        present=persisted | live,
        op_kind=OpKind.LIKE_SONG,
        emit_create=False,
        repo=repo,
        target=target,
        direction=direction,
        on_event=on_event,
        stop=stop,
    )


def _plan_liked_albums(plan, source, target, repo, direction, on_event, stop=_NEVER_STOP):
    albums: list[RemoteAlbum] = source.liked_albums()
    sig = signature([a.id for a in albums])
    row = repo.get_collection_by_kind("liked_albums")
    if row and getattr(row, direction.source_sig_col) == sig:
        log.info("Liked Albums unchanged since last run — skipping")
        on_event(CollectionSkipped("Liked Albums", CollectionKind.LIKED_ALBUMS))
        plan.skipped += len(albums)
        return
    on_event(CollectionStarted("Liked Albums", CollectionKind.LIKED_ALBUMS, len(albums)))
    present = target.present_album_keys()
    for alb in albums:
        if stop.is_set():
            return
        akey = (matching.normalize_artist(alb.artist), matching.normalize_title(alb.name))
        if akey in present:
            plan.skipped += 1
            continue
        plan.ops.append(
            SyncOp(
                OpKind.ADD_LIBRARY_ALBUM,
                "Liked Albums",
                CollectionKind.LIKED_ALBUMS,
                collection_key="liked_albums",
                source=SongInfo(title=alb.name, artist=alb.artist),
                note=alb.id,
            )
        )
    plan.collections.append(
        CollectionSig(
            key="liked_albums",
            name="Liked Albums",
            kind=CollectionKind.LIKED_ALBUMS,
            spotify_playlist_id=None,
            signature=sig,
        )
    )
