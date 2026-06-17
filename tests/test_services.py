"""Adapter parsing tests with fake spotipy / ytmusicapi clients (no network)."""

from __future__ import annotations

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
