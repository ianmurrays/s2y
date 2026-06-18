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
from .events import Advanced, EventSink, OpApplied, PhaseFinished, PhaseStarted, null_sink
from .matching import normalize_artist, normalize_title
from .models import MatchMethod, OpKind, SyncOp, SyncPlan, YtCandidate
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

    # ----- planning --------------------------------------------------------- #
    def build_plan(self, on_event: EventSink = null_sink) -> SyncPlan:
        return planner.build_plan(
            spotify=self.spotify, ytmusic=self.ytmusic, repo=self.repo,
            config=self.config, on_event=on_event, stop=self._stop,
        )

    # ----- applying --------------------------------------------------------- #
    def apply_plan(self, plan: SyncPlan, on_event: EventSink = null_sink) -> dict:
        on_event(PhaseStarted("apply", total=len(plan.ops)))
        log.info("Applying %d operation(s)…", len(plan.ops))

        self._persist_matches(plan)
        self._persist_quarantine(plan)

        pacer = Pacer(self.config.sync.track_sleep, sleep=self._sleep)
        failures: dict[str, int] = defaultdict(int)
        total = len(plan.ops)
        applied = failed = 0
        stopped_at = 0  # 1-based index of the first op we did NOT apply (0 = ran fully)
        for i, op in enumerate(plan.ops, 1):
            if self._stop.is_set():
                # The UI asked us to wind down (quit / left the screen). Stop now
                # and treat every remaining op's collection as incomplete below.
                stopped_at = i
                log.info("Apply: stop requested at %d/%d — winding down", i, total)
                break
            try:
                self._apply_op(op)
                applied += 1
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
            # Withhold the signature for any collection with an unapplied op so the
            # next run reprocesses it (same invariant as a failed add).
            for op in plan.ops[stopped_at - 1:]:
                failures[op.collection_key] += 1

        self._persist_signatures(plan, failures)
        log.info("Apply finished: %d applied, %d failed", applied, failed)
        on_event(PhaseFinished("apply"))
        return {
            "applied": applied,
            "failed": failed,
            "quarantined": len(plan.quarantine),
            "skipped": plan.skipped,
        }

    # ----- op application --------------------------------------------------- #
    def _apply_op(self, op: SyncOp) -> None:
        if op.kind is OpKind.CREATE_PLAYLIST:
            self._yt_playlist_ids[op.collection_key] = self.ytmusic.get_or_create_playlist(
                op.collection
            )
        elif op.kind is OpKind.ADD_PLAYLIST_ITEM:
            pid = self._playlist_id(op)
            if op.video_id:
                self.ytmusic.add_playlist_items(pid, [op.video_id])
        elif op.kind is OpKind.LIKE_SONG:
            if op.video_id:
                self.ytmusic.like_song(op.video_id)
        elif op.kind is OpKind.ADD_LIBRARY_ALBUM:
            artist = op.source.artist if op.source else ""
            album = op.source.title if op.source else op.collection
            self.ytmusic.like_album(artist, album)
        else:  # pragma: no cover - exhaustive
            raise ValueError(f"unknown op kind: {op.kind}")

    def _playlist_id(self, op: SyncOp) -> str:
        pid = self._yt_playlist_ids.get(op.collection_key)
        if pid is None:
            pid = self.ytmusic.get_or_create_playlist(op.collection)
            self._yt_playlist_ids[op.collection_key] = pid
        return pid

    # ----- persistence ------------------------------------------------------ #
    def _persist_matches(self, plan: SyncPlan) -> None:
        if not plan.matches:
            return
        with self.repo.transaction():
            for m in plan.matches:
                if not m.source.spotify_uri:
                    continue
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
