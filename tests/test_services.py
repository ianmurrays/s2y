"""Adapter parsing tests with fake spotipy / ytmusicapi clients (no network)."""

from __future__ import annotations

import logging

import pytest
import requests

from s2y.services.base import ServiceError, is_transient
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
            "items": [{"added_at": "x", "album": {"id": "al1", "name": "Greatest",
                                                  "artists": [{"name": "Band"}]}}],
            "next": None,
        }

    def next(self, page):
        return page.get("_next_page")


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
        return {"tracks": [{"videoId": "v1"}, {"videoId": "v2"}, {"videoId": None}]}

    def get_liked_songs(self, limit=100):
        return {"tracks": [{"videoId": "lk1"}, {"videoId": "lk2"}]}

    def get_library_albums(self, limit=25, order=None):
        return [{"title": "Greatest", "artists": [{"name": "Band"}]}]

    def search(self, query, filter=None, limit=20, **kw):
        if filter == "songs":
            return [{"videoId": "s1", "title": "Song A", "artists": [{"name": "Artist A"}],
                     "album": {"name": "Album"}, "duration_seconds": 200},
                    {"title": "No video id"}]
        if filter == "videos":
            return [{"videoId": "vid1", "title": "Song A", "artists": [{"name": "Artist A"}],
                     "duration_seconds": 201}]
        if filter == "albums":
            return [{"browseId": "MPRE1", "title": "Greatest"}]
        return []

    def get_album(self, browseId):
        return {"title": "Greatest", "artists": [{"name": "Band"}],
                "audioPlaylistId": "OLAK5", "tracks": [
                    {"videoId": "at1", "title": "Track 1", "duration_seconds": 180}]}

    def create_playlist(self, title, description, privacy_status="PRIVATE", **kw):
        self.created.append(title)
        return "newpl"

    def add_playlist_items(self, playlistId, videoIds=None, duplicates=False):
        self.added.append((playlistId, list(videoIds or [])))
        return {"status": "STATUS_SUCCEEDED",
                "playlistEditResults": [{"videoId": vid, "setVideoId": f"set-{vid}"}
                                        for vid in (videoIds or [])]}

    def rate_song(self, videoId, rating):
        self.liked.append(videoId)

    def rate_playlist(self, playlistId, rating):
        self.rated_playlists.append(playlistId)


def test_yt_list_playlists_filters_liked_music_and_parses_count():
    pls = YTMusicClient(FakeYT(), sleep=NO_SLEEP).list_playlists()
    assert [p.id for p in pls] == ["yt1"]
    assert pls[0].count == 1234


def test_yt_present_ids():
    c = YTMusicClient(FakeYT(), sleep=NO_SLEEP)
    assert c.playlist_video_ids("yt1") == {"v1", "v2"}
    assert c.liked_video_ids() == {"lk1", "lk2"}
    assert c.library_album_keys() == {("band", "greatest")}


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
    sess.request("GET", "http://example")               # no timeout -> injected
    sess.request("GET", "http://example", timeout=1.5)  # caller's value preserved
    assert seen == [9.0, 1.5]


def test_is_transient_classifies_errors():
    from ytmusicapi.exceptions import YTMusicServerError

    class WithStatus(Exception):
        def __init__(self, status):
            super().__init__("x")
            self.http_status = status

    assert is_transient(WithStatus(429)) is True       # rate limited
    assert is_transient(WithStatus(503)) is True        # server error
    assert is_transient(WithStatus(401)) is False       # auth — deterministic
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
