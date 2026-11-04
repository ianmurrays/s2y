"""Reverse (YouTube Music -> Spotify) engine tests with in-memory fakes.

Mirrors tests/test_engine.py for the opposite direction, plus the reverse-specific
footguns the design audit flagged (reverse_map vs track_map clobber, present_spotify
membership, source-side isolation) and a forward->reverse->forward round-trip that
proves no echo / duplicate adds.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import pytest

from s2y.config import Config
from s2y.core.engine import Engine
from s2y.core.events import DedupDegraded, TrackMatched
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
    def find_playlist_id(self, name):
        pl = self._playlists.get(name)
        return pl["id"] if pl else None

    def _candidates_for(self, ids):
        out = []
        for i in sorted(ids):
            u = self._by_id(i)
            if u:
                out.append(
                    YtCandidate(video_id=i, title=u.title, artist=u.artist, duration_s=u.duration_s)
                )
        return out

    def present_playlist_candidates(self, pid):
        pl = self._playlist_by_id(pid)
        return self._candidates_for(pl["uris"]) if pl else []

    def present_liked_candidates(self):
        return self._candidates_for(self._liked)

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
    sp.present_playlist_candidates = lambda pid: []
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


# --------------------------------------------------------------------------- #
# Diagnostics: the logging that explains "why is it re-proposing everything"
# --------------------------------------------------------------------------- #
def test_reverse_dedup_summary_logged_when_unmapped(repo, config, caplog):
    # A fresh reverse plan of an unmapped YT playlist: present is empty, so every track
    # looks new. The INFO diagnostics must say so plainly (this is the line that turns
    # "it re-adds everything" into a diagnosis).
    yt, sp = reverse_world()
    with caplog.at_level(logging.INFO, logger="s2y.core.planner"):
        rev_engine(sp, yt, repo, config).build_plan()
    text = caplog.text
    # Source playlist name is logged verbatim (repr) so an emoji/whitespace variant shows.
    assert "Source playlist name='Chill'" in text
    # The dedup-base summary fires and shows an empty present set / unmapped collection.
    assert "dedup base" in text
    assert "collection_row=none" in text
    assert "target_pl_id=NULL" in text
    assert "present=0" in text


def test_reverse_per_track_verdict_debug(repo, config, caplog):
    # After a partial apply (track:2 failed), a re-run must show the per-track verdicts:
    # track:1 is [present] (known-landed via membership), track:2 is [ADD] (still missing).
    yt, sp = reverse_world(fail={"spotify:track:2"})
    rev_engine(sp, yt, repo, config).apply_plan(rev_engine(sp, yt, repo, config).build_plan())
    sp.present_playlist_candidates = lambda pid: []  # live read cold; membership covers track:1
    with caplog.at_level(logging.DEBUG, logger="s2y.core.planner"):
        rev_engine(sp, yt, repo, config).build_plan()
    text = caplog.text
    assert "-> spotify:track:1 [present]" in text
    assert "-> spotify:track:2 [ADD]" in text


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
    # Recognized from the live read, so it took no search and wrote no reverse mapping.
    assert sp.search_calls == 0
    assert repo.get_reverse_by_video_id("yv1") is None

    # 3) Forward again: nothing new to do (no ping-pong back to YouTube).
    plan3 = Engine(sp, yt, repo, config, direction=FORWARD, sleep=NO_SLEEP).build_plan()
    assert plan3.ops == []


# --------------------------------------------------------------------------- #
# Content-based dedup: a song already present under a DIFFERENT release id
# --------------------------------------------------------------------------- #
class ReleaseFake:
    """A fake (both roles, one object) that controls search results and present tracks
    SEPARATELY — so the same song can sit in the target under a different id than search
    resolves to (the real reverse-sync duplicate trap). Used for both directions: the
    engine routes ids by :class:`Direction`, the fake just returns SongInfo/YtCandidate.
    """

    def __init__(self, *, pl_id, target_pl_id, sources, present, search, snapshot=None):
        self._pl_id = pl_id
        self._target_pl_id = target_pl_id
        self._sources = sources  # [SongInfo, ...] in the source "Auto" playlist
        self._present = list(present)  # [YtCandidate, ...] already in the target playlist
        self._search = search  # title-substring -> YtCandidate (the matcher's pick)
        self._snapshot = snapshot
        self.search_calls = 0
        self.added: list[tuple[str, list[str]]] = []
        self.likes: list[str] = []

    # ---- source role ----
    def verify(self):
        return "x"

    def list_playlists(self):
        return [
            RemotePlaylist(
                id=self._pl_id, name="Auto", count=len(self._sources), snapshot_id=self._snapshot
            )
        ]

    def playlist_tracks(self, pid):
        return list(self._sources)

    def liked_songs(self):
        return []

    def liked_albums(self):
        return []

    # ---- target role ----
    def find_playlist_id(self, name):
        return self._target_pl_id

    def present_playlist_candidates(self, pid):
        return list(self._present)

    def present_liked_candidates(self):
        return []

    def present_album_keys(self):
        return set()

    def search_songs(self, query, *, limit=6):
        self.search_calls += 1
        return [c for key, c in self._search.items() if key.lower() in query.lower()]

    def search_videos(self, query, *, limit=4):
        return []

    def album_candidates(self, artist, album, *, limit_albums=3):
        return []

    def get_or_create_playlist(self, name):
        return self._target_pl_id

    def add_playlist_items(self, pid, ids):
        self.added.append((pid, list(ids)))
        return {i: None for i in ids}

    def like_song(self, vid):
        self.likes.append(vid)

    def like_album(self, artist, album):
        return None


def test_reverse_dedups_song_present_under_different_uri(repo, config):
    # YT 'Auto' has "Blinding Lights"; the Spotify 'Auto' already holds it under a
    # DIFFERENT uri (and a looser artist credit). Search would resolve it to yet another
    # uri. The planner must recognize it as present and propose NO add.
    fake = ReleaseFake(
        pl_id="yt-auto",
        target_pl_id="sp-auto",
        sources=[SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000, yt_video_id="yv1")],
        present=[YtCandidate("spotify:track:ALT", "Blinding Lights", "Weeknd", duration_s=200)],
        search={
            "Blinding Lights": YtCandidate(
                "spotify:track:NEW", "Blinding Lights", "The Weeknd", duration_s=200
            )
        },
    )
    coll = repo.get_or_create_collection(name="Auto", kind="playlist", yt_playlist_id="yt-auto")
    repo.update_collection(coll.id, spotify_playlist_id="sp-auto")

    eng = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP)
    events: list = []
    plan = eng.build_plan(events.append, persist_matches=True)
    assert plan.ops == []
    assert plan.present == 1
    assert fake.search_calls == 0  # recognized before any search
    shown = [e for e in events if isinstance(e, TrackMatched)]
    assert [e.present for e in shown] == [True]  # the UI labels it, not as an add

    eng.apply_plan(plan)
    assert fake.added == []  # nothing was added to Spotify
    # Per-collection fact: the global identity cache and the ledger stay untouched.
    assert repo.get_reverse_by_video_id("yv1") is None
    assert repo.get_track_by_spotify_uri("spotify:track:ALT") is None

    # Re-run after the source changed: recognized again, still no search and no ops.
    fake._snapshot = None
    plan2 = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP).build_plan()
    assert plan2.ops == []
    assert fake.search_calls == 0


def test_reverse_still_adds_when_only_a_different_recording_is_present(repo, config):
    # Same title/artist present but a very different duration (a live cut) — NOT the same
    # recording, so the studio version must still be proposed as an add.
    fake = ReleaseFake(
        pl_id="yt-auto",
        target_pl_id="sp-auto",
        sources=[SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000, yt_video_id="yv1")],
        present=[
            YtCandidate("spotify:track:LIVE", "Blinding Lights", "The Weeknd", duration_s=320)
        ],
        search={
            "Blinding Lights": YtCandidate(
                "spotify:track:NEW", "Blinding Lights", "The Weeknd", duration_s=200
            )
        },
    )
    coll = repo.get_or_create_collection(name="Auto", kind="playlist", yt_playlist_id="yt-auto")
    repo.update_collection(coll.id, spotify_playlist_id="sp-auto")

    plan = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP).build_plan()
    add_ids = [op.video_id for op in plan.ops if op.kind is OpKind.ADD_PLAYLIST_ITEM]
    assert add_ids == ["spotify:track:NEW"]
    assert plan.present == 0


def test_forward_dedups_song_present_under_different_video_id(repo, config):
    # The same fix applies forward (Spotify -> YouTube): a song already on YouTube under
    # a different videoId than search returns must not be re-proposed.
    fake = ReleaseFake(
        pl_id="sp-auto",
        target_pl_id="yt-auto",
        sources=[SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000, spotify_uri="su1")],
        present=[YtCandidate("ytALT", "Blinding Lights", "The Weeknd", duration_s=200)],
        search={
            "Blinding Lights": YtCandidate("ytNEW", "Blinding Lights", "The Weeknd", duration_s=200)
        },
    )
    coll = repo.get_or_create_collection(
        name="Auto", kind="playlist", spotify_playlist_id="sp-auto"
    )
    repo.update_collection(coll.id, yt_playlist_id="yt-auto")

    eng = Engine(fake, fake, repo, config, direction=FORWARD, sleep=NO_SLEEP)
    plan = eng.build_plan(persist_matches=True)
    assert plan.ops == []
    assert plan.present == 1
    eng.apply_plan(plan)
    assert fake.added == []
    assert repo.get_track_by_spotify_uri("su1") is None  # nothing cached at either id


def _song_release_fake(*, present, **kw):
    return ReleaseFake(
        pl_id="yt-auto",
        target_pl_id="sp-auto",
        sources=[SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000, yt_video_id="yv1")],
        present=present,
        search={
            "Blinding Lights": YtCandidate(
                "spotify:track:NEW", "Blinding Lights", "The Weeknd", duration_s=200
            )
        },
        **kw,
    )


_ALT = YtCandidate("spotify:track:ALT", "Blinding Lights", "The Weeknd", duration_s=200)


def test_first_sync_dedups_against_same_named_target_playlist(repo, config):
    # No collection row and no stored target id: apply would resolve the playlist by
    # name and add into it, so the plan has to read that same playlist.
    fake = _song_release_fake(present=[_ALT])
    eng = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP)
    plan = eng.build_plan()
    assert plan.ops == []  # no add, and no CREATE for a playlist that exists
    eng.apply_plan(plan)
    assert fake.added == []


def test_first_sync_with_same_named_playlist_adds_without_create(repo, config):
    fake = _song_release_fake(present=[])
    plan = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP).build_plan()
    assert [op.kind for op in plan.ops] == [OpKind.ADD_PLAYLIST_ITEM]


def test_present_link_leaves_verified_mapping_untouched(repo, config):
    fake = ReleaseFake(
        pl_id="sp-auto",
        target_pl_id="yt-auto",
        sources=[SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000, spotify_uri="su1")],
        present=[
            YtCandidate("ytOTHER", "Blinding Lights (Radio Edit)", "The Weeknd", duration_s=200)
        ],
        search={},
    )
    repo.upsert_track(
        spotify_uri="su1",
        yt_video_id="ytHUMAN",
        canon_title="blinding lights",
        canon_artist="the weeknd",
        match_method="manual",
        match_confidence=1.0,
        verified=True,
    )
    eng = Engine(fake, fake, repo, config, direction=FORWARD, sleep=NO_SLEEP)
    eng.apply_plan(eng.build_plan(persist_matches=True))
    assert fake.added == []
    row = repo.get_track_by_spotify_uri("su1")
    assert (row.yt_video_id, row.match_method, row.verified) == ("ytHUMAN", "manual", True)


def test_present_tracks_keep_each_playlists_ledger_separate(repo, config):
    # One Spotify track, present as ALT1 in P1 and ALT2 in P2: neither playlist's ledger
    # may claim an id it doesn't hold, and the identity row stays unwritten.
    src = SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000, spotify_uri="su1")

    class TwoPlaylists(ReleaseFake):
        def list_playlists(self):
            return [RemotePlaylist(id="sp-p1", name="P1"), RemotePlaylist(id="sp-p2", name="P2")]

        def find_playlist_id(self, name):
            return {"P1": "yt-p1", "P2": "yt-p2"}[name]

        def present_playlist_candidates(self, pid):
            vid = {"yt-p1": "ALT1", "yt-p2": "ALT2"}[pid]
            return [YtCandidate(vid, "Blinding Lights", "The Weeknd", duration_s=200)]

    fake = TwoPlaylists(pl_id="", target_pl_id="", sources=[src], present=[], search={})
    eng = Engine(fake, fake, repo, config, direction=FORWARD, sleep=NO_SLEEP)
    plan = eng.build_plan(persist_matches=True)
    eng.apply_plan(plan)

    assert plan.ops == []
    assert plan.present == 2
    assert repo.get_track_by_spotify_uri("su1") is None
    for sp_id in ("sp-p1", "sp-p2"):
        assert repo.present_target_ids("yt", repo.get_collection_by_spotify_id(sp_id).id) == set()


def test_failed_live_read_proposes_no_blind_add(repo, config):
    # The song sits in the target under ALT with no ledger row; a failed read can't see
    # that, so adding the search pick would create a real duplicate.
    fake = _song_release_fake(present=[_ALT])
    coll = repo.get_or_create_collection(name="Auto", kind="playlist", yt_playlist_id="yt-auto")
    repo.update_collection(coll.id, spotify_playlist_id="sp-auto")

    def boom(pid):
        raise RuntimeError("503")

    fake.present_playlist_candidates = boom
    events: list = []
    plan = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP).build_plan(
        events.append
    )
    assert plan.ops == []
    assert plan.degraded == ["Auto"]
    assert "Auto" not in [c.name for c in plan.collections]  # signature stays withheld
    assert fake.search_calls == 0
    assert any(isinstance(e, DedupDegraded) for e in events)


def test_failed_name_lookup_proposes_no_blind_add(repo, config):
    fake = _song_release_fake(present=[_ALT])

    def boom(name):
        raise RuntimeError("503")

    fake.find_playlist_id = boom
    plan = Engine(fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP).build_plan()
    assert plan.ops == []
    assert plan.degraded == ["Auto"]


def test_stop_interrupts_the_track_loop(repo, config):
    fake = _song_release_fake(present=[_ALT])
    stop = threading.Event()

    def on_event(event):
        if isinstance(event, TrackMatched):
            stop.set()

    fake._sources = fake._sources * 3
    plan = Engine(
        fake, fake, repo, config, direction=REVERSE, sleep=NO_SLEEP, stop=stop
    ).build_plan(on_event)
    assert plan.collections == []  # abandoned after the first track
