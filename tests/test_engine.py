"""End-to-end engine tests with in-memory fakes + a :memory: store."""

from __future__ import annotations

from collections import defaultdict

import pytest

from s2y.config import Config
from s2y.core.engine import Engine
from s2y.core.matching import normalize_artist, normalize_title
from s2y.core.models import (
    CollectionKind,
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
    t1 = SongInfo("Blinding Lights", "The Weeknd", "After Hours",
                  duration_ms=200000, spotify_uri="spotify:track:1")
    t2 = SongInfo("Levitating", "Dua Lipa", "Future Nostalgia",
                  duration_ms=203000, spotify_uri="spotify:track:2")
    liked = SongInfo("As It Was", "Harry Styles", "Harry's House",
                     duration_ms=167000, spotify_uri="spotify:track:3")
    tracks = [t1, t2]
    if with_quarantine:
        tracks.append(SongInfo("Totally Obscure Bootleg", "Nobody At All",
                               duration_ms=999000, spotify_uri="spotify:track:x"))
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
        spotify_uri=q["source_uri"], yt_video_id="resolved_vid",
        canon_title="x", canon_artist="y", match_method="manual",
        match_confidence=1.0, verified=True,
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
