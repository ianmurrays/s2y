"""Reverse (YouTube Music -> Spotify) engine tests with in-memory fakes.

Mirrors tests/test_engine.py for the opposite direction, plus the reverse-specific
footguns the design audit flagged (reverse_map vs track_map clobber, present_spotify
membership, source-side isolation) and a forward->reverse->forward round-trip that
proves no echo / duplicate adds.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from s2y.config import Config
from s2y.core.engine import Engine
from s2y.core.models import FORWARD, REVERSE, OpKind, RemotePlaylist, SongInfo, YtCandidate
from s2y.core.planner import signature
from s2y.store import connect
from s2y.store.repository import Repository

NO_SLEEP = lambda _: None  # noqa: E731


# --------------------------------------------------------------------------- #
# A shared "song universe" both services know about (drives search/matching),
# plus a per-service mutable library (liked set + named playlists).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UTrack:
    title: str
    artist: str
    spotify_uri: str
    yt_video_id: str
    duration_s: int


UNIVERSE = (
    UTrack("Blinding Lights", "The Weeknd", "spotify:track:1", "yv1", 200),
    UTrack("Levitating", "Dua Lipa", "spotify:track:2", "yv2", 203),
    UTrack("As It Was", "Harry Styles", "spotify:track:3", "yv3", 167),
)


class FakeService:
    """A fake that plays BOTH source and target for one service side.

    ``side`` is "spotify" or "yt"; the same physical song has a different id on each
    side (from UNIVERSE). Search resolves a query to the matching id *on this side*;
    the library (liked + playlists) mutates as writes are applied.
    """

    def __init__(self, side, *, liked=(), playlists=None, extra_liked=(), fail_ids=()):
        self.side = side
        self._liked = set(liked)
        self._playlists: dict[str, dict] = {}
        self._pl_seq = 0
        for name, ids in (playlists or {}).items():
            self._pl_seq += 1
            self._playlists[name] = {"id": f"{side}-pl{self._pl_seq}", "uris": set(ids)}
        self._extra_liked = list(extra_liked)  # off-universe source tracks (-> quarantine)
        self._fail = set(fail_ids)
        self.search_calls = 0
        self.created: list[str] = []
        self.added: list[tuple[str, list[str]]] = []
        self.song_likes: list[str] = []
        self.album_likes: list[tuple[str, str]] = []

    def _id(self, u: UTrack) -> str:
        return u.spotify_uri if self.side == "spotify" else u.yt_video_id

    def _by_id(self, the_id):
        return next((u for u in UNIVERSE if self._id(u) == the_id), None)

    def _song(self, u: UTrack) -> SongInfo:
        kw = (
            {"spotify_uri": u.spotify_uri}
            if self.side == "spotify"
            else {"yt_video_id": u.yt_video_id}
        )
        return SongInfo(u.title, u.artist, duration_ms=u.duration_s * 1000, **kw)

    def _playlist_by_id(self, pid):
        return next((pl for pl in self._playlists.values() if pl["id"] == pid), None)

    def verify(self):
        return self.side

    # ---- source role ----
    def list_playlists(self):
        out = []
        for name, pl in self._playlists.items():
            # Spotify exposes a free snapshot token; YouTube does not (None).
            snap = signature(sorted(pl["uris"])) if self.side == "spotify" else None
            out.append(
                RemotePlaylist(id=pl["id"], name=name, count=len(pl["uris"]), snapshot_id=snap)
            )
        return out

    def playlist_tracks(self, pid):
        pl = self._playlist_by_id(pid)
        if pl is None:
            return []
        return [self._song(u) for u in (self._by_id(i) for i in sorted(pl["uris"])) if u]

    def liked_songs(self):
        live = [self._song(u) for u in (self._by_id(i) for i in sorted(self._liked)) if u]
        return live + list(self._extra_liked)

    def liked_albums(self):
        return []

    # ---- target role ----
    def present_playlist_ids(self, pid):
        pl = self._playlist_by_id(pid)
        return set(pl["uris"]) if pl else set()

    def present_liked_ids(self):
        return set(self._liked)

    def present_album_keys(self):
        return set()

    def search_songs(self, query, *, limit=6):
        self.search_calls += 1
        ql = query.lower()
        return [
            YtCandidate(
                video_id=self._id(u), title=u.title, artist=u.artist, duration_s=u.duration_s
            )
            for u in UNIVERSE
            if u.title.lower() in ql
        ]

    def search_videos(self, query, *, limit=4):
        return []

    def album_candidates(self, artist, album, *, limit_albums=3):
        return []

    def get_or_create_playlist(self, name):
        if name not in self._playlists:
            self._pl_seq += 1
            self._playlists[name] = {"id": f"{self.side}-pl{self._pl_seq}", "uris": set()}
            self.created.append(name)
        return self._playlists[name]["id"]

    def add_playlist_items(self, pid, ids):
        for i in ids:
            if i in self._fail:
                raise RuntimeError(f"add failed for {i}")
        pl = self._playlist_by_id(pid)
        if pl is None:
            raise RuntimeError(f"no such playlist {pid}")
        self.added.append((pid, list(ids)))
        pl["uris"].update(ids)
        set_id = (lambda i: f"set-{i}") if self.side == "yt" else (lambda i: None)
        return {i: set_id(i) for i in ids}

    def like_song(self, the_id):
        if the_id in self._fail:
            raise RuntimeError(f"like failed for {the_id}")
        self.song_likes.append(the_id)
        self._liked.add(the_id)

    def like_album(self, artist, album):
        self.album_likes.append((artist, album))
        return f"{self.side}-album"


@pytest.fixture
def repo():
    return Repository(connect(":memory:"))


@pytest.fixture
def config():
    return Config()


def reverse_world(*, with_quarantine=False, fail=None):
    """YT (source) has a 'Chill' playlist + a liked song; Spotify (target) is empty."""
    yt = FakeService(
        "yt",
        liked={"yv3"},  # "As It Was" liked on YouTube
        playlists={"Chill": ["yv1", "yv2"]},  # Blinding Lights + Levitating
        extra_liked=(
            [SongInfo("Obscure Bootleg", "Nobody", duration_ms=999000, yt_video_id="yvX")]
            if with_quarantine
            else ()
        ),
    )
    sp = FakeService("spotify", fail_ids=fail or ())
    return yt, sp


def rev_engine(sp, yt, repo, config):
    # Engine(spotify_adapter, ytmusic_adapter, ...); REVERSE makes YT the source.
    return Engine(sp, yt, repo, config, direction=REVERSE, sleep=NO_SLEEP)


# --------------------------------------------------------------------------- #
# Ported forward tests, reversed
# --------------------------------------------------------------------------- #
def test_reverse_build_plan_produces_expected_ops(repo, config):
    yt, sp = reverse_world()
    plan = rev_engine(sp, yt, repo, config).build_plan()
    kinds = [op.kind for op in plan.ops]
    assert kinds.count(OpKind.CREATE_PLAYLIST) == 1
    assert kinds.count(OpKind.ADD_PLAYLIST_ITEM) == 2
    assert kinds.count(OpKind.LIKE_SONG) == 1
    assert plan.quarantine == []
    assert len(plan.matches) == 3  # 2 playlist + 1 liked


def test_reverse_apply_executes_writes(repo, config):
    yt, sp = reverse_world()
    eng = rev_engine(sp, yt, repo, config)
    stats = eng.apply_plan(eng.build_plan())
    assert sp.created == ["Chill"]
    assert {u for _, uris in sp.added for u in uris} == {"spotify:track:1", "spotify:track:2"}
    assert sp.song_likes == ["spotify:track:3"]
    assert stats["applied"] == 4 and stats["failed"] == 0
    # The reverse identity cache is populated (keyed on the YT video id).
    assert repo.get_reverse_by_video_id("yv1").spotify_uri == "spotify:track:1"


def test_reverse_rerun_idempotent_via_content_fingerprint(repo, config):
    yt, sp = reverse_world()
    rev_engine(sp, yt, repo, config).apply_plan(rev_engine(sp, yt, repo, config).build_plan())
    plan2 = rev_engine(sp, yt, repo, config).build_plan()
    assert plan2.is_empty and plan2.ops == []


def test_reverse_cached_matches_avoid_research(repo, config):
    yt, sp = reverse_world()
    rev_engine(sp, yt, repo, config).apply_plan(rev_engine(sp, yt, repo, config).build_plan())
    sp.search_calls = 0
    # Force the playlist to be re-examined (a new track), but the old ones resolve
    # from reverse_map without re-searching Spotify.
    yt._playlists["Chill"]["uris"].add("yv3")
    rev_engine(sp, yt, repo, config).build_plan()
    # yv1/yv2 cached; only the newly-added yv3 triggers a search (it was also liked,
    # so it may already be cached from the liked pass — assert we didn't re-search the
    # two known playlist tracks).
    assert sp.search_calls <= 1


def test_reverse_quarantine_persisted_with_yt_source_side(repo, config):
    yt, sp = reverse_world(with_quarantine=True)
    eng = rev_engine(sp, yt, repo, config)
    plan = eng.build_plan()
    assert len(plan.quarantine) == 1
    eng.apply_plan(plan)
    assert repo.count_unresolved_quarantine() == 1
    q = repo.get_quarantine_by_source("yt", "yvX")
    assert q is not None and q["source_side"] == "yt"

    # Re-examined next run, but the pending track is not re-matched.
    yt._extra_liked.append(SongInfo("Another", "X", yt_video_id="yvY"))  # force re-examine
    plan2 = rev_engine(sp, yt, repo, config).build_plan()
    assert all(q2.source.yt_video_id != "yvX" for q2 in plan2.quarantine)


def test_reverse_failed_op_withholds_fingerprint(repo, config):
    yt, sp = reverse_world(fail={"spotify:track:2"})  # adding Levitating to Spotify fails
    eng = rev_engine(sp, yt, repo, config)
    stats = eng.apply_plan(eng.build_plan())
    assert stats["failed"] == 1
    # The playlist signature (yt_content_fingerprint) must be withheld for retry.
    coll = repo.get_collection_by_yt_id("yt-pl1")
    assert coll is None or coll.yt_content_fingerprint is None

    plan2 = rev_engine(sp, yt, repo, config).build_plan()
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert "spotify:track:2" in add_ids  # retried
    assert "spotify:track:1" not in add_ids  # already present, not re-added


def test_reverse_membership_recorded_present_spotify(repo, config):
    yt, sp = reverse_world()
    eng = rev_engine(sp, yt, repo, config)
    eng.apply_plan(eng.build_plan())
    coll = repo.get_collection_by_yt_id("yt-pl1")
    assert repo.present_target_ids("spotify", coll.id) == {"spotify:track:1", "spotify:track:2"}
    track = repo.get_track_by_spotify_uri("spotify:track:1")
    m = repo.get_membership(coll.id, track.id)
    assert m["present_spotify"] == 1


def test_reverse_resume_dedups_via_membership_when_live_read_empty(repo, config):
    yt, sp = reverse_world(fail={"spotify:track:2"})
    rev_engine(sp, yt, repo, config).apply_plan(rev_engine(sp, yt, repo, config).build_plan())
    # A live Spotify read that knows nothing (signed back in, cache cold).
    sp.present_playlist_ids = lambda pid: set()
    plan2 = rev_engine(sp, yt, repo, config).build_plan()
    add_ids = [op.video_id for op in plan2.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert "spotify:track:1" not in add_ids  # known-landed via membership
    assert "spotify:track:2" in add_ids  # genuinely missing — retried


# --------------------------------------------------------------------------- #
# Reverse-specific audit footguns
# --------------------------------------------------------------------------- #
def test_reverse_cache_uses_reverse_map_not_track_map_clobber(repo, config):
    # A pre-existing FORWARD mapping: spotify:track:1 -> a DIFFERENT video.
    repo.upsert_track(
        spotify_uri="spotify:track:1",
        yt_video_id="yvFORWARD",
        canon_title="blinding lights",
        canon_artist="the weeknd",
        match_method="meta",
        match_confidence=0.9,
    )
    yt = FakeService("yt", liked={"yv1"})  # As Blinding Lights (yv1) liked on YT
    sp = FakeService("spotify")
    eng = rev_engine(sp, yt, repo, config)
    eng.apply_plan(eng.build_plan())
    # reverse_map records the reverse pair; the forward track_map row is NOT clobbered.
    assert repo.get_reverse_by_video_id("yv1").spotify_uri == "spotify:track:1"
    assert repo.get_track_by_spotify_uri("spotify:track:1").yt_video_id == "yvFORWARD"


def test_reverse_playlist_name_collision_does_not_crash(repo, config):
    # Spotify already has a forward-mapped playlist "Chill" (id spotify-pl1).
    sp = FakeService("spotify", playlists={"Chill": []})
    sp_chill_id = sp._playlists["Chill"]["id"]
    fwd = repo.get_or_create_collection(
        name="Chill", kind="playlist", spotify_playlist_id=sp_chill_id
    )
    repo.update_collection(fwd.id, yt_playlist_id="yt-forwardchill")
    # An INDEPENDENT YouTube playlist also named "Chill" (a *different* id) is the
    # reverse source. get_or_create_playlist("Chill") resolves by name to spotify-pl1,
    # which the forward row already owns — the apply must not crash on UNIQUE.
    yt = FakeService("yt", playlists={"Chill": ["yv1"]})
    eng = rev_engine(sp, yt, repo, config)
    stats = eng.apply_plan(eng.build_plan())
    assert stats["failed"] == 0  # no UNIQUE(spotify_playlist_id) crash
    assert "spotify:track:1" in sp._playlists["Chill"]["uris"]  # the add still landed
    # The reverse collection (anchored on the YT id) is left WITHOUT claiming the
    # already-owned Spotify id; the forward mapping is untouched.
    rev = repo.get_collection_by_yt_id(yt._playlists["Chill"]["id"])
    assert rev is not None and rev.id != fwd.id and rev.spotify_playlist_id is None
    assert repo.get_collection(fwd.id).spotify_playlist_id == sp_chill_id


def test_reverse_quarantine_side_isolated(repo):
    # The same source_uri can be quarantined on each side independently.
    repo.add_quarantine(
        source_side="spotify", source_uri="u", title="t", artist="a", album=None, reason="x"
    )
    repo.add_quarantine(
        source_side="yt", source_uri="u", title="t", artist="a", album=None, reason="x"
    )
    assert repo.count_unresolved_quarantine() == 2


def test_reverse_playlist_anchored_by_yt_id(repo, config):
    yt, sp = reverse_world()
    rev_engine(sp, yt, repo, config).apply_plan(rev_engine(sp, yt, repo, config).build_plan())
    coll = repo.get_collection_by_yt_id("yt-pl1")
    assert coll is not None
    assert coll.spotify_playlist_id is not None  # filled with the created Spotify playlist id
    assert coll.spotify_playlist_id == sp._playlists["Chill"]["id"]


# --------------------------------------------------------------------------- #
# The Phase-2 risk: round-trip must not echo / duplicate
# --------------------------------------------------------------------------- #
def test_forward_then_reverse_then_forward_no_duplicates(repo, config):
    # Spotify (source for forward) has one liked song and a one-track playlist.
    sp = FakeService("spotify", liked={"spotify:track:1"}, playlists={"Mix": ["spotify:track:2"]})
    yt = FakeService("yt")  # empty target

    # 1) Forward: Spotify -> YouTube.
    Engine(sp, yt, repo, config, direction=FORWARD, sleep=NO_SLEEP).apply_plan(
        Engine(sp, yt, repo, config, direction=FORWARD, sleep=NO_SLEEP).build_plan()
    )
    assert yt._liked == {"yv1"}
    assert yt._playlists["Mix"]["uris"] == {"yv2"}

    # 2) Reverse: YouTube -> Spotify. Everything round-tripped is already on Spotify,
    #    so it must produce ZERO add/like ops (live dedup) — no echo.
    rev_plan = Engine(sp, yt, repo, config, direction=REVERSE, sleep=NO_SLEEP).build_plan()
    assert [
        op for op in rev_plan.ops if op.kind in (OpKind.ADD_PLAYLIST_ITEM, OpKind.LIKE_SONG)
    ] == []
    Engine(sp, yt, repo, config, direction=REVERSE, sleep=NO_SLEEP).apply_plan(rev_plan)
    assert sp._liked == {"spotify:track:1"}  # no growth
    assert sp._playlists["Mix"]["uris"] == {"spotify:track:2"}  # no growth
    # reverse_map is now warm for the next reverse run.
    assert repo.get_reverse_by_video_id("yv1").spotify_uri == "spotify:track:1"

    # 3) Forward again: nothing new to do (no ping-pong back to YouTube).
    plan3 = Engine(sp, yt, repo, config, direction=FORWARD, sleep=NO_SLEEP).build_plan()
    assert plan3.ops == []
