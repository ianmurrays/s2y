"""The sync engine: orchestrates planning and applying.

``build_plan`` is a thin delegate to :mod:`s2y.core.planner` (read-only).
``apply_plan`` performs the writes and folds the results back into the local
store — persisting the cross-service map, quarantine, and per-collection
signatures so the next run is incremental and idempotent.

The engine is constructed with injected service adapters (the ports), a
:class:`Repository`, and a :class:`Config`, which makes it fully testable with
in-memory fakes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from ..config import Config
from ..services.ratelimit import Pacer, SleepFn
from ..store.repository import Repository
from . import planner
from .events import (
    Advanced,
    Event,
    EventSink,
    OpApplied,
    PhaseFinished,
    PhaseStarted,
    TrackMatched,
    null_sink,
)
from .matching import normalize_artist, normalize_title
from .models import (
    CollectionKind,
    MatchMethod,
    MatchOutcome,
    MatchResult,
    OpKind,
    SyncOp,
    SyncPlan,
    YtCandidate,
)
from .ports import SpotifySource, YtMusicTarget

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        spotify: SpotifySource,
        ytmusic: YtMusicTarget,
        repo: Repository,
        config: Config,
        *,
        sleep: SleepFn = time.sleep,
        stop: threading.Event | None = None,
    ) -> None:
        self.spotify = spotify
        self.ytmusic = ytmusic
        self.repo = repo
        self.config = config
        self._sleep = sleep
        # Cooperative cancellation: the UI sets this on quit / leaving the screen.
        # A never-set event keeps headless (CLI/test) runs running to completion.
        self._stop = stop if stop is not None else threading.Event()
        self._yt_playlist_ids: dict[str, str] = {}  # collection_key -> yt playlist id
        self._coll_ids: dict[str, int] = {}  # collection_key -> playlist_map row id

    # ----- planning --------------------------------------------------------- #
    def build_plan(
        self, on_event: EventSink = null_sink, *, persist_matches: bool = False
    ) -> SyncPlan:
        """Build the plan. Read-only by default (``--dry-run`` writes nothing).

        With ``persist_matches`` (a real, non-dry run), each freshly resolved match
        is written to ``track_map`` as it is found — so an interruption mid-matching
        (e.g. the YouTube session expiring during the search-heavy phase) keeps the
        work done so far, and the re-run resolves those tracks from cache instead of
        re-searching from scratch. The planner stays pure: it only emits
        ``TrackMatched`` events; persistence is the engine's concern here.
        """
        if not persist_matches:
            return planner.build_plan(
                spotify=self.spotify, ytmusic=self.ytmusic, repo=self.repo,
                config=self.config, on_event=on_event, stop=self._stop,
            )

        buffer: list[MatchResult] = []

        def flush() -> None:
            fresh = [m for m in buffer if m.source.spotify_uri]
            buffer.clear()
            if not fresh:
                return
            with self.repo.transaction():
                for m in fresh:
                    self._cache_match(m)

        def sink(event: Event) -> None:
            on_event(event)
            if isinstance(event, TrackMatched):
                r = event.result
                # Only persist genuinely new resolutions; cached hits are already
                # in track_map, and quarantine/no-match carry no videoId.
                if r.outcome is MatchOutcome.AUTO and r.video_id and r.method is not MatchMethod.CACHED:
                    buffer.append(r)
                    if len(buffer) >= 25:
                        flush()

        plan = planner.build_plan(
            spotify=self.spotify, ytmusic=self.ytmusic, repo=self.repo,
            config=self.config, on_event=sink, stop=self._stop,
        )
        flush()
        return plan

    # ----- applying --------------------------------------------------------- #
    def apply_plan(self, plan: SyncPlan, on_event: EventSink = null_sink) -> dict:
        on_event(PhaseStarted("apply", total=len(plan.ops)))
        log.info("Applying %d operation(s)…", len(plan.ops))

        run_id = self.repo.start_run("sync", dry_run=False)
        self._persist_matches(plan)
        self._persist_quarantine(plan)

        pacer = Pacer(self.config.sync.track_sleep, sleep=self._sleep)
        failures: dict[str, int] = defaultdict(int)
        total = len(plan.ops)
        applied = failed = 0
        stopped_at = 0  # 1-based index of the first op we did NOT apply (0 = ran fully)
        try:
            for i, op in enumerate(plan.ops, 1):
                if self._stop.is_set():
                    # The UI asked us to wind down (quit / left the screen). Stop now
                    # and treat every remaining op's collection as incomplete below.
                    stopped_at = i
                    log.info("Apply: stop requested at %d/%d — winding down", i, total)
                    break
                try:
                    set_video_id = self._apply_op(op)
                    applied += 1
                    # Durably record what just landed on YT (its own commit), so a
                    # crash here can't lose it and the re-run dedupes against state.
                    self._record_landed(op, set_video_id)
                    log.info("applied %d/%d: %s", i, total, op.describe())
                    on_event(OpApplied(op, ok=True))
                except Exception as exc:  # noqa: BLE001 — one bad op must not abort the run
                    failed += 1
                    failures[op.collection_key] += 1
                    log.warning("apply failed %d/%d: %s: %s", i, total, op.describe(), exc)
                    on_event(OpApplied(op, ok=False, error=str(exc)))
                on_event(Advanced(1))
                pacer.wait()

            if stopped_at:
                # Withhold the signature for any collection with an unapplied op so
                # the next run reprocesses it (same invariant as a failed add).
                for op in plan.ops[stopped_at - 1:]:
                    failures[op.collection_key] += 1

            self._persist_signatures(plan, failures)
        except Exception:
            self.repo.finish_run(run_id, "error", {"applied": applied, "failed": failed})
            raise

        stats = {
            "applied": applied,
            "failed": failed,
            "quarantined": len(plan.quarantine),
            "skipped": plan.skipped,
        }
        status = "aborted" if stopped_at else ("error" if failed else "ok")
        self.repo.finish_run(run_id, status, stats)
        log.info("Apply finished: %d applied, %d failed", applied, failed)
        on_event(PhaseFinished("apply"))
        return stats

    # ----- op application --------------------------------------------------- #
    def _apply_op(self, op: SyncOp) -> str | None:
        """Apply one op. Returns the ``setVideoId`` for a playlist add (needed to
        remove it later in Phase 3), else ``None``."""
        if op.kind is OpKind.CREATE_PLAYLIST:
            self._yt_playlist_ids[op.collection_key] = self.ytmusic.get_or_create_playlist(
                op.collection
            )
        elif op.kind is OpKind.ADD_PLAYLIST_ITEM:
            pid = self._playlist_id(op)
            if op.video_id:
                set_ids = self.ytmusic.add_playlist_items(pid, [op.video_id])
                return set_ids.get(op.video_id)
        elif op.kind is OpKind.LIKE_SONG:
            if op.video_id:
                self.ytmusic.like_song(op.video_id)
        elif op.kind is OpKind.ADD_LIBRARY_ALBUM:
            artist = op.source.artist if op.source else ""
            album = op.source.title if op.source else op.collection
            self.ytmusic.like_album(artist, album)
        else:  # pragma: no cover - exhaustive
            raise ValueError(f"unknown op kind: {op.kind}")
        return None

    def _playlist_id(self, op: SyncOp) -> str:
        pid = self._yt_playlist_ids.get(op.collection_key)
        if pid is None:
            pid = self.ytmusic.get_or_create_playlist(op.collection)
            self._yt_playlist_ids[op.collection_key] = pid
        return pid

    def _record_landed(self, op: SyncOp, set_video_id: str | None) -> None:
        """Record that a track op landed on YT in the ``playlist_membership`` ledger.

        ``track_map`` is the identity cache ("we know the videoId"); membership is
        the landed-on-YT ledger ("it is actually there"). Recording it after the add
        succeeds — never before — means a failed/never-reached add leaves no
        membership row, so the next run re-proposes it; dedup is driven by membership
        ∪ live YT, never by the (eagerly written) cache. Best-effort: a bookkeeping
        slip must never fail an otherwise-successful apply, so it is fully guarded.
        Albums dedupe via ``library_album_keys()`` and carry no membership row.
        """
        if op.kind not in (OpKind.ADD_PLAYLIST_ITEM, OpKind.LIKE_SONG):
            return
        src = op.source
        if src is None or not src.spotify_uri:
            return
        try:
            track = self.repo.get_track_by_spotify_uri(src.spotify_uri)
            if track is None:
                return
            coll_id = self._collection_id(op)
            with self.repo.transaction():
                self.repo.upsert_membership(
                    coll_id, track.id, present_yt=True, yt_set_video_id=set_video_id
                )
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not abort the run
            log.warning("could not record membership for %s: %s", op.describe(), exc)

    def _collection_id(self, op: SyncOp) -> int:
        """The ``playlist_map`` row id for an op's collection (created if needed)."""
        cached = self._coll_ids.get(op.collection_key)
        if cached is not None:
            return cached
        spotify_pl_id = (
            op.collection_key if op.collection_kind is CollectionKind.PLAYLIST else None
        )
        coll = self.repo.get_or_create_collection(
            name=op.collection, kind=op.collection_kind.value, spotify_playlist_id=spotify_pl_id,
        )
        self._coll_ids[op.collection_key] = coll.id
        return coll.id

    # ----- persistence ------------------------------------------------------ #
    def _cache_match(self, m: MatchResult) -> None:
        """Upsert one resolved match into ``track_map``. Caller owns the transaction."""
        if not m.source.spotify_uri:
            return
        self.repo.upsert_track(
            spotify_uri=m.source.spotify_uri,
            yt_video_id=m.video_id,
            canon_title=normalize_title(m.source.title),
            canon_artist=normalize_artist(m.source.artist),
            isrc=m.source.isrc,
            duration_ms=m.source.duration_ms,
            match_method=m.method.value,
            match_confidence=m.confidence,
            verified=(m.method is MatchMethod.MANUAL),
        )

    def _persist_matches(self, plan: SyncPlan) -> None:
        if not plan.matches:
            return
        with self.repo.transaction():
            for m in plan.matches:
                self._cache_match(m)

    def _persist_quarantine(self, plan: SyncPlan) -> None:
        if not plan.quarantine:
            return
        with self.repo.transaction():
            for q in plan.quarantine:
                self.repo.add_quarantine(
                    source_side="spotify",
                    source_uri=q.source.spotify_uri,
                    title=q.source.title,
                    artist=q.source.artist,
                    album=q.source.album or None,
                    reason=q.reason or q.outcome.value,
                    best_candidate=_candidate_dict(q.best),
                )

    def _persist_signatures(self, plan: SyncPlan, failures: dict[str, int]) -> None:
        """Record collection mappings; store the change-detect signature only on
        full success.

        We always persist the ``yt_playlist_id`` mapping (even on partial failure)
        so the next run can dedupe against the live YT playlist and not re-add what
        already landed. But the source signature is stored only when every op
        succeeded — otherwise the next run must reprocess the collection to retry
        the failed adds (a failed add must not hide behind 'nothing changed').
        """
        with self.repo.transaction():
            for cs in plan.collections:
                coll = self.repo.get_or_create_collection(
                    name=cs.name, kind=cs.kind.value,
                    spotify_playlist_id=cs.spotify_playlist_id,
                )
                updates: dict[str, str] = {}
                yt_id = self._yt_playlist_ids.get(cs.key)
                if yt_id:
                    updates["yt_playlist_id"] = yt_id
                clean = not failures.get(cs.key)
                if clean:
                    updates["spotify_snapshot_id"] = cs.signature
                if updates:
                    self.repo.update_collection(coll.id, **updates)
                if clean:
                    # A source snapshot gives Phase 2's diff a base to compare against.
                    self.repo.save_snapshot("spotify", coll.id, {"signature": cs.signature})


def _candidate_dict(cand: YtCandidate | None) -> dict | None:
    if cand is None:
        return None
    return {
        "video_id": cand.video_id,
        "title": cand.title,
        "artist": cand.artist,
        "album": cand.album,
        "duration_s": cand.duration_s,
    }
