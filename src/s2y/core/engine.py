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

import json
import logging
import threading
import time
from collections import defaultdict

from ..config import Config
from ..services.ratelimit import Pacer, SleepFn, chunked
from ..store.repository import Repository
from . import matching, planner
from .events import (
    Advanced,
    ClassifyVerdictEvent,
    Event,
    EventSink,
    LogLine,
    OpApplied,
    PhaseFinished,
    PhaseStarted,
    TrackMatched,
    null_sink,
)
from .matching import normalize_artist, normalize_title
from .models import (
    FORWARD,
    ClassifyDecision,
    ClassifyItem,
    CollectionKind,
    Direction,
    MatchMethod,
    MatchOutcome,
    MatchResult,
    OpKind,
    Side,
    SongInfo,
    SyncOp,
    SyncPlan,
    YtCandidate,
)
from .ports import Classifier, MusicSource, MusicTarget
from .resolve import link_track_map, resolve_quarantine_match

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        spotify: MusicSource,
        ytmusic: MusicTarget,
        repo: Repository,
        config: Config,
        *,
        direction: Direction = FORWARD,
        sleep: SleepFn = time.sleep,
        stop: threading.Event | None = None,
        classifier: Classifier | None = None,
    ) -> None:
        # The two adapters keep their Spotify/YT names (both implement both roles);
        # ``direction`` decides which is read (source) and which is written (target).
        self.spotify = spotify
        self.ytmusic = ytmusic
        self.direction = direction
        if direction.source == Side.SPOTIFY:
            self._source, self._target = spotify, ytmusic
        else:
            self._source, self._target = ytmusic, spotify  # type: ignore[assignment]
        # Public role views (the TUI's playlist picker lists engine.source).
        self.source: MusicSource = self._source
        self.target: MusicTarget = self._target
        self._search = self._target  # the matcher's search surface is the target catalogue
        self.repo = repo
        self.config = config
        # Optional LLM judge for the quarantine (used by classify_quarantine). None
        # keeps every existing call site working; the method no-ops without it.
        self.classifier = classifier
        self._sleep = sleep
        # Cooperative cancellation: the UI sets this on quit / leaving the screen.
        # A never-set event keeps headless (CLI/test) runs running to completion.
        self._stop = stop if stop is not None else threading.Event()
        self._target_playlist_ids: dict[str, str] = {}  # collection_key -> target playlist id
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
                source=self._source,
                target=self._target,
                repo=self.repo,
                config=self.config,
                direction=self.direction,
                on_event=on_event,
                stop=self._stop,
            )

        buffer: list[MatchResult] = []

        def flush() -> None:
            fresh = [m for m in buffer if m.source.source_ref(self.direction.source)]
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
                if (
                    r.outcome is MatchOutcome.AUTO
                    and r.video_id
                    and r.method not in (MatchMethod.CACHED, MatchMethod.PRESENT)
                ):
                    buffer.append(r)
                    if len(buffer) >= 25:
                        flush()

        plan = planner.build_plan(
            source=self._source,
            target=self._target,
            repo=self.repo,
            config=self.config,
            direction=self.direction,
            on_event=sink,
            stop=self._stop,
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
                for op in plan.ops[stopped_at - 1 :]:
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

    # ----- classify (LLM judge over the quarantine) ------------------------- #
    def classify_quarantine(self, on_event: EventSink = null_sink, *, apply: bool = True) -> dict:
        """Run the LLM judge over every unresolved quarantine entry, in batches.

        The quarantine is split into chunks of ``classify.batch_size``. For each
        track we re-search YouTube Music (reusing the matcher's cascade) for a fresh
        candidate list and union in the candidate the human would have seen; the
        whole chunk is then judged in a *single* call to the injected
        :class:`Classifier` (so a large backlog costs a handful of model calls, not
        one per track). A confident ACCEPT (at or above
        ``classify.auto_accept_confidence``) auto-resolves the quarantine via the
        shared :func:`resolve_quarantine_match`; everything else stays for the human.
        With ``apply=False`` nothing is resolved (a preview). The loop is cooperative
        — it bails out promptly when ``stop`` is set, between chunks — and paced like
        the apply loop. No-ops when no classifier is configured.
        """
        stats = {
            "considered": 0,
            "accepted": 0,
            "rejected": 0,
            "uncertain": 0,
            "applied": 0,
        }
        if self.classifier is None:
            return stats

        # Only judge quarantines from THIS direction's source side: a reverse-direction
        # engine searches Spotify, a forward one searches YouTube — judging the wrong
        # side would re-search the wrong catalogue.
        rows = [
            r
            for r in self.repo.list_unresolved_quarantine()
            if r["source_side"] == self.direction.source.value
        ]
        total = len(rows)
        on_event(PhaseStarted("classify", total=total))
        log.info("Classifying %d quarantined track(s) with the LLM judge…", total)
        threshold = self.config.classify.auto_accept_confidence
        limit = self.config.classify.max_candidates
        size = max(1, self.config.classify.batch_size)
        n_batches = (total + size - 1) // size
        pacer = Pacer(self.config.sync.track_sleep, sleep=self._sleep)

        done = 0  # tracks gathered so far, for the "tracks lo–hi of N" narration
        for k, chunk in enumerate(chunked(rows, size), 1):
            if self._stop.is_set():
                log.info("Classify: stop requested — winding down")
                break

            # Gather fresh candidates for every row in the chunk, then judge them all
            # in one model call. Narrate each blocking window (the searches and the
            # model call) *before* it starts so the UI never looks frozen.
            lo, hi = done + 1, done + len(chunk)
            done += len(chunk)
            on_event(LogLine(f"Fetching YouTube candidates — tracks {lo}–{hi} of {total}…"))
            items: list[ClassifyItem] = []
            for row in chunk:
                source = _song_from_quarantine_row(row)
                candidates = _union_stored_best(
                    matching.match(source, self._search).candidates, row, limit=limit
                )
                items.append(ClassifyItem(source, candidates))

            on_event(LogLine(f"Asking Claude about {len(items)} track(s)… (batch {k}/{n_batches})"))
            verdicts = self.classifier.classify_batch(tuple(items))

            for row, verdict in zip(chunk, verdicts, strict=True):
                stats["considered"] += 1
                applied = False
                if verdict.decision is ClassifyDecision.ACCEPT:
                    stats["accepted"] += 1
                    if apply and verdict.video_id and verdict.confidence >= threshold:
                        resolve_quarantine_match(
                            self.repo,
                            quarantine_id=row["id"],
                            source_side=row["source_side"],
                            source_uri=row["source_uri"],
                            target_id=verdict.video_id,
                            title=row["title"],
                            artist=row["artist"],
                            match_method="claude",
                            match_confidence=verdict.confidence,
                        )
                        applied = True
                        stats["applied"] += 1
                elif verdict.decision is ClassifyDecision.REJECT:
                    stats["rejected"] += 1
                else:
                    stats["uncertain"] += 1

                on_event(ClassifyVerdictEvent(row["title"], row["artist"], verdict, applied))
                on_event(Advanced(1))

            pacer.wait()

        log.info(
            "Classify finished: %d considered, %d accepted, %d applied",
            stats["considered"],
            stats["accepted"],
            stats["applied"],
        )
        on_event(PhaseFinished("classify"))
        return stats

    # ----- op application --------------------------------------------------- #
    def _apply_op(self, op: SyncOp) -> str | None:
        """Apply one op. Returns the ``setVideoId`` for a playlist add (needed to
        remove it later in Phase 3), else ``None``."""
        if op.kind is OpKind.CREATE_PLAYLIST:
            self._target_playlist_ids[op.collection_key] = self._target.get_or_create_playlist(
                op.collection
            )
        elif op.kind is OpKind.ADD_PLAYLIST_ITEM:
            pid = self._playlist_id(op)
            if op.video_id:
                set_ids = self._target.add_playlist_items(pid, [op.video_id])
                return set_ids.get(op.video_id)
        elif op.kind is OpKind.LIKE_SONG:
            if op.video_id:
                self._target.like_song(op.video_id)
        elif op.kind is OpKind.ADD_LIBRARY_ALBUM:
            artist = op.source.artist if op.source else ""
            album = op.source.title if op.source else op.collection
            # A failure, so the signature is withheld and the album is retried next run.
            if self._target.like_album(artist, album) is None:
                raise LookupError(f"no matching album on the target: {album!r} by {artist!r}")
        else:  # pragma: no cover - exhaustive
            raise ValueError(f"unknown op kind: {op.kind}")
        return None

    def _playlist_id(self, op: SyncOp) -> str:
        pid = self._target_playlist_ids.get(op.collection_key)
        if pid is None:
            pid = self._target.get_or_create_playlist(op.collection)
            self._target_playlist_ids[op.collection_key] = pid
        return pid

    def _record_landed(self, op: SyncOp, set_video_id: str | None) -> None:
        """Record that a track op landed on YT in the ``playlist_membership`` ledger.

        The identity cache (``track_map``/``reverse_map``) means "we know the target
        id"; membership is the landed-on-target ledger ("it is actually there").
        Recording it after the add succeeds — never before — means a failed/never-
        reached add leaves no membership row, so the next run re-proposes it; dedup is
        driven by membership ∪ live target, never by the (eagerly written) cache.
        Best-effort: a bookkeeping slip must never fail an otherwise-successful apply,
        so it is fully guarded. Albums dedupe via ``present_album_keys()`` and carry no
        membership row. Membership always keys on the track's ``track_map`` row (by its
        Spotify uri), which the reverse cache write guarantees exists.
        """
        if op.kind not in (OpKind.ADD_PLAYLIST_ITEM, OpKind.LIKE_SONG):
            return
        src = op.source
        if src is None:
            return
        # The track_map row is keyed by Spotify uri in both directions: forward that is
        # the source ref; reverse it is the target id (op.video_id).
        spotify_uri = src.spotify_uri if self.direction.source == Side.SPOTIFY else op.video_id
        if not spotify_uri:
            return
        try:
            track = self.repo.get_track_by_spotify_uri(spotify_uri)
            if track is None:
                log.debug(
                    "membership: no track_map row for %s — not recording %s; it will be "
                    "re-proposed next run unless a live read covers it",
                    spotify_uri,
                    op.describe(),
                )
                return
            coll_id = self._collection_id(op)
            with self.repo.transaction():
                if self.direction.target == Side.YT:
                    self.repo.upsert_membership(
                        coll_id, track.id, present_yt=True, yt_set_video_id=set_video_id
                    )
                else:
                    self.repo.upsert_membership(
                        coll_id, track.id, present_spotify=True, spotify_added_at=src.added_at
                    )
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not abort the run
            log.warning("could not record membership for %s: %s", op.describe(), exc)

    def _collection_id(self, op: SyncOp) -> int:
        """The ``playlist_map`` row id for an op's collection (created if needed)."""
        cached = self._coll_ids.get(op.collection_key)
        if cached is not None:
            return cached
        if op.collection_kind is not CollectionKind.PLAYLIST:
            coll = self.repo.get_or_create_collection(
                name=op.collection, kind=op.collection_kind.value
            )
        elif self.direction.source == Side.SPOTIFY:
            coll = self.repo.get_or_create_collection(
                name=op.collection, kind="playlist", spotify_playlist_id=op.collection_key
            )
        else:
            coll = self.repo.get_or_create_collection(
                name=op.collection, kind="playlist", yt_playlist_id=op.collection_key
            )
        self._coll_ids[op.collection_key] = coll.id
        return coll.id

    # ----- persistence ------------------------------------------------------ #
    def _cache_match(self, m: MatchResult) -> None:
        """Upsert one resolved match into the identity cache. Caller owns the transaction.

        Forward writes ``track_map`` keyed on the Spotify uri. Reverse writes
        ``reverse_map`` keyed on the YouTube videoId (its own unique anchor, so it can
        never clobber a forward mapping), and *additionally* links the pair into
        ``track_map`` for membership/forward reuse via the shared
        :func:`s2y.core.resolve.link_track_map` (which holds the no-clobber guard).
        """
        ref = m.source.source_ref(self.direction.source)
        if not ref or not m.video_id:
            return
        verified = m.method is MatchMethod.MANUAL
        if self.direction.source == Side.SPOTIFY:
            self.repo.upsert_track(
                spotify_uri=ref,
                yt_video_id=m.video_id,
                canon_title=normalize_title(m.source.title),
                canon_artist=normalize_artist(m.source.artist),
                isrc=m.source.isrc,
                duration_ms=m.source.duration_ms,
                match_method=m.method.value,
                match_confidence=m.confidence,
                verified=verified,
            )
        else:
            # ref is the YouTube videoId (source); m.video_id is the Spotify uri (target).
            self.repo.upsert_reverse(
                yt_video_id=ref,
                spotify_uri=m.video_id,
                canon_title=normalize_title(m.source.title),
                canon_artist=normalize_artist(m.source.artist),
                match_method=m.method.value,
                match_confidence=m.confidence,
                verified=verified,
            )
            link_track_map(
                self.repo,
                spotify_uri=m.video_id,
                yt_video_id=ref,
                canon_title=normalize_title(m.source.title),
                canon_artist=normalize_artist(m.source.artist),
                duration_ms=m.source.duration_ms,
                match_method=m.method.value,
                match_confidence=m.confidence,
                verified=verified,
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
                    source_side=self.direction.source.value,
                    source_uri=q.source.source_ref(self.direction.source),
                    title=q.source.title,
                    artist=q.source.artist,
                    album=q.source.album or None,
                    reason=q.reason or q.outcome.value,
                    best_candidate=_candidate_dict(q.best),
                )

    def _persist_signatures(self, plan: SyncPlan, failures: dict[str, int]) -> None:
        """Record collection mappings; store the change-detect signature only on
        full success.

        We always persist the target playlist-id mapping (even on partial failure)
        so the next run can dedupe against the live target playlist and not re-add
        what already landed. But the source signature is stored only when every op
        succeeded — otherwise the next run must reprocess the collection to retry
        the failed adds (a failed add must not hide behind 'nothing changed').
        """
        with self.repo.transaction():
            for cs in plan.collections:
                if self.direction.source == Side.SPOTIFY:
                    coll = self.repo.get_or_create_collection(
                        name=cs.name, kind=cs.kind.value, spotify_playlist_id=cs.spotify_playlist_id
                    )
                else:
                    coll = self.repo.get_or_create_collection(
                        name=cs.name, kind=cs.kind.value, yt_playlist_id=cs.yt_playlist_id
                    )
                updates: dict[str, str] = {}
                target_pl_id = self._target_playlist_ids.get(cs.key)
                if target_pl_id:
                    if self.direction.target == Side.YT:
                        # yt_playlist_id has no UNIQUE constraint — always safe to write.
                        updates["yt_playlist_id"] = target_pl_id
                    else:
                        # spotify_playlist_id is UNIQUE. A reverse YT playlist whose name
                        # matches an existing Spotify playlist resolves (by name) to an id
                        # that another collection row may already own — claiming it here
                        # would crash the whole apply on the unique constraint. Only write
                        # it when this row already owns it or nobody does; otherwise leave
                        # it NULL and let dedup fall back to membership ∪ a live read by name.
                        owner = self.repo.get_collection_by_spotify_id(target_pl_id)
                        if owner is None or owner.id == coll.id:
                            updates["spotify_playlist_id"] = target_pl_id
                        else:
                            log.debug(
                                "reverse: not claiming spotify_playlist_id %s for %r — owned "
                                "by collection %d; stays unmapped, dedup falls back to "
                                "membership + a live read by name",
                                target_pl_id,
                                cs.name,
                                owner.id,
                            )
                clean = not failures.get(cs.key)
                if not clean:
                    log.info(
                        "%r: withholding change signature — %d op(s) failed; the collection "
                        "will be reprocessed next run to retry them",
                        cs.name,
                        failures.get(cs.key, 0),
                    )
                if clean:
                    updates[self.direction.source_sig_col] = cs.signature
                if updates:
                    self.repo.update_collection(coll.id, **updates)
                if clean:
                    # A source snapshot gives Phase 2's diff a base to compare against.
                    self.repo.save_snapshot(
                        self.direction.source.value, coll.id, {"signature": cs.signature}
                    )


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


def _song_from_quarantine_row(row) -> SongInfo:
    """Reconstruct a :class:`SongInfo` from a quarantine row for re-searching.

    The source ref goes into the field matching the row's ``source_side`` so the
    rebuilt song is honest about its origin. Duration/ISRC aren't stored on the row;
    that's fine — the matcher still returns the same candidate list, it just can't
    apply its own duration bonus.
    """
    ref_field = "spotify_uri" if row["source_side"] == Side.SPOTIFY else "yt_video_id"
    return SongInfo(
        title=row["title"],
        artist=row["artist"],
        album=row["album"] or "",
        **{ref_field: row["source_uri"]},
    )


def _union_stored_best(
    candidates: tuple[YtCandidate, ...], row, *, limit: int
) -> tuple[YtCandidate, ...]:
    """Fresh candidates (capped at ``limit``) plus the row's stored best, if missing.

    Guarantees the candidate the human would have reviewed is always among those
    the judge sees, even if YouTube search results have since drifted.
    """
    out = list(candidates[:limit])
    raw = row["best_candidate_json"]
    if not raw:
        return tuple(out)
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return tuple(out)
    vid = d.get("video_id") if isinstance(d, dict) else None
    if vid and vid not in {c.video_id for c in out}:
        out.append(
            YtCandidate(
                video_id=vid,
                title=d.get("title", "") or "",
                artist=d.get("artist", "") or "",
                album=d.get("album", "") or "",
                duration_s=d.get("duration_s"),
            )
        )
    return tuple(out)
