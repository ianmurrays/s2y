"""End-to-end engine tests with in-memory fakes + a :memory: store."""

from __future__ import annotations

import threading
from collections import defaultdict

import pytest

from s2y.config import Config
from s2y.core.engine import Engine
from s2y.core.events import (
    Advanced,
    DedupDegraded,
    LogLine,
    PhaseFinished,
    PhaseStarted,
)
from s2y.core.matching import normalize_artist, normalize_title
from s2y.core.models import (
    ClassifyDecision,
    ClassifyVerdict,
    OpKind,
    RemoteAlbum,
    RemotePlaylist,
    SongInfo,
    YtCandidate,
)
from s2y.store import connect
from s2y.store.repository import Repository

NO_SLEEP = lambda _: None  # noqa: E731


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeSpotify:
    def __init__(self, *, playlists, tracks, liked=None, albums=None):
        self._playlists = playlists
        self._tracks = tracks
        self._liked = liked or []
        self._albums = albums or []

    def verify(self):
        return "Ian"

    def list_playlists(self):
        return list(self._playlists)

    def playlist_tracks(self, pid):
        return list(self._tracks.get(pid, []))

    def liked_songs(self):
        return list(self._liked)

    def liked_albums(self):
        return list(self._albums)


class FakeYT:
    """Catalog-backed YT fake whose state mutates as writes are applied."""

    def __init__(self, catalog, *, fail_video_ids=None):
        self.catalog = catalog  # source-title -> YtCandidate
        self._pl_name_to_id: dict[str, str] = {}
        self._pl_contents: dict[str, set[str]] = defaultdict(set)
        self._liked: set[str] = set()
        self._album_keys: set[tuple[str, str]] = set()
        self._fail = set(fail_video_ids or set())
        self._seq = 1000
        self.created: list[str] = []
        self.added: list[tuple[str, list[str]]] = []
        self.song_likes: list[str] = []
        self.album_likes: list[tuple[str, str]] = []
        self.search_calls = 0

    def verify(self):
        return "YouTube Music"

    def list_playlists(self):
        return [RemotePlaylist(id=i, name=n) for n, i in self._pl_name_to_id.items()]

    def playlist_video_ids(self, pid):
        return set(self._pl_contents.get(pid, set()))

    def liked_video_ids(self):
        return set(self._liked)

    def library_album_keys(self):
        return set(self._album_keys)

    def search_songs(self, query, *, limit=6):
        self.search_calls += 1
        ql = query.lower()
        return [c for t, c in self.catalog.items() if t.lower() in ql]

    def search_videos(self, query, *, limit=4):
        return []

    def album_candidates(self, artist, album, *, limit_albums=3):
        return []

    def get_or_create_playlist(self, name):
        if name not in self._pl_name_to_id:
            self._seq += 1
            self._pl_name_to_id[name] = f"pl{self._seq}"
            self.created.append(name)
        return self._pl_name_to_id[name]

    def add_playlist_items(self, pid, video_ids):
        for v in video_ids:
            if v in self._fail:
                raise RuntimeError(f"add failed for {v}")
        self.added.append((pid, list(video_ids)))
        for v in video_ids:
            self._pl_contents[pid].add(v)
        return {v: f"set-{v}" for v in video_ids}

    def like_song(self, video_id):
        self.song_likes.append(video_id)
        self._liked.add(video_id)

    def like_album(self, artist, album):
        self.album_likes.append((artist, album))
        self._album_keys.add((normalize_artist(artist), normalize_title(album)))
        return "MPRE-x"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def cand(vid, title, artist, dur):
    return YtCandidate(video_id=vid, title=title, artist=artist, duration_s=dur)


@pytest.fixture
def repo():
    return Repository(connect(":memory:"))


@pytest.fixture
def config():
    return Config()  # all collections on, union policy, track_sleep 0.1


def make_world(*, with_quarantine=False, fail=None):
    t1 = SongInfo(
        "Blinding Lights",
        "The Weeknd",
        "After Hours",
        duration_ms=200000,
        spotify_uri="spotify:track:1",
    )
    t2 = SongInfo(
        "Levitating",
        "Dua Lipa",
        "Future Nostalgia",
        duration_ms=203000,
        spotify_uri="spotify:track:2",
    )
    liked = SongInfo(
        "As It Was",
        "Harry Styles",
        "Harry's House",
        duration_ms=167000,
        spotify_uri="spotify:track:3",
    )
    tracks = [t1, t2]
    if with_quarantine:
        tracks.append(
            SongInfo(
                "Totally Obscure Bootleg",
                "Nobody At All",
                duration_ms=999000,
                spotify_uri="spotify:track:x",
            )
        )
    spotify = FakeSpotify(
        playlists=[RemotePlaylist(id="sp1", name="Chill", count=len(tracks), snapshot_id="snap-1")],
        tracks={"sp1": tracks},
        liked=[liked],
        albums=[RemoteAlbum(id="al1", name="Random Access Memories", artist="Daft Punk")],
    )
    catalog = {
        "Blinding Lights": cand("yv1", "Blinding Lights", "The Weeknd", 200),
        "Levitating": cand("yv2", "Levitating", "Dua Lipa", 203),
        "As It Was": cand("yv3", "As It Was", "Harry Styles", 167),
    }
    yt = FakeYT(catalog, fail_video_ids=fail)
    return spotify, yt


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_build_plan_produces_expected_ops(repo, config):
    spotify, yt = make_world()
    plan = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()

    kinds = [op.kind for op in plan.ops]
    assert kinds.count(OpKind.CREATE_PLAYLIST) == 1
    assert kinds.count(OpKind.ADD_PLAYLIST_ITEM) == 2
    assert kinds.count(OpKind.LIKE_SONG) == 1
    assert kinds.count(OpKind.ADD_LIBRARY_ALBUM) == 1
    assert plan.quarantine == []
    assert len(plan.matches) == 3  # 2 playlist + 1 liked (albums aren't track matches)


def test_apply_executes_writes(repo, config):
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    stats = eng.apply_plan(eng.build_plan())

    assert yt.created == ["Chill"]
    assert {v for _, vids in yt.added for v in vids} == {"yv1", "yv2"}
    assert yt.song_likes == ["yv3"]
    assert yt.album_likes == [("Daft Punk", "Random Access Memories")]
    assert stats["applied"] == 5
    assert stats["failed"] == 0


def test_dry_run_makes_no_writes(repo, config):
    spotify, yt = make_world()
    Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()  # plan only
    assert yt.created == [] and yt.added == [] and yt.song_likes == []
    # No DB rows written either.
    assert repo.list_collections() == []
    assert repo.get_track_by_spotify_uri("spotify:track:1") is None


def test_rerun_is_idempotent_via_change_detect(repo, config):
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())

    # Fresh engine, same world/snapshots -> everything is unchanged.
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert plan2.is_empty
    assert plan2.ops == []


def test_present_dedup_when_snapshot_changes(repo, config):
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())

    # Same tracks but a new snapshot id -> collection is re-examined, yet the
    # tracks are already on the YT side, so no add ops are produced.
    spotify._playlists = [RemotePlaylist(id="sp1", name="Chill", count=2, snapshot_id="snap-2")]
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert [op for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM] == []


def test_cached_matches_avoid_research(repo, config):
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())

    yt.search_calls = 0
    spotify._playlists = [RemotePlaylist(id="sp1", name="Chill", count=2, snapshot_id="snap-2")]
    Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert yt.search_calls == 0  # all resolved from track_map


def test_quarantine_is_persisted_and_not_rematched(repo, config):
    spotify, yt = make_world(with_quarantine=True)
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    plan = eng.build_plan()
    assert len(plan.quarantine) == 1
    eng.apply_plan(plan)
    assert repo.count_unresolved_quarantine() == 1

    # New snapshot -> re-examined, but the quarantined track is pending review and
    # must not be matched again (no new quarantine row, no op).
    spotify._playlists = [RemotePlaylist(id="sp1", name="Chill", count=3, snapshot_id="snap-2")]
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert plan2.quarantine == []
    assert repo.count_unresolved_quarantine() == 1


def test_failed_op_keeps_collection_unsynced_for_retry(repo, config):
    spotify, yt = make_world(fail={"yv2"})  # adding Levitating will fail
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    stats = eng.apply_plan(eng.build_plan())
    assert stats["failed"] == 1

    # The playlist signature must NOT be stored (so the missing add is retried),
    # while Liked Songs/Albums (which succeeded) are recorded.
    chill = repo.get_collection_by_spotify_id("sp1")
    assert chill is None or chill.spotify_snapshot_id is None

    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert "yv2" in add_ids  # retried
    assert "yv1" not in add_ids  # already present, not re-added


def test_resolved_quarantine_is_applied_on_next_run(repo, config):
    spotify, yt = make_world(with_quarantine=True)
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())  # the bootleg track gets quarantined
    assert repo.count_unresolved_quarantine() == 1

    # Simulate the review screen: resolve to a chosen video, invalidate signatures.
    q = repo.list_unresolved_quarantine()[0]
    track = repo.upsert_track(
        spotify_uri=q["source_uri"],
        yt_video_id="resolved_vid",
        canon_title="x",
        canon_artist="y",
        match_method="manual",
        match_confidence=1.0,
        verified=True,
    )
    repo.resolve_quarantine(q["id"], track.id)
    repo.invalidate_signatures()

    # Same Spotify snapshot, yet the resolved track must now be added (and the
    # already-synced tracks must not be re-added).
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert add_ids == ["resolved_vid"]


def test_selected_playlists_filter(repo, config):
    spotify, yt = make_world()
    config.collections.selected_playlists = ["other-id"]  # excludes sp1
    config.collections.sync_liked_songs = False
    config.collections.sync_liked_albums = False
    plan = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert plan.ops == []


def test_empty_selection_means_all_or_none_by_master_toggle(repo, config):
    # An empty selected_playlists is interpreted by the sync_playlists toggle:
    # enabled => sync ALL; disabled => sync NONE. This is what the picker relies
    # on so that "0 checked" (saved as disabled+[]) never means "sync everything".
    spotify, yt = make_world()
    config.collections.selected_playlists = []
    config.collections.sync_liked_songs = False
    config.collections.sync_liked_albums = False

    config.collections.sync_playlists = True  # empty + enabled == all
    assert Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan().ops

    config.collections.sync_playlists = False  # the picker's 0-checked state
    assert Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan().ops == []


# --------------------------------------------------------------------------- #
# Cooperative cancellation (the quit/leave-screen path)
# --------------------------------------------------------------------------- #
def test_build_plan_bails_out_when_stop_preset(repo, config):
    spotify, yt = make_world()
    stop = threading.Event()
    stop.set()  # as if the user quit before planning started
    plan = Engine(spotify, yt, repo, config, sleep=NO_SLEEP, stop=stop).build_plan()
    assert plan.ops == []
    assert plan.matches == []
    assert yt.search_calls == 0  # never reached the matching loop


def test_apply_stops_when_signaled_and_retries_next_run(repo, config):
    spotify, yt = make_world()
    stop = threading.Event()
    # Trip the stop the moment the first playlist add lands, so the run winds
    # down mid-plan (like pressing q during apply).
    orig_add = yt.add_playlist_items

    def add_then_stop(pid, vids):
        result = orig_add(pid, vids)
        stop.set()
        return result

    yt.add_playlist_items = add_then_stop

    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP, stop=stop)
    plan = eng.build_plan()  # built before the stop trips: a full plan
    stats = eng.apply_plan(plan)

    # Some work happened, but we stopped before finishing the whole plan.
    assert 0 < stats["applied"] < len(plan.ops)
    # Matches are persisted up front regardless of the early stop.
    assert repo.get_track_by_spotify_uri("spotify:track:1") is not None
    # No collection touched by an unapplied op keeps its signature.
    chill = repo.get_collection_by_spotify_id("sp1")
    assert chill is None or chill.spotify_snapshot_id is None

    # A fresh engine (clear stop) reprocesses and retries the missing add, but
    # does not re-add what already landed.
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert "yv2" in add_ids  # the add we never reached is retried
    assert "yv1" not in add_ids  # already present, not re-added


# --------------------------------------------------------------------------- #
# Resumability: durable membership ledger + guarded dedup + incremental matching
# --------------------------------------------------------------------------- #
def test_membership_recorded_on_add(repo, config):
    """Each successful add is durably recorded (present_yt + setVideoId)."""
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())

    chill = repo.get_collection_by_spotify_id("sp1")
    assert repo.yt_present_video_ids(chill.id) == {"yv1", "yv2"}
    t1 = repo.get_track_by_spotify_uri("spotify:track:1")
    m = repo.get_membership(chill.id, t1.id)
    assert m["present_yt"] == 1
    assert m["yt_set_video_id"] == "set-yv1"  # captured from add_playlist_items

    # Liked Songs membership is recorded too (rating state has no setVideoId).
    liked = repo.get_collection_by_kind("liked_songs")
    assert repo.yt_present_video_ids(liked.id) == {"yv3"}


def test_resume_dedups_via_membership_when_live_read_empty(repo, config):
    """The core data-loss regression: a track that already landed must not be
    re-added even when the live YT read comes back empty (e.g. a just-reauthed or
    flaky session). Dedup is driven by the persisted ledger, not just live YT."""
    spotify, yt = make_world(fail={"yv2"})  # yv1 lands, yv2 fails -> sig withheld
    Engine(spotify, yt, repo, config, sleep=NO_SLEEP).apply_plan(
        Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    )

    # Simulate a live read that knows nothing (signed back in, cache cold).
    yt.playlist_video_ids = lambda pid: set()
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert "yv1" not in add_ids  # known-landed via membership — NOT re-added
    assert "yv2" in add_ids  # genuinely missing — retried


def test_live_dedup_failure_degrades_instead_of_aborting(repo, config):
    """A signed-out live read must not take down the whole re-run; it degrades to
    the persisted membership and emits DedupDegraded."""
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())  # full apply -> membership {yv1, yv2}

    def boom(pid):
        raise RuntimeError("signinendpoint: please sign in")

    yt.playlist_video_ids = boom
    spotify._playlists = [RemotePlaylist(id="sp1", name="Chill", count=2, snapshot_id="snap-2")]

    events: list = []
    plan2 = Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan(events.append)
    assert any(isinstance(e, DedupDegraded) for e in events)  # did not raise
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert add_ids == []  # persisted membership covers both tracks


def test_matching_persisted_incrementally_avoids_research(repo, config):
    """With persist_matches, resolutions are cached as they happen — so a crash
    mid-matching keeps the work and the re-run does not re-search YouTube."""
    spotify, yt = make_world()
    # A real (non-dry) plan that never reaches apply_plan.
    Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan(persist_matches=True)
    assert repo.get_track_by_spotify_uri("spotify:track:1") is not None
    assert repo.get_track_by_spotify_uri("spotify:track:2") is not None

    yt.search_calls = 0
    spotify._playlists = [RemotePlaylist(id="sp1", name="Chill", count=2, snapshot_id="snap-2")]
    Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert yt.search_calls == 0  # everything resolved from the incrementally-built cache


def test_dry_run_build_plan_opens_no_run(repo, config):
    """A read-only plan (persist_matches=False, the default) opens no sync_run row
    and writes no track_map — so dry runs never pollute resume state."""
    spotify, yt = make_world()
    Engine(spotify, yt, repo, config, sleep=NO_SLEEP).build_plan()
    assert repo.last_run() is None
    assert repo.get_track_by_spotify_uri("spotify:track:1") is None


def test_run_status_reflects_outcome(repo, config):
    # Clean apply -> ok.
    spotify, yt = make_world()
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())
    assert repo.last_run()["status"] == "ok"

    # A failed op -> error.
    repo2 = Repository(connect(":memory:"))
    spotify, yt = make_world(fail={"yv2"})
    eng = Engine(spotify, yt, repo2, config, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan())
    assert repo2.last_run()["status"] == "error"

    # A cooperative stop mid-apply -> aborted.
    repo3 = Repository(connect(":memory:"))
    spotify, yt = make_world()
    stop = threading.Event()
    orig_add = yt.add_playlist_items

    def add_then_stop(pid, vids):
        result = orig_add(pid, vids)
        stop.set()
        return result

    yt.add_playlist_items = add_then_stop
    eng = Engine(spotify, yt, repo3, config, sleep=NO_SLEEP, stop=stop)
    eng.apply_plan(eng.build_plan())
    assert repo3.last_run()["status"] == "aborted"


# --------------------------------------------------------------------------- #
# LLM quarantine judge (classify_quarantine) — with an in-memory fake classifier
# --------------------------------------------------------------------------- #
class FakeClassifier:
    """Scripted verdicts keyed by source title; records titles seen + batch calls."""

    def __init__(self, scripted: dict):
        self.scripted = scripted
        self.calls: list[str] = []  # every source title judged, flattened
        self.batch_calls = 0  # how many times classify_batch ran (= claude -p spawns)

    def classify_batch(self, items):
        self.batch_calls += 1
        self.calls.extend(i.source.title for i in items)
        return tuple(
            self.scripted.get(
                i.source.title, ClassifyVerdict(ClassifyDecision.UNCERTAIN, None, 0.0)
            )
            for i in items
        )


def _classify_world(repo, *, verdict):
    """A minimal world: one seeded quarantine row whose re-search finds a candidate."""
    spotify = FakeSpotify(playlists=[], tracks={})
    yt = FakeYT({"Obscure": cand("yvX", "Obscure", "Nobody", 200)})
    # A collection with a stored signature, to prove invalidation on resolve.
    coll = repo.get_or_create_collection(name="Chill", kind="playlist", spotify_playlist_id="sp1")
    repo.update_collection(coll.id, spotify_snapshot_id="snap-1")
    repo.add_quarantine(
        source_side="spotify",
        source_uri="spotify:track:x",
        title="Obscure",
        artist="Nobody",
        album=None,
        reason="low_conf",
        best_candidate={"video_id": "yvX", "title": "Obscure", "artist": "Nobody"},
    )
    classifier = FakeClassifier({"Obscure": verdict})
    eng = Engine(spotify, yt, repo, Config(), sleep=NO_SLEEP, classifier=classifier)
    return eng, classifier, coll.id


def test_classify_confident_accept_resolves_and_invalidates(repo):
    eng, _, coll_id = _classify_world(
        repo, verdict=ClassifyVerdict(ClassifyDecision.ACCEPT, "yvX", 0.95)
    )
    stats = eng.classify_quarantine(apply=True)

    assert stats["applied"] == 1
    assert repo.count_unresolved_quarantine() == 0
    track = repo.get_track_by_spotify_uri("spotify:track:x")
    assert track is not None and track.yt_video_id == "yvX"
    assert track.match_method == "claude" and track.verified is True
    # Signatures were invalidated so the resolved track actually syncs next run.
    assert repo.get_collection(coll_id).spotify_snapshot_id is None


def test_classify_below_threshold_accept_is_not_applied(repo):
    eng, _, _ = _classify_world(repo, verdict=ClassifyVerdict(ClassifyDecision.ACCEPT, "yvX", 0.50))
    stats = eng.classify_quarantine(apply=True)
    assert stats["accepted"] == 1 and stats["applied"] == 0
    assert repo.count_unresolved_quarantine() == 1


def test_classify_reject_and_uncertain_keep_quarantine(repo):
    for verdict in (
        ClassifyVerdict(ClassifyDecision.REJECT, None, 0.9),
        ClassifyVerdict(ClassifyDecision.UNCERTAIN, None, 0.0),
    ):
        r = Repository(connect(":memory:"))
        eng, _, _ = _classify_world(r, verdict=verdict)
        eng.classify_quarantine(apply=True)
        assert r.count_unresolved_quarantine() == 1


def test_classify_dry_run_resolves_nothing(repo):
    eng, _, _ = _classify_world(repo, verdict=ClassifyVerdict(ClassifyDecision.ACCEPT, "yvX", 0.95))
    stats = eng.classify_quarantine(apply=False)
    assert stats["accepted"] == 1 and stats["applied"] == 0
    assert repo.count_unresolved_quarantine() == 1


def test_classify_stops_when_signaled(repo):
    eng, classifier, _ = _classify_world(
        repo, verdict=ClassifyVerdict(ClassifyDecision.ACCEPT, "yvX", 0.95)
    )
    eng._stop.set()  # as if the user quit before the loop began  # noqa: SLF001
    stats = eng.classify_quarantine(apply=True)
    assert stats["considered"] == 0
    assert classifier.calls == []  # never asked the model
    assert repo.count_unresolved_quarantine() == 1


def test_classify_without_classifier_is_a_noop(repo, config):
    spotify, yt = make_world(with_quarantine=True)
    eng = Engine(spotify, yt, repo, config, sleep=NO_SLEEP)  # no classifier
    eng.apply_plan(eng.build_plan())
    before = repo.count_unresolved_quarantine()
    stats = eng.classify_quarantine(apply=True)
    assert stats == {"considered": 0, "accepted": 0, "rejected": 0, "uncertain": 0, "applied": 0}
    assert repo.count_unresolved_quarantine() == before


def test_classify_judges_a_whole_quarantine_in_one_batch_call(repo):
    """Three quarantined rows are judged in a SINGLE classifier call (the win)."""
    spotify = FakeSpotify(playlists=[], tracks={})
    yt = FakeYT(
        {
            "Obscure": cand("yv1", "Obscure", "Nobody", 200),
            "Hidden": cand("yv2", "Hidden", "Someone", 210),
            "Buried": cand("yv3", "Buried", "Anon", 220),
        }
    )
    for uri, title, vid in [
        ("spotify:track:1", "Obscure", "yv1"),
        ("spotify:track:2", "Hidden", "yv2"),
        ("spotify:track:3", "Buried", "yv3"),
    ]:
        repo.add_quarantine(
            source_side="spotify",
            source_uri=uri,
            title=title,
            artist="Nobody",
            album=None,
            reason="low_conf",
            best_candidate={"video_id": vid},
        )
    classifier = FakeClassifier(
        {
            "Obscure": ClassifyVerdict(ClassifyDecision.ACCEPT, "yv1", 0.95),
            "Hidden": ClassifyVerdict(ClassifyDecision.REJECT, None, 0.8),
            "Buried": ClassifyVerdict(ClassifyDecision.ACCEPT, "yv3", 0.9),
        }
    )
    eng = Engine(spotify, yt, repo, Config(), sleep=NO_SLEEP, classifier=classifier)

    stats = eng.classify_quarantine(apply=True)

    assert classifier.batch_calls == 1  # ONE claude -p for all three tracks
    assert stats["considered"] == 3 and stats["applied"] == 2
    assert repo.count_unresolved_quarantine() == 1  # the rejected one stays


def test_classify_splits_into_chunks_by_batch_size(repo):
    """A quarantine larger than batch_size splits into multiple calls."""
    spotify = FakeSpotify(playlists=[], tracks={})
    yt = FakeYT({f"T{i}": cand(f"v{i}", f"T{i}", "A", 200) for i in range(5)})
    for i in range(5):
        repo.add_quarantine(
            source_side="spotify",
            source_uri=f"spotify:track:{i}",
            title=f"T{i}",
            artist="A",
            album=None,
            reason="low_conf",
        )
    classifier = FakeClassifier({})  # all UNCERTAIN — nothing resolved
    cfg = Config()
    cfg.classify.batch_size = 2
    eng = Engine(spotify, yt, repo, cfg, sleep=NO_SLEEP, classifier=classifier)

    stats = eng.classify_quarantine(apply=True)

    assert classifier.batch_calls == 3  # 2 + 2 + 1
    assert stats["considered"] == 5
    assert repo.count_unresolved_quarantine() == 5


def test_classify_emits_progress_narration(repo):
    """The judge narrates each blocking window so the TUI never looks frozen."""
    spotify = FakeSpotify(playlists=[], tracks={})
    yt = FakeYT({f"T{i}": cand(f"v{i}", f"T{i}", "A", 200) for i in range(3)})
    for i in range(3):
        repo.add_quarantine(
            source_side="spotify",
            source_uri=f"spotify:track:{i}",
            title=f"T{i}",
            artist="A",
            album=None,
            reason="low_conf",
        )
    classifier = FakeClassifier({})
    cfg = Config()
    cfg.classify.batch_size = 2  # -> 2 batches over 3 tracks
    eng = Engine(spotify, yt, repo, cfg, sleep=NO_SLEEP, classifier=classifier)

    events: list = []
    eng.classify_quarantine(events.append, apply=True)

    starts = [e for e in events if isinstance(e, PhaseStarted) and e.phase == "classify"]
    assert starts and starts[0].total == 3
    logs = [e.message for e in events if isinstance(e, LogLine)]
    assert any(m.startswith("Fetching YouTube candidates") for m in logs)
    assert any(m.startswith("Asking Claude") for m in logs)
    assert any("batch 1/2" in m for m in logs) and any("batch 2/2" in m for m in logs)
    # One progress tick per track, and the phase is closed out.
    assert sum(e.amount for e in events if isinstance(e, Advanced)) == 3
    assert any(isinstance(e, PhaseFinished) and e.phase == "classify" for e in events)
