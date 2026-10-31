"""Adapter parsing tests with fake spotipy / ytmusicapi clients (no network)."""

from __future__ import annotations

import json
import logging
import subprocess

import pytest
import requests

from s2y.core.models import ClassifyDecision, ClassifyItem, SongInfo, YtCandidate
from s2y.services.base import ServiceError, is_transient
from s2y.services.claude_classify import ClaudeClassifier
from s2y.services.spotify import SpotifyClient
from s2y.services.ytmusic import YTMusicClient

NO_SLEEP = lambda _: None  # noqa: E731


# --------------------------------------------------------------------------- #
# Spotify
# --------------------------------------------------------------------------- #
def _track(name, artist, uri, *, isrc=None, dur=200000, album="Album", local=False, ttype="track"):
    return {
        "added_at": "2026-01-01T00:00:00Z",
        "track": {
            "name": name,
            "type": ttype,
            "is_local": local,
            "duration_ms": dur,
            "uri": uri,
            "external_ids": {"isrc": isrc} if isrc else {},
            "artists": [{"name": artist}],
            "album": {"name": album},
        },
    }


class FakeSpotify:
    def current_user(self):
        return {"display_name": "Ian", "id": "ian"}

    def current_user_playlists(self, limit=50):
        return {
            "items": [{"id": "p1", "name": "One", "tracks": {"total": 1}, "snapshot_id": "s1"}],
            "next": "url",
            "_next_page": {
                "items": [{"id": "p2", "name": "Two", "tracks": {"total": 2}, "snapshot_id": "s2"}],
                "next": None,
            },
        }

    def playlist_items(self, playlist_id, limit=100, additional_types=("track",)):
        return {
            "items": [
                _track("Song A", "Artist A", "spotify:track:a", isrc="US0000000001"),
                {"added_at": "x", "track": None},  # unavailable -> skipped
                _track("Local", "X", "spotify:local:x", local=True),  # local -> skipped
            ],
            "next": "url",
            "_next_page": {
                "items": [_track("Song B", "Artist B", "spotify:track:b")],
                "next": None,
            },
        }

    def current_user_saved_tracks(self, limit=50):
        return {"items": [_track("Liked", "Lk", "spotify:track:l")], "next": None}

    def current_user_saved_albums(self, limit=50):
        return {
            "items": [
                {
                    "added_at": "x",
                    "album": {"id": "al1", "name": "Greatest", "artists": [{"name": "Band"}]},
                }
            ],
            "next": None,
        }

    def next(self, page):
        return page.get("_next_page")

    # ----- target role (reverse sync) ----- #
    def __init__(self):
        self.created: list[tuple[str, str]] = []  # (user, name)
        self.added: list[tuple[str, list[str]]] = []
        self.liked_tracks: list[str] = []
        self.saved_albums: list[str] = []

    def search(self, q=None, type=None, limit=10, **kw):
        if type == "track":
            return {
                "tracks": {
                    "items": [
                        {
                            "name": "Song A",
                            "uri": "spotify:track:a",
                            "duration_ms": 200000,
                            "artists": [{"name": "Artist A"}],
                            "album": {"name": "Album"},
                        },
                        {"name": "No uri", "artists": [{"name": "X"}]},  # dropped
                    ]
                }
            }
        if type == "album":
            return {
                "albums": {
                    "items": [{"id": "al1", "name": "Greatest", "artists": [{"name": "Band"}]}]
                }
            }
        return {}

    def album_tracks(self, album_id, limit=50):
        return {
            "items": [
                {
                    "name": "Track 1",
                    "uri": "spotify:track:t1",
                    "duration_ms": 180000,
                    "artists": [{"name": "Band"}],
                }
            ],
            "next": None,
        }

    def user_playlist_create(self, user, name, public=True, collaborative=False, description=""):
        self.created.append((user, name))
        return {"id": "newpl", "name": name}

    def playlist_add_items(self, playlist_id, items, position=None):
        self.added.append((playlist_id, list(items)))
        return {"snapshot_id": "snap"}

    def current_user_saved_tracks_add(self, tracks):
        self.liked_tracks.extend(tracks)

    def current_user_saved_albums_add(self, albums):
        self.saved_albums.extend(albums)


def test_spotify_verify():
    assert SpotifyClient(FakeSpotify(), sleep=NO_SLEEP).verify() == "Ian"


def test_spotify_list_playlists_paginates():
    pls = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP).list_playlists()
    assert [p.id for p in pls] == ["p1", "p2"]
    assert pls[0].snapshot_id == "s1"
    assert pls[1].count == 2


def test_spotify_playlist_tracks_skips_bad_and_paginates():
    tracks = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP).playlist_tracks("p1")
    assert [t.title for t in tracks] == ["Song A", "Song B"]  # None + local dropped
    assert tracks[0].isrc == "US0000000001"
    assert tracks[0].spotify_uri == "spotify:track:a"
    assert tracks[0].duration_ms == 200000


def test_spotify_liked_songs_and_albums():
    c = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP)
    assert [t.title for t in c.liked_songs()] == ["Liked"]
    albums = c.liked_albums()
    assert albums[0].name == "Greatest"
    assert albums[0].artist == "Band"


# ----- Spotify target role (reverse sync) ----- #
def test_spotify_present_candidates():
    c = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP)
    # Null track + local dropped, and the second page is read.
    tracks = c.present_playlist_candidates("p1")
    assert [(t.video_id, t.title, t.artist, t.duration_s) for t in tracks] == [
        ("spotify:track:a", "Song A", "Artist A", 200),
        ("spotify:track:b", "Song B", "Artist B", 200),
    ]
    liked = c.present_liked_candidates()
    assert [(t.video_id, t.title, t.artist, t.duration_s) for t in liked] == [
        ("spotify:track:l", "Liked", "Lk", 200)
    ]
    assert c.present_album_keys() == {("band", "greatest")}


def test_spotify_present_candidates_drops_episodes():
    fake = FakeSpotify()
    fake.playlist_items = lambda pid, limit=100, additional_types=("track",): {
        "items": [_track("Ep", "Host", "spotify:episode:e", ttype="episode")],
        "next": None,
    }
    assert SpotifyClient(fake, sleep=NO_SLEEP).present_playlist_candidates("p1") == []


def test_spotify_find_playlist_id_has_no_side_effects():
    fake = FakeSpotify()
    c = SpotifyClient(fake, sleep=NO_SLEEP)
    assert c.find_playlist_id("Two") == "p2"
    assert c.find_playlist_id("Missing") is None
    assert fake.created == []


def test_spotify_search_candidate_duration_is_seconds():
    c = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP)
    songs = c.search_songs("q")
    assert [s.video_id for s in songs] == ["spotify:track:a"]  # no-uri result dropped
    assert songs[0].duration_s == 200  # 200000 ms -> 200 s
    assert songs[0].is_song is True
    assert c.search_videos("q") == []  # Spotify has no video axis


def test_spotify_album_candidates():
    cands = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP).album_candidates("Band", "Greatest")
    assert [c.video_id for c in cands] == ["spotify:track:t1"]
    assert cands[0].album == "Greatest"
    assert cands[0].duration_s == 180


def test_spotify_get_or_create_playlist_uses_cache_then_creates():
    sp = FakeSpotify()
    c = SpotifyClient(sp, sleep=NO_SLEEP)
    assert c.get_or_create_playlist("One") == "p1"  # existing, matched by name
    assert c.get_or_create_playlist("Brand New") == "newpl"  # created
    assert sp.created == [("ian", "Brand New")]
    assert c.get_or_create_playlist("Brand New") == "newpl"  # cached, no second create
    assert sp.created == [("ian", "Brand New")]


def test_spotify_add_playlist_items_dedups_internally():
    sp = FakeSpotify()
    c = SpotifyClient(sp, sleep=NO_SLEEP)
    # p1 already contains spotify:track:a and :b (from the playlist_items fake).
    mapping = c.add_playlist_items("p1", ["spotify:track:a", "spotify:track:new"])
    assert mapping == {
        "spotify:track:a": None,
        "spotify:track:new": None,
    }  # no setVideoId on Spotify
    assert sp.added == [("p1", ["spotify:track:new"])]  # the already-present uri filtered out


def test_spotify_like_song_and_album():
    sp = FakeSpotify()
    c = SpotifyClient(sp, sleep=NO_SLEEP)
    c.like_song("spotify:track:z")
    assert sp.liked_tracks == ["spotify:track:z"]
    assert c.like_album("Band", "Greatest") == "al1"
    assert sp.saved_albums == ["al1"]


# --------------------------------------------------------------------------- #
# YouTube Music
# --------------------------------------------------------------------------- #
class FakeYT:
    def __init__(self):
        self.created: list[str] = []
        self.added: list[tuple[str, list[str]]] = []
        self.liked: list[str] = []
        self.rated_playlists: list[str] = []

    def get_library_playlists(self, limit=25):
        return [
            {"playlistId": "LM", "title": "Liked Music", "count": "10"},  # filtered out
            {"playlistId": "yt1", "title": "One", "count": "1,234"},
        ]

    def get_playlist(self, playlistId, limit=100, **kw):
        return {
            "tracks": [
                {
                    "videoId": "v1",
                    "title": "Song A",
                    "artists": [{"name": "Artist A"}],
                    "album": {"name": "Album"},
                    "duration_seconds": 200,
                },
                {"videoId": "v2", "title": "Song B", "artists": [{"name": "Artist B"}]},
                {"videoId": None, "title": "Unavailable"},  # dropped
            ]
        }

    def get_liked_songs(self, limit=100):
        return {
            "tracks": [
                {"videoId": "lk1", "title": "Liked 1", "artists": [{"name": "L1"}]},
                {"videoId": "lk2", "title": "Liked 2", "artists": [{"name": "L2"}]},
            ]
        }

    def get_library_albums(self, limit=25, order=None):
        return [{"browseId": "MPREab", "title": "Greatest", "artists": [{"name": "Band"}]}]

    def search(self, query, filter=None, limit=20, **kw):
        if filter == "songs":
            return [
                {
                    "videoId": "s1",
                    "title": "Song A",
                    "artists": [{"name": "Artist A"}],
                    "album": {"name": "Album"},
                    "duration_seconds": 200,
                },
                {"title": "No video id"},
            ]
        if filter == "videos":
            return [
                {
                    "videoId": "vid1",
                    "title": "Song A",
                    "artists": [{"name": "Artist A"}],
                    "duration_seconds": 201,
                }
            ]
        if filter == "albums":
            return [{"browseId": "MPRE1", "title": "Greatest"}]
        return []

    def get_album(self, browseId):
        return {
            "title": "Greatest",
            "artists": [{"name": "Band"}],
            "audioPlaylistId": "OLAK5",
            "tracks": [{"videoId": "at1", "title": "Track 1", "duration_seconds": 180}],
        }

    def create_playlist(self, title, description, privacy_status="PRIVATE", **kw):
        self.created.append(title)
        return "newpl"

    def add_playlist_items(self, playlistId, videoIds=None, duplicates=False):
        self.added.append((playlistId, list(videoIds or [])))
        return {
            "status": "STATUS_SUCCEEDED",
            "playlistEditResults": [
                {"videoId": vid, "setVideoId": f"set-{vid}"} for vid in (videoIds or [])
            ],
        }

    def rate_song(self, videoId, rating):
        self.liked.append(videoId)

    def rate_playlist(self, playlistId, rating):
        self.rated_playlists.append(playlistId)


def test_yt_list_playlists_filters_liked_music_and_parses_count():
    pls = YTMusicClient(FakeYT(), sleep=NO_SLEEP).list_playlists()
    assert [p.id for p in pls] == ["yt1"]
    assert pls[0].count == 1234


def test_yt_present_candidates():
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    # None videoId dropped; duration_seconds parsed, and None when absent.
    tracks = c.present_playlist_candidates("yt1")
    assert [(t.video_id, t.title, t.artist, t.duration_s) for t in tracks] == [
        ("v1", "Song A", "Artist A", 200),
        ("v2", "Song B", "Artist B", None),
    ]
    assert [t.video_id for t in c.present_liked_candidates()] == ["lk1", "lk2"]
    assert c.present_album_keys() == {("band", "greatest")}


def test_yt_find_playlist_id_has_no_side_effects():
    fake = FakeYT()
    c = YTMusicClient(fake, sleep=NO_SLEEP)
    assert c.find_playlist_id("One") == "yt1"
    assert c.find_playlist_id("Missing") is None
    assert fake.created == []


def test_yt_rich_reads_for_reverse_source():
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    tracks = c.playlist_tracks("yt1")
    assert [t.title for t in tracks] == ["Song A", "Song B"]  # None videoId dropped
    assert tracks[0].yt_video_id == "v1"
    assert tracks[0].duration_ms == 200_000  # duration_seconds * 1000
    assert tracks[0].album == "Album"
    liked = c.liked_songs()
    assert [t.yt_video_id for t in liked] == ["lk1", "lk2"]
    albums = c.liked_albums()
    assert albums[0].id == "MPREab" and albums[0].name == "Greatest" and albums[0].artist == "Band"


def test_yt_search_songs_and_videos():
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    songs = c.search_songs("q")
    assert [s.video_id for s in songs] == ["s1"]  # the no-videoId result dropped
    assert songs[0].is_song is True
    videos = c.search_videos("q")
    assert videos[0].is_song is False


def test_yt_album_candidates():
    cands = YTMusicClient(FakeYT(), sleep=NO_SLEEP).album_candidates("Band", "Greatest")
    assert [c.video_id for c in cands] == ["at1"]
    assert cands[0].album == "Greatest"


def test_yt_get_or_create_playlist_uses_cache_then_creates():
    yt = FakeYT()
    c = YTMusicClient(yt, sleep=NO_SLEEP)
    assert c.get_or_create_playlist("One") == "yt1"  # existing
    assert c.get_or_create_playlist("Brand New") == "newpl"  # created
    assert yt.created == ["Brand New"]
    # second lookup of the new one is cached (no extra create)
    assert c.get_or_create_playlist("Brand New") == "newpl"
    assert yt.created == ["Brand New"]


def test_yt_add_playlist_items_captures_set_video_ids():
    yt = FakeYT()
    mapping = YTMusicClient(yt, sleep=NO_SLEEP).add_playlist_items("yt1", ["v1", "v2"])
    assert mapping == {"v1": "set-v1", "v2": "set-v2"}
    assert yt.added == [("yt1", ["v1", "v2"])]


def test_yt_set_video_ids_tolerates_none_entries():
    # ytmusicapi can yield None entries in playlistEditResults; must not crash.
    from s2y.services.ytmusic import _set_video_ids

    resp = {"status": "OK", "playlistEditResults": [{"videoId": "v1", "setVideoId": "s1"}, None]}
    assert _set_video_ids(resp, ["v1", "v2"]) == {"v1": "s1", "v2": None}


def test_yt_like_song_and_album():
    yt = FakeYT()
    c = YTMusicClient(yt, sleep=NO_SLEEP)
    c.like_song("v9")
    assert yt.liked == ["v9"]
    browse = c.like_album("Band", "Greatest")
    assert browse == "MPRE1"
    assert yt.rated_playlists == ["OLAK5"]


# --------------------------------------------------------------------------- #
# Robustness: timeouts + visible retries
# --------------------------------------------------------------------------- #
def test_timeout_session_injects_default_timeout(monkeypatch):
    import requests

    from s2y.services.base import TimeoutSession

    seen: list[object] = []

    def fake_request(self, method, url, **kwargs):
        seen.append(kwargs.get("timeout"))
        return "resp"

    monkeypatch.setattr(requests.Session, "request", fake_request)
    sess = TimeoutSession(9.0)
    sess.request("GET", "http://example")  # no timeout -> injected
    sess.request("GET", "http://example", timeout=1.5)  # caller's value preserved
    assert seen == [9.0, 1.5]


def test_is_transient_classifies_errors():
    from ytmusicapi.exceptions import YTMusicServerError

    class WithStatus(Exception):
        def __init__(self, status):
            super().__init__("x")
            self.http_status = status

    assert is_transient(WithStatus(429)) is True  # rate limited
    assert is_transient(WithStatus(503)) is True  # server error
    assert is_transient(WithStatus(401)) is False  # auth — deterministic
    assert is_transient(WithStatus(404)) is False
    assert is_transient(requests.exceptions.Timeout()) is True
    assert is_transient(requests.exceptions.ConnectionError()) is True
    assert is_transient(YTMusicServerError("backend")) is True
    assert is_transient(KeyError("twoColumnBrowseResultsRenderer")) is False
    assert is_transient(RuntimeError("???")) is False


def test_yt_call_retries_transient_errors_and_logs(caplog):
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise requests.exceptions.ConnectionError("blip")  # transient
        return "ok"

    with caplog.at_level(logging.WARNING, logger="s2y.services.ytmusic"):
        assert c._call(flaky, "search songs 'x'") == "ok"  # noqa: SLF001
    assert state["n"] == 2  # retried once
    assert any("search songs 'x'" in r.message for r in caplog.records)


def test_spotify_call_retries_transient_errors_and_logs(caplog):
    c = SpotifyClient(FakeSpotify(), sleep=NO_SLEEP)
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise requests.exceptions.ConnectionError("blip")  # transient
        return "ok"

    with caplog.at_level(logging.WARNING, logger="s2y.services.spotify"):
        assert c._call(flaky, "liked songs") == "ok"  # noqa: SLF001
    assert state["n"] == 2
    assert any("liked songs" in r.message for r in caplog.records)


def test_yt_call_fails_fast_on_non_transient_error():
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    calls = {"n": 0}

    def deterministic():
        calls["n"] += 1
        raise KeyError("nope")  # not transient -> must not retry

    with pytest.raises(KeyError):
        c._call(deterministic, "get liked songs")  # noqa: SLF001
    assert calls["n"] == 1  # one attempt, no backoff storm


# --------------------------------------------------------------------------- #
# Claude classifier (LLM judge) — subprocess parsing, no real `claude` spawned
# --------------------------------------------------------------------------- #
_ITEMS = (
    ClassifyItem(SongInfo("Archangel", "Burial"), (YtCandidate("v1", "Archangel", "Burial"),)),
    ClassifyItem(
        SongInfo("Ghost Voices", "Virtual Self"),
        (YtCandidate("v2", "Ghost Voices", "Virtual Self"),),
    ),
    ClassifyItem(SongInfo("Untitled", "Nobody"), (YtCandidate("v3", "Untitled", "Nobody"),)),
)


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _envelope(verdicts: list[dict], *, is_error=False) -> str:
    return json.dumps({"is_error": is_error, "result": json.dumps(verdicts)})


def test_claude_classifier_parses_batch_envelope(monkeypatch):
    out = _envelope(
        [
            {"index": 1, "decision": "accept", "video_id": "v1", "confidence": 0.9},
            {"index": 2, "decision": "reject", "video_id": None, "confidence": 0.7},
            {"index": 3, "decision": "accept", "video_id": "v3", "confidence": 0.95},
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout=out))
    out_verdicts = ClaudeClassifier().classify_batch(_ITEMS)
    assert len(out_verdicts) == 3
    assert out_verdicts[0].decision is ClassifyDecision.ACCEPT and out_verdicts[0].video_id == "v1"
    assert out_verdicts[1].decision is ClassifyDecision.REJECT
    assert out_verdicts[2].decision is ClassifyDecision.ACCEPT and out_verdicts[2].video_id == "v3"


def test_claude_classifier_batches_into_one_subprocess_call(monkeypatch):
    """The whole point: N tracks => ONE `claude -p` spawn, not N."""
    calls = {"n": 0}

    def fake(cmd, **kwargs):
        calls["n"] += 1
        return _completed(
            stdout=_envelope(
                [
                    {"index": i + 1, "decision": "reject", "confidence": 0.5}
                    for i in range(len(_ITEMS))
                ]
            )
        )

    monkeypatch.setattr(subprocess, "run", fake)
    ClaudeClassifier().classify_batch(_ITEMS)
    assert calls["n"] == 1


def test_claude_classifier_builds_expected_command(monkeypatch):
    captured: dict = {}

    def fake(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(stdout=_envelope([{"index": 1, "decision": "reject"}]))

    monkeypatch.setattr(subprocess, "run", fake)
    ClaudeClassifier(model="haiku").classify_batch(_ITEMS[:1])
    cmd = captured["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert "--output-format" in cmd and "json" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--tools" in cmd  # tools disabled for a fast, single-shot judgment


def test_claude_classifier_empty_batch_skips_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess must not be called for an empty batch")

    monkeypatch.setattr(subprocess, "run", boom)
    assert ClaudeClassifier().classify_batch(()) == ()


def _all_uncertain(verdicts) -> bool:
    return len(verdicts) == len(_ITEMS) and all(
        v.decision is ClassifyDecision.UNCERTAIN for v in verdicts
    )


def test_claude_classifier_binary_missing_is_all_uncertain(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", boom)
    assert _all_uncertain(ClaudeClassifier(claude_path="/no/such/claude").classify_batch(_ITEMS))


def test_claude_classifier_timeout_is_all_uncertain(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert _all_uncertain(ClaudeClassifier().classify_batch(_ITEMS))


def test_claude_classifier_nonzero_exit_is_all_uncertain(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(returncode=1, stderr="boom"))
    assert _all_uncertain(ClaudeClassifier().classify_batch(_ITEMS))


def test_claude_classifier_is_error_envelope_is_all_uncertain(monkeypatch):
    out = json.dumps({"is_error": True, "result": None})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout=out))
    assert _all_uncertain(ClaudeClassifier().classify_batch(_ITEMS))


def test_claude_classifier_no_result_field_is_all_uncertain(monkeypatch):
    out = json.dumps({"is_error": False})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout=out))
    assert _all_uncertain(ClaudeClassifier().classify_batch(_ITEMS))


def test_claude_classifier_non_json_stdout_is_all_uncertain(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(stdout="not json"))
    assert _all_uncertain(ClaudeClassifier().classify_batch(_ITEMS))


def test_yt_call_translates_signed_out_into_service_error():
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    calls = {"n": 0}

    def signed_out():
        calls["n"] += 1
        # The real ytmusicapi parse failure embeds the logged-out response.
        raise KeyError(
            "Unable to find 'twoColumnBrowseResultsRenderer' ... 'signInEndpoint' ... "
            "Sign in to listen to your liked tracks ..."
        )

    with pytest.raises(ServiceError, match="signed out"):
        c._call(signed_out, "get liked songs")  # noqa: SLF001
    assert calls["n"] == 1  # surfaced immediately, not retried for minutes
