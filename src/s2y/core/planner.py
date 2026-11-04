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
from . import matching
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


def _live_present(read, name: str, plan: SyncPlan, on_event: EventSink):
    """Run a live target-side read, returning ``None`` (NOT aborting) when it fails.

    A still/again signed-out session is the most common mid-sync failure; without
    this guard its ``ServiceError`` would propagate out of ``build_plan`` and take
    down the *entire* re-run — the exact "I reset it but it starts over" symptom.
    Callers skip the collection on ``None``: exact-id dedup in ``add_playlist_items``
    can't see a song present under another release id, so planning adds blind could
    duplicate it. No signature is stored, so the next run retries. Core must not import
    the services layer's ``ServiceError``, so we catch broadly around just this read."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 — a broken target read must not abort the plan
        log.warning("live present read failed for %r: %s — skipping this run", name, exc)
        plan.degraded.append(name)
        on_event(DedupDegraded(name, str(exc)))
        return None


def _cached(source: SongInfo, repo: Repository, direction: Direction) -> MatchResult | None:
    """The identity-cache hit for one source track, if any."""
    ref = source.source_ref(direction.source)
    cached = repo.get_cached_target(direction.source.value, ref) if ref else None
    if cached is None:
        return None
    log.debug(
        "resolve %r — %r: cache HIT (%s) -> %s conf=%.2f",
        source.title,
        source.artist,
        direction.source.value,
        cached.target_id,
        cached.match_confidence or 1.0,
    )
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


def _pending_review(source: SongInfo, repo: Repository, direction: Direction) -> bool:
    """Already awaiting human review, so don't re-match it every run."""
    ref = source.source_ref(direction.source)
    pending = repo.get_quarantine_by_source(direction.source.value, ref) if ref else None
    if pending is None or pending["resolved"]:
        return False
    log.debug("resolve %r — %r: pending quarantine — skipping", source.title, source.artist)
    return True


def _search(source: SongInfo, target: MusicTarget) -> MatchResult:
    """Resolve one source track through the matcher's (networked) search cascade."""
    res = matching.match(source, target)
    if log.isEnabledFor(logging.DEBUG):
        if res.outcome is MatchOutcome.AUTO and res.video_id:
            log.debug(
                "resolve %r — %r: fresh match -> %s conf=%.2f via %s",
                source.title,
                source.artist,
                res.video_id,
                res.confidence,
                res.method.value,
            )
        else:
            log.debug(
                "resolve %r — %r: %s conf=%.2f — quarantined",
                source.title,
                source.artist,
                res.outcome.value,
                res.confidence,
            )
    return res


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
    present_index: matching.PresentIndex,
    op_kind: OpKind,
    emit_create: bool,
    repo: Repository,
    target: MusicTarget,
    direction: Direction,
    on_event: EventSink,
    stop: threading.Event = _NEVER_STOP,
) -> None:
    to_add: list[tuple[str, SongInfo]] = []
    seen: set[str] = set()
    exact = content = 0
    debug = log.isEnabledFor(logging.DEBUG)
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
        res = _cached(src, repo, direction)
        if res is None and _pending_review(src, repo, direction):
            plan.skipped += 1
            continue
        # A track already in the target under another release id needs no search and no
        # add. Nothing is persisted: the identity cache is global but this fact is per
        # collection, and re-probing the live read next run is offline and cheap.
        if res is None or res.video_id not in present:
            hit = present_index.find(src)
            if hit is not None:
                found, similarity = hit
                content += 1
                on_event(
                    TrackMatched(
                        MatchResult(src, MatchOutcome.AUTO, found, MatchMethod.PRESENT, similarity),
                        present=True,
                    )
                )
                if debug:
                    log.debug(
                        "  %s — %s -> %s [present-by-content]",
                        src.title,
                        src.artist,
                        found.video_id,
                    )
                continue
        if res is None:
            res = _search(src, target)
        vid = res.video_id
        if res.outcome is not MatchOutcome.AUTO or not vid:
            on_event(TrackMatched(res))
            plan.quarantine.append(res)
            continue
        on_event(TrackMatched(res, present=vid in present))
        plan.matches.append(res)
        if vid in present:
            exact += 1
            if debug:
                log.debug("  %s — %s -> %s [present]", src.title, src.artist, vid)
            continue
        if vid in seen:
            continue
        seen.add(vid)
        to_add.append((vid, src))
        if debug:
            log.debug("  %s — %s -> %s [ADD]", src.title, src.artist, vid)

    plan.present += exact + content
    log.info(
        "%s: %d already on target (%d by content), %d to add",
        name,
        exact + content,
        content,
        len(to_add),
    )

    if to_add and emit_create and kind is CollectionKind.PLAYLIST:
        plan.ops.append(SyncOp(OpKind.CREATE_PLAYLIST, name, kind, collection_key=key))
    for vid, src in to_add:
        plan.ops.append(SyncOp(op_kind, name, kind, collection_key=key, source=src, video_id=vid))

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
        "present": plan.present,
        "degraded": len(plan.degraded),
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
        # repr() so the exact characters are visible — a trailing space or an emoji
        # variation selector in the name is the kind of thing that makes the target-side
        # playlist lookup miss (Spotify matches playlist names byte-for-byte).
        log.info("Source playlist name=%r id=%s count=%s", pl.name, pl.id, pl.count)
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
        # live read of the target playlist. An unmapped collection still looks the
        # playlist up by name, because apply would resolve the same name and add into it.
        persisted_ids = repo.present_target_ids(direction.target.value, row.id) if row else set()
        mapped_id = _target_playlist_id(row, direction)

        def read(mapped=mapped_id, name=pl.name):
            tid = mapped or target.find_playlist_id(name)
            return tid, (target.present_playlist_candidates(tid) if tid else [])

        live = _live_present(read, pl.name, plan, on_event)
        if live is None:
            continue
        target_pl_id, live_cands = live
        present = persisted_ids | {c.video_id for c in live_cands}
        present_index = matching.PresentIndex(live_cands)
        # The single most diagnostic line for "it re-proposes everything": a target
        # playlist that can't be found yields an empty present set, so every matched
        # track looks new. Surfaced at INFO so it shows in the TUI, no -v needed.
        log.info(
            "%r: dedup base — collection_row=%s target_pl_id=%s persisted=%d live=%d present=%d",
            pl.name,
            "yes" if row else "none",
            target_pl_id or "NULL (live read skipped)",
            len(persisted_ids),
            len(live_cands),
            len(present),
        )
        _plan_track_collection(
            plan,
            name=pl.name,
            kind=CollectionKind.PLAYLIST,
            key=pl.id,
            source_playlist_id=pl.id,
            sig=sig,
            sources=tracks,
            present=present,
            present_index=present_index,
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
    persisted_ids = repo.present_target_ids(direction.target.value, row.id) if row else set()
    live_cands = _live_present(target.present_liked_candidates, "Liked Songs", plan, on_event)
    if live_cands is None:
        return
    present = persisted_ids | {c.video_id for c in live_cands}
    present_index = matching.PresentIndex(live_cands)
    log.info(
        "Liked Songs: dedup base — collection_row=%s persisted=%d live=%d present=%d",
        "yes" if row else "none",
        len(persisted_ids),
        len(live_cands),
        len(present),
    )
    _plan_track_collection(
        plan,
        name="Liked Songs",
        kind=CollectionKind.LIKED_SONGS,
        key="liked_songs",
        source_playlist_id=None,
        sig=sig,
        sources=liked,
        present=present,
        present_index=present_index,
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
