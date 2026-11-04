"""Tests for normalization and the scored matching cascade."""

from __future__ import annotations

import pytest

from s2y.core import matching
from s2y.core.matching import (
    match,
    normalize_artist,
    normalize_title,
    score_candidate,
)
from s2y.core.models import MatchOutcome, SongInfo, YtCandidate


class FakeSearch:
    """In-memory MatchSearch with call counters, for testing the cascade."""

    def __init__(self, *, songs=None, videos=None, album=None):
        self.songs = songs or []
        self.videos = videos or []
        self.album = album or []
        self.calls = {"songs": 0, "videos": 0, "album": 0}

    def search_songs(self, query, *, limit=6):
        self.calls["songs"] += 1
        return self.songs[:limit]

    def search_videos(self, query, *, limit=4):
        self.calls["videos"] += 1
        return self.videos[:limit]

    def album_candidates(self, artist, album, *, limit_albums=3):
        self.calls["album"] += 1
        return self.album


def song(vid, title, artist, *, album="", dur=None, is_song=True):
    return YtCandidate(
        video_id=vid, title=title, artist=artist, album=album, duration_s=dur, is_song=is_song
    )


# --------------------------------------------------------------------------- #
# Content dedup: PresentIndex
# --------------------------------------------------------------------------- #
def _src(title, artist, dur):
    return SongInfo(title, artist, duration_ms=None if dur is None else dur * 1000)


def _present(source, cands):
    hit = matching.PresentIndex(cands).find(source)
    return None if hit is None else hit[0].video_id


def test_present_matches_exact_same_song():
    cands = [song("uri:A", "Blinding Lights", "The Weeknd", dur=200)]
    assert _present(_src("Blinding Lights", "The Weeknd", 200), cands) == "uri:A"


def test_present_tolerates_artist_credit_and_edition_variance():
    # The exact-id dedup misses these; content match must catch them. (Real cases:
    # "The Goo Goo Dolls" vs "Goo Goo Dolls", a remaster suffix on the title.)
    cands = [song("uri:A", "Iris", "The Goo Goo Dolls", dur=290)]
    assert _present(_src("Iris", "Goo Goo Dolls", 290), cands) == "uri:A"

    cands = [song("uri:B", "Father and Son", "Yusuf / Cat Stevens", dur=220)]
    assert _present(_src("Father and Son", "Cat Stevens", 220), cands) == "uri:B"

    cands = [song("uri:C", "A Little Respect - 2009 Remaster", "Erasure", dur=214)]
    assert _present(_src("A Little Respect", "Erasure", 214), cands) == "uri:C"


def test_present_rejects_duration_mismatch():
    # Same title/artist but a very different length (e.g. a live cut) is NOT the same
    # recording — it must still be proposed as an add, never silently deduped.
    cands = [song("uri:A", "Blinding Lights", "The Weeknd", dur=320)]
    assert _present(_src("Blinding Lights", "The Weeknd", 200), cands) is None


@pytest.mark.parametrize(
    ("source", "present", "dur"),
    [
        ("Hallelujah (Live)", "Hallelujah", 413),
        ("Dancing in the Dark (Remix)", "Dancing in the Dark", 245),
        ("Interlude Pt. 1", "Interlude Pt. 2", 120),
        ("Symphony No. 5", "Symphony No. 6", 300),
        ("Blinding Lights", "Blinding Lights Reprise", 200),
    ],
)
def test_present_rejects_other_versions_and_siblings(source, present, dur):
    # score_candidate gives all of these >= AUTO_ACCEPT; a false hit silently loses a track.
    cands = [song("uri:A", present, "Artist", dur=dur)]
    assert _present(_src(source, "Artist", dur), cands) is None


@pytest.mark.parametrize(("src_dur", "cand_dur"), [(None, 200), (200, None), (None, None)])
def test_present_fails_closed_on_missing_duration(src_dur, cand_dur):
    cands = [song("uri:A", "Blinding Lights", "The Weeknd", dur=cand_dur)]
    assert _present(_src("Blinding Lights", "The Weeknd", src_dur), cands) is None


def test_present_does_not_conflate_non_latin_titles():
    # normalize_title drops non-Latin letters: these reduce to "2" and "" respectively.
    cands = [song("uri:A", "恋 2", "アーティスト", dur=200)]
    assert _present(_src("愛 2", "アーティスト", 200), cands) is None
    assert _present(_src("恋 2", "アーティスト", 200), cands) == "uri:A"
    cands = [song("uri:B", "!!!", "Artist", dur=200)]
    assert _present(_src("???", "Artist", 200), cands) is None


def test_present_rejects_unrelated_song():
    cands = [song("uri:A", "Levitating", "Dua Lipa", dur=203)]
    assert _present(_src("Blinding Lights", "The Weeknd", 200), cands) is None


def test_present_picks_best_candidate():
    cands = [
        song("uri:wrong", "Blinding Lights", "Someone Else", dur=200),
        song("uri:right", "Blinding Lights", "The Weeknd", dur=200),
    ]
    assert _present(_src("Blinding Lights", "The Weeknd", 200), cands) == "uri:right"


def test_present_empty_index_returns_none():
    assert _present(_src("Blinding Lights", "The Weeknd", 200), []) is None


def test_same_album():
    assert matching.same_album(
        "Abbey Road", "The Beatles", "Abbey Road (Deluxe Edition)", "Beatles"
    )
    assert not matching.same_album("Abbey Road", "The Beatles", "Abbey Road", "Tribute Band")
    assert not matching.same_album("Hits", "Band", "Hits Vol. 2", "Band")
    assert matching.same_album("恋", "歌手", "恋", "歌手")
    assert not matching.same_album("恋", "歌手", "愛", "歌手")


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def test_normalize_strips_remaster_suffix():
    assert normalize_title("Bohemian Rhapsody - Remastered 2011") == "bohemian rhapsody"


def test_normalize_strips_edition_brackets():
    assert normalize_title("Song (2009 Remaster)") == "song"


def test_normalize_keeps_meaningful_qualifier():
    assert normalize_title("Song (Live)") == "song live"
    assert normalize_title("Song - Live") == "song live"


def test_normalize_strips_feat():
    assert normalize_title("Sicko Mode (feat. Drake)") == "sicko mode"
    assert normalize_artist("Travis Scott feat. Drake") == "travis scott"


def test_normalize_folds_accents_and_case():
    assert normalize_title("Déjà Vu") == "deja vu"
    assert normalize_artist("Beyoncé") == "beyonce"


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def test_score_exact_is_high():
    src = SongInfo("Blinding Lights", "The Weeknd", "After Hours", duration_ms=200000)
    cand = song("v", "Blinding Lights", "The Weeknd", album="After Hours", dur=200)
    assert score_candidate(src, cand) >= matching.AUTO_ACCEPT


def test_score_wrong_duration_is_gated_down():
    src = SongInfo("Yesterday", "The Beatles", duration_ms=125000)
    good = song("v1", "Yesterday", "The Beatles", dur=125)
    bad = song("v2", "Yesterday", "The Beatles", dur=900)  # 15-min something else
    assert score_candidate(src, good) > score_candidate(src, bad)
    assert score_candidate(src, bad) < matching.AUTO_ACCEPT


def test_score_video_penalized_vs_song():
    src = SongInfo("Title", "Artist", duration_ms=180000)
    as_song = song("v1", "Title", "Artist", dur=180, is_song=True)
    as_video = song("v2", "Title", "Artist", dur=180, is_song=False)
    assert score_candidate(src, as_song) > score_candidate(src, as_video)


# --------------------------------------------------------------------------- #
# Cascade
# --------------------------------------------------------------------------- #
def test_exact_match_auto_accepts():
    src = SongInfo("Blinding Lights", "The Weeknd", "After Hours", duration_ms=200000)
    s = FakeSearch(
        songs=[song("vid", "Blinding Lights", "The Weeknd", album="After Hours", dur=200)]
    )
    res = match(src, s)
    assert res.outcome is MatchOutcome.AUTO
    assert res.video_id == "vid"
    assert res.confidence >= matching.AUTO_ACCEPT


def test_videos_not_consulted_when_song_match_is_strong():
    src = SongInfo("Title", "Artist", duration_ms=180000)
    s = FakeSearch(
        songs=[song("vid", "Title", "Artist", dur=180)],
        videos=[song("bad", "Title", "Artist", dur=180)],
    )
    res = match(src, s)
    assert res.outcome is MatchOutcome.AUTO
    assert s.calls["videos"] == 0  # stage 3 skipped — we already had a winner


def test_video_fallback_when_no_songs():
    src = SongInfo("Rare Track", "Indie Artist", duration_ms=210000)
    s = FakeSearch(
        songs=[], videos=[song("vv", "Rare Track", "Indie Artist", dur=210, is_song=False)]
    )
    res = match(src, s)
    assert s.calls["videos"] == 1
    assert res.video_id == "vv"
    assert res.outcome is MatchOutcome.AUTO  # exact video still clears threshold


def test_album_stage_contributes_match():
    src = SongInfo("Deep Cut", "Some Band", album="The Album", duration_ms=240000)
    s = FakeSearch(album=[song("alb", "Deep Cut", "Some Band", album="The Album", dur=240)])
    res = match(src, s)
    assert res.outcome is MatchOutcome.AUTO
    assert res.video_id == "alb"
    assert s.calls["album"] == 1


def test_live_request_does_not_auto_accept_studio_only():
    src = SongInfo("Anthem - Live", "Band", duration_ms=300000)  # live, ~5min
    s = FakeSearch(songs=[song("studio", "Anthem", "Band", dur=200)])  # studio ~3min
    res = match(src, s)
    assert res.outcome is not MatchOutcome.AUTO  # quarantined for review
    assert res.confidence < matching.AUTO_ACCEPT


def test_low_confidence_quarantines_with_candidates():
    src = SongInfo("Imagine", "John Lennon", duration_ms=183000)
    s = FakeSearch(songs=[song("wrong", "Radioactive", "Imagine Dragons", dur=187)])
    res = match(src, s)
    assert res.outcome is MatchOutcome.QUARANTINE
    assert len(res.candidates) >= 1  # surfaced for human review


def test_missing_artist_is_no_match():
    src = SongInfo("Untitled", "", duration_ms=100000)
    res = match(src, FakeSearch(songs=[song("x", "Untitled", "Someone", dur=100)]))
    assert res.outcome is MatchOutcome.NO_MATCH


def test_no_candidates_is_no_match():
    src = SongInfo("Ghost Song", "Nobody", duration_ms=100000)
    res = match(src, FakeSearch())
    assert res.outcome is MatchOutcome.NO_MATCH


def test_wrong_artist_exact_title_is_quarantined():
    # Same title, totally different artist -> must not auto-accept.
    src = SongInfo("Hello", "Adele", duration_ms=295000)
    s = FakeSearch(songs=[song("x", "Hello", "Lionel Richie", dur=295)])
    res = match(src, s)
    assert res.outcome is not MatchOutcome.AUTO


def test_album_bonus_cannot_rescue_wrong_artist():
    # Exact title + same album name + same duration but wrong artist.
    src = SongInfo("Intro", "Real Band", album="Greatest Hits", duration_ms=180000)
    s = FakeSearch(songs=[song("x", "Intro", "Cover Band", album="Greatest Hits", dur=180)])
    assert match(src, s).outcome is not MatchOutcome.AUTO


def test_small_duration_drift_blocks_auto_accept():
    # 9 seconds off on an otherwise-exact match -> review, not auto.
    src = SongInfo("Blinding Lights", "The Weeknd", duration_ms=200000)
    s = FakeSearch(songs=[song("x", "Blinding Lights", "The Weeknd", dur=209)])
    res = match(src, s)
    assert res.outcome is not MatchOutcome.AUTO
    assert res.confidence < matching.AUTO_ACCEPT


def test_tiny_duration_drift_still_accepts():
    # A couple seconds is normal encoding variance -> still auto.
    src = SongInfo("Levitating", "Dua Lipa", duration_ms=203000)
    s = FakeSearch(songs=[song("x", "Levitating", "Dua Lipa", dur=205)])
    assert match(src, s).outcome is MatchOutcome.AUTO


def test_normalize_strips_edition_segments_anywhere():
    assert normalize_title("Song - Radio Edit - Remix") == "song remix"
    assert normalize_title("Song - Remastered 2011 - Live") == "song live"


def test_search_failure_does_not_crash():
    class Boom:
        def search_songs(self, q, *, limit=6):
            raise RuntimeError("network down")

        def search_videos(self, q, *, limit=4):
            raise RuntimeError("network down")

        def album_candidates(self, a, al, *, limit_albums=3):
            raise RuntimeError("network down")

    res = match(SongInfo("T", "A", duration_ms=1000), Boom())
    assert res.outcome is MatchOutcome.NO_MATCH  # degraded gracefully
