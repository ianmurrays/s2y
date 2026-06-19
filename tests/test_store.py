"""Store-layer tests against an in-memory SQLite database."""

from __future__ import annotations

import itertools

import pytest

from s2y.store import connect
from s2y.store.repository import Repository


@pytest.fixture
def repo() -> Repository:
    conn = connect(":memory:")
    # Deterministic, monotonically increasing timestamps for assertions.
    counter = itertools.count(1)
    return Repository(conn, clock=lambda: f"2026-01-01T00:00:{next(counter):02d}+00:00")


def test_resolve_quarantine_match_helper(repo: Repository):
    """The shared accept path: verified track_map row + resolved quarantine +
    invalidated signatures, all in one transaction (used by the TUI and LLM judge)."""
    from s2y.core.resolve import resolve_quarantine_match

    coll = repo.get_or_create_collection(
        name="Chill", kind="playlist", spotify_playlist_id="sp1"
    )
    repo.update_collection(coll.id, spotify_snapshot_id="snap-1")
    qid = repo.add_quarantine(
        source_side="spotify", source_uri="spotify:track:7", title="Wéird Tïtle",
        artist="Nobody", album=None, reason="low_conf",
        best_candidate={"video_id": "vz"},
    )

    track = resolve_quarantine_match(
        repo, quarantine_id=qid, spotify_uri="spotify:track:7", yt_video_id="vz",
        title="Wéird Tïtle", artist="Nobody", match_method="claude",
        match_confidence=0.9,
    )

    got = repo.get_track_by_spotify_uri("spotify:track:7")
    assert got is not None and got.yt_video_id == "vz"
    assert got.verified is True and got.match_method == "claude"
    assert got.canon_title == "weird title"  # accents folded + casefolded
    q = repo.get_quarantine_by_source("spotify", "spotify:track:7")
    assert q["resolved"] == 1 and q["resolution_track_map_id"] == track.id
    assert repo.count_unresolved_quarantine() == 0
    # Signatures invalidated so the resolved track is re-examined next run.
    assert repo.get_collection(coll.id).spotify_snapshot_id is None


# --------------------------------------------------------------------------- #
# track_map
# --------------------------------------------------------------------------- #
def test_upsert_track_insert_and_fetch(repo: Repository):
    row = repo.upsert_track(
        spotify_uri="spotify:track:1",
        yt_video_id="vid1",
        canon_title="song",
        canon_artist="artist",
        isrc="US1234567890",
        duration_ms=210000,
        match_method="isrc",
        match_confidence=0.95,
        verified=False,
    )
    assert row.id > 0
    fetched = repo.get_track_by_spotify_uri("spotify:track:1")
    assert fetched is not None
    assert fetched.yt_video_id == "vid1"
    assert fetched.isrc == "US1234567890"
    assert fetched.verified is False


def test_upsert_track_is_idempotent_on_uri(repo: Repository):
    a = repo.upsert_track(
        spotify_uri="spotify:track:1", yt_video_id="vid1",
        canon_title="s", canon_artist="a",
    )
    b = repo.upsert_track(
        spotify_uri="spotify:track:1", yt_video_id="vid2",
        canon_title="s", canon_artist="a",
    )
    assert a.id == b.id  # same row, updated in place
    assert repo.get_track_by_spotify_uri("spotify:track:1").yt_video_id == "vid2"


def test_upsert_does_not_downgrade_verified(repo: Repository):
    repo.upsert_track(
        spotify_uri="u", yt_video_id="v1", canon_title="s", canon_artist="a",
        verified=True, match_confidence=1.0,
    )
    # A later automatic (unverified) match must not clear the human verification.
    repo.upsert_track(
        spotify_uri="u", yt_video_id="v2", canon_title="s", canon_artist="a",
        verified=False, match_confidence=0.4,
    )
    row = repo.get_track_by_spotify_uri("u")
    assert row.verified is True
    assert row.yt_video_id == "v2"  # YT side still refreshed


def test_get_track_by_video_id_prefers_verified(repo: Repository):
    repo.upsert_track(spotify_uri="u1", yt_video_id="shared", canon_title="s",
                      canon_artist="a", verified=False, match_confidence=0.5)
    repo.upsert_track(spotify_uri="u2", yt_video_id="shared", canon_title="s",
                      canon_artist="a", verified=True, match_confidence=0.9)
    row = repo.get_track_by_video_id("shared")
    assert row.spotify_uri == "u2"


def test_set_track_resolution(repo: Repository):
    t = repo.upsert_track(spotify_uri="u", yt_video_id=None, canon_title="s",
                          canon_artist="a", match_method="fuzzy")
    repo.set_track_resolution(t.id, yt_video_id="chosen")
    row = repo.get_track(t.id)
    assert row.yt_video_id == "chosen"
    assert row.verified is True
    assert row.match_method == "manual"


# --------------------------------------------------------------------------- #
# collections
# --------------------------------------------------------------------------- #
def test_get_or_create_playlist_collection(repo: Repository):
    a = repo.get_or_create_collection(name="Chill", kind="playlist", spotify_playlist_id="sp1")
    b = repo.get_or_create_collection(name="Chill", kind="playlist", spotify_playlist_id="sp1")
    assert a.id == b.id


def test_playlist_requires_spotify_id(repo: Repository):
    with pytest.raises(ValueError):
        repo.get_or_create_collection(name="x", kind="playlist")


def test_singleton_collections_are_unique(repo: Repository):
    a = repo.get_or_create_collection(name="Liked Songs", kind="liked_songs")
    b = repo.get_or_create_collection(name="Liked Songs", kind="liked_songs")
    assert a.id == b.id
    # Only one liked_songs row exists.
    assert len([c for c in repo.list_collections() if c.kind == "liked_songs"]) == 1


def test_collection_rename_persists(repo: Repository):
    repo.get_or_create_collection(name="Old", kind="playlist", spotify_playlist_id="sp1")
    again = repo.get_or_create_collection(name="New", kind="playlist", spotify_playlist_id="sp1")
    assert again.name == "New"


def test_update_collection_rejects_unknown_field(repo: Repository):
    c = repo.get_or_create_collection(name="x", kind="liked_songs")
    with pytest.raises(ValueError):
        repo.update_collection(c.id, bogus="nope")


def test_update_collection_snapshot_and_fingerprint(repo: Repository):
    c = repo.get_or_create_collection(name="x", kind="playlist", spotify_playlist_id="sp1")
    repo.update_collection(c.id, spotify_snapshot_id="snap1", yt_playlist_id="yt1",
                           yt_content_fingerprint="fp1")
    row = repo.get_collection(c.id)
    assert row.spotify_snapshot_id == "snap1"
    assert row.yt_playlist_id == "yt1"
    assert row.yt_content_fingerprint == "fp1"


# --------------------------------------------------------------------------- #
# membership
# --------------------------------------------------------------------------- #
def test_membership_insert_and_merge(repo: Repository):
    c = repo.get_or_create_collection(name="x", kind="playlist", spotify_playlist_id="sp1")
    t = repo.upsert_track(spotify_uri="u", yt_video_id="v", canon_title="s", canon_artist="a")
    repo.upsert_membership(c.id, t.id, present_spotify=True, spotify_added_at="2026-01-01")
    repo.upsert_membership(c.id, t.id, present_yt=True, yt_set_video_id="set1")
    row = repo.get_membership(c.id, t.id)
    assert row["present_spotify"] == 1
    assert row["present_yt"] == 1  # merged, not overwritten
    assert row["yt_set_video_id"] == "set1"
    assert row["spotify_added_at"] == "2026-01-01"


def test_yt_present_video_ids(repo: Repository):
    c = repo.get_or_create_collection(name="x", kind="playlist", spotify_playlist_id="sp1")
    t1 = repo.upsert_track(spotify_uri="u1", yt_video_id="v1", canon_title="s", canon_artist="a")
    t2 = repo.upsert_track(spotify_uri="u2", yt_video_id="v2", canon_title="s", canon_artist="a")
    repo.upsert_membership(c.id, t1.id, present_yt=True)
    repo.upsert_membership(c.id, t2.id, present_spotify=True)  # not yet on YT
    assert repo.yt_present_video_ids(c.id) == {"v1"}


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #
def test_snapshot_latest_wins(repo: Repository):
    c = repo.get_or_create_collection(name="x", kind="playlist", spotify_playlist_id="sp1")
    repo.save_snapshot("spotify", c.id, {"uris": ["a"]})
    repo.save_snapshot("spotify", c.id, {"uris": ["a", "b"]})
    assert repo.get_latest_snapshot("spotify", c.id) == {"uris": ["a", "b"]}
    assert repo.get_latest_snapshot("yt", c.id) is None


# --------------------------------------------------------------------------- #
# quarantine
# --------------------------------------------------------------------------- #
def test_quarantine_add_list_resolve(repo: Repository):
    qid = repo.add_quarantine(
        source_side="spotify", source_uri="u", title="t", artist="a", album="al",
        reason="no_match", best_candidate={"video_id": "v"},
    )
    assert repo.count_unresolved_quarantine() == 1
    t = repo.upsert_track(spotify_uri="u", yt_video_id="v", canon_title="t", canon_artist="a")
    repo.resolve_quarantine(qid, t.id)
    assert repo.count_unresolved_quarantine() == 0


def test_quarantine_conflict_bumps_attempts(repo: Repository):
    repo.add_quarantine(source_side="spotify", source_uri="u", title="t", artist="a",
                        album=None, reason="no_match")
    repo.add_quarantine(source_side="spotify", source_uri="u", title="t", artist="a",
                        album=None, reason="low_conf")
    row = repo.get_quarantine_by_source("spotify", "u")
    assert row["attempts"] == 2
    assert row["reason"] == "low_conf"


# --------------------------------------------------------------------------- #
# sync_run + transactions
# --------------------------------------------------------------------------- #
def test_sync_run_lifecycle(repo: Repository):
    rid = repo.start_run("import", dry_run=True)
    repo.finish_run(rid, "ok", {"added": 3})
    last = repo.last_run()
    assert last["status"] == "ok"
    assert last["dry_run"] == 1


def test_transaction_rolls_back_on_error(repo: Repository):
    try:
        with repo.transaction():
            repo.upsert_track(spotify_uri="u", yt_video_id="v", canon_title="s", canon_artist="a")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert repo.get_track_by_spotify_uri("u") is None  # rolled back


def test_foreign_key_enforced(repo: Repository):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_membership(9999, 8888, present_spotify=True)  # dangling FKs


def test_connection_usable_across_threads(tmp_path):
    """The engine connection is created on one thread and used on worker threads
    (asyncio.to_thread). This must not raise sqlite3.ProgrammingError."""
    from concurrent.futures import ThreadPoolExecutor

    r = Repository(connect(tmp_path / "x.db"))
    r.upsert_track(spotify_uri="u", yt_video_id="v", canon_title="s", canon_artist="a")

    with ThreadPoolExecutor(max_workers=1) as pool:
        row = pool.submit(lambda: r.get_track_by_spotify_uri("u")).result()
    assert row is not None and row.yt_video_id == "v"
