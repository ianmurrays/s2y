"""Track matching: resolve a Spotify :class:`SongInfo` to a YouTube Music video.

This is the single biggest correctness lever in the whole app. The reference
project's default algorithm blindly returns the first song-search hit, which
routinely picks the wrong version (live/remix/cover/sped-up) — and in a two-way
sync a bad match would propagate *back* into Spotify. So we:

* normalize titles/artists (casefold, fold accents, drop "feat." and edition
  tags like "(Remastered 2011)" while KEEPING meaningful qualifiers like
  "(Live)"/"remix" so a remix never matches the original at high confidence),
* score each candidate on title + artist similarity,
* **gate on duration** (±a few seconds) — the reference uses none,
* auto-accept only above a confidence threshold and **quarantine everything else
  for human review** rather than guessing.

The module is pure: it receives candidate lists through the :class:`MatchSearch`
Protocol (satisfied by ``s2y.services.ytmusic``), never touching the network.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Protocol

from .models import MatchMethod, MatchOutcome, MatchResult, SongInfo, YtCandidate

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
AUTO_ACCEPT = 0.82  # >= this => accept without asking
REVIEW_FLOOR = 0.50  # >= this (but < AUTO) => quarantine WITH a best candidate
DURATION_TOL_S = 4  # treat durations within this many seconds as equal
_ARTIST_GATE = 0.7  # below this the artist is dubious (see score_candidate)


class MatchSearch(Protocol):
    """The search surface the matcher needs. Implemented by the YTMusic adapter."""

    def search_songs(self, query: str, *, limit: int = 6) -> list[YtCandidate]: ...
    def search_videos(self, query: str, *, limit: int = 4) -> list[YtCandidate]: ...
    def album_candidates(
        self, artist: str, album: str, *, limit_albums: int = 3
    ) -> list[YtCandidate]: ...


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
# Parenthetical/bracketed qualifiers that DON'T change the recording's identity
# and should be stripped before comparison.
_EDITION_TERMS = re.compile(
    r"""\b(
        remaster(ed)?(\s*\d{4})? | re-?master(ed)? |
        deluxe(\s+edition)? | expanded(\s+edition)? | anniversary(\s+edition)? |
        bonus\s+track | mono | stereo | explicit | clean |
        radio\s+edit | single\s+version | album\s+version | original\s+mix |
        \d{4}\s+remaster | \d{4}\s+version
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Qualifiers that DO change the recording — never strip these.
_MEANINGFUL = re.compile(
    r"\b(live|acoustic|remix|instrumental|demo|unplugged|reprise|cover|karaoke|"
    r"sped\s*up|slowed|edit\s+by)\b",
    re.IGNORECASE,
)

_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*", re.IGNORECASE)
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_DASH_SPLIT = re.compile(r"\s+-\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NON_WORD = re.compile(r"[\W_]+")


def fold_accents(text: str) -> str:
    """NFKD-decompose and drop combining marks: 'Beyoncé' -> 'beyonce'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _drop_edition_brackets(text: str) -> str:
    """Remove only bracketed groups whose content is an edition tag.

    Keeps meaningful brackets — '(Live)', '(Acoustic)', '(Remix)' — so those
    versions don't get scored as the original.
    """

    def repl(match: re.Match) -> str:
        inner = match.group(0)[1:-1]
        return "" if _EDITION_TERMS.search(inner) else match.group(0)

    return _BRACKETS.sub(repl, text)


def _drop_edition_segments(text: str) -> str:
    """Drop ' - <edition>' segments anywhere after the title.

    The title (first dash-separated part) is always kept. Each later segment is
    dropped if it is an edition tag ('Remastered 2011', 'Radio Edit') but kept if
    it is a meaningful qualifier ('Live', 'Acoustic', 'Remix') so those versions
    stay distinct from the original. Filtering every segment (not just the last)
    handles 'Song - Radio Edit - Remix' -> 'Song - Remix'.
    """
    parts = _DASH_SPLIT.split(text)
    if len(parts) <= 1:
        return text
    kept = [parts[0]]
    for part in parts[1:]:
        if _EDITION_TERMS.search(part) and not _MEANINGFUL.search(part):
            continue
        kept.append(part)
    return " - ".join(kept)


def normalize_title(title: str) -> str:
    t = title or ""
    t = _drop_edition_brackets(t)
    t = _drop_edition_segments(t)
    t = _FEAT.sub("", t)  # drop trailing "feat. X"
    t = fold_accents(t.casefold())
    t = _NON_ALNUM.sub(" ", t)
    return " ".join(t.split())


def normalize_artist(artist: str) -> str:
    a = artist or ""
    a = _FEAT.sub("", a)
    a = fold_accents(a.casefold())
    a = _NON_ALNUM.sub(" ", a)
    return " ".join(a.split())


def _word_key(text: str) -> str:
    """Like :func:`normalize_artist`, but keeps non-Latin letters."""
    folded = fold_accents(_FEAT.sub("", text or "").casefold())
    return " ".join(_NON_WORD.sub(" ", folded).split())


def _title_key(title: str) -> str:
    """Like :func:`normalize_title`, but keeps non-Latin letters."""
    return _word_key(_drop_edition_segments(_drop_edition_brackets(title or "")))


def canonical_key(song: SongInfo) -> tuple[str, str]:
    """Stable (title, artist) key used for dedup and the track_map canon columns."""
    return normalize_title(song.title), normalize_artist(song.artist)


# --------------------------------------------------------------------------- #
# Similarity + scoring
# --------------------------------------------------------------------------- #
def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    # Substring containment is a strong signal, but only when the contained
    # string is most of the container — otherwise "hello" would over-match
    # "hello goodbye" (and a studio cut would over-match a "- live" request).
    shorter, longer = sorted((a, b), key=len)
    contain = 0.0
    if len(shorter) >= 3 and shorter in longer and len(shorter) / len(longer) >= 0.6:
        contain = 0.92
    return max(ratio, jaccard, contain)


def score_candidate(
    source: SongInfo,
    cand: YtCandidate,
    *,
    duration_tol_s: int = DURATION_TOL_S,
) -> float:
    """Confidence in [0, 1] that ``cand`` is the same recording as ``source``."""
    title_sim = _similarity(normalize_title(source.title), normalize_title(cand.title))
    artist_sim = _similarity(normalize_artist(source.artist), normalize_artist(cand.artist))

    # Title carries a little more weight, but the artist matters enough that a
    # strong title alone can't carry a wrong artist to auto-accept.
    score = 0.60 * title_sim + 0.40 * artist_sim

    # Album agreement is a weak positive — but only trust it when the artist
    # already matches well, otherwise a shared/compilation album name could
    # rescue a wrong-artist candidate into auto-accept.
    if (
        source.album
        and cand.album
        and artist_sim >= 0.7
        and _similarity(normalize_title(source.album), normalize_title(cand.album)) > 0.85
    ):
        score += 0.04

    # A dubious artist must not auto-accept on the strength of the title alone.
    # Genuinely different artists that merely share a word (e.g. "Real Band" vs
    # "Cover Band") land around 0.6-0.65, so the gate sits at 0.7; exact and
    # containment matches ("The Weeknd" vs "Weeknd") score >= 0.9 and pass.
    if artist_sim < _ARTIST_GATE:
        score *= 0.7

    # Duration gate — the key accuracy improvement over the reference. Small
    # differences are fine; larger ones scale the score down so an otherwise
    # exact text match with a notably different length (live / sped-up /
    # instrumental) drops out of auto-accept and into human review.
    if source.duration_s is not None and cand.duration_s is not None:
        diff = abs(source.duration_s - cand.duration_s)
        if diff <= duration_tol_s:
            score += 0.08
        else:
            score *= max(0.45, 1.0 - 0.05 * (diff - duration_tol_s))

    # "videos" results are noisier (live clips, covers, sped-up) than "songs".
    if not cand.is_song:
        score *= 0.85

    return max(0.0, min(1.0, score))


# --------------------------------------------------------------------------- #
# Content dedup: "is this song already in the target collection?"
# --------------------------------------------------------------------------- #
class PresentIndex:
    """Index of the tracks already in a target collection, for content-based dedup.

    Exact-id (uri/videoId) dedup misses the common cross-service case where the same
    recording sits under a *different* release id on each service, with the artist even
    spelled differently ("Cat Stevens" vs "Yusuf / Cat Stevens"). A wrong hit here means
    a track silently never syncs, so this is stricter than :func:`score_candidate`: the
    title must be equal once edition tags are stripped (so "(Live)"/"(Remix)"/"Pt. 2"
    stay distinct), both durations must be known and within tolerance, and the artist
    must clear the matcher's artist gate. Anything less is proposed as an add.

    Keys keep non-Latin letters, unlike :func:`normalize_title`, which reduces "恋 2" and
    "愛 2" to the same "2". The blind spot is a cross-language title rename ("Hijo de la
    Luna" / "Figlio della luna") — a rare, acceptable miss (it just gets added).
    """

    def __init__(
        self, candidates: list[YtCandidate], *, duration_tol_s: int = DURATION_TOL_S
    ) -> None:
        self._tol = duration_tol_s
        self._by_title: dict[str, list[tuple[YtCandidate, str]]] = {}
        for c in candidates:
            title = _title_key(c.title)
            if c.video_id and title and c.duration_s is not None:
                self._by_title.setdefault(title, []).append((c, _word_key(c.artist)))

    def find(self, source: SongInfo) -> tuple[YtCandidate, float] | None:
        """The present candidate that is ``source``, with its artist similarity, or ``None``."""
        title = _title_key(source.title)
        if not title or source.duration_s is None:
            return None
        artist = _word_key(source.artist)
        best: YtCandidate | None = None
        best_sim = 0.0
        for c, c_artist in self._by_title.get(title, ()):
            if abs(source.duration_s - (c.duration_s or 0)) > self._tol:
                continue
            sim = _similarity(artist, c_artist)
            if sim >= _ARTIST_GATE and sim > best_sim:
                best_sim, best = sim, c
        return None if best is None else (best, best_sim)


# --------------------------------------------------------------------------- #
# The cascade
# --------------------------------------------------------------------------- #
def _safe(fn, *args, **kwargs) -> list[YtCandidate]:
    """Run a search stage, swallowing failures so one bad call can't abort matching."""
    try:
        return list(fn(*args, **kwargs)) or []
    except Exception:
        return []


def _query(source: SongInfo) -> str:
    return f"{source.title} {source.artist}".strip()


def match(
    source: SongInfo,
    search: MatchSearch,
    *,
    auto_accept: float = AUTO_ACCEPT,
    review_floor: float = REVIEW_FLOOR,
    duration_tol_s: int = DURATION_TOL_S,
) -> MatchResult:
    """Resolve ``source`` to a YouTube Music candidate via the scored cascade."""
    if not source.title.strip() or not source.artist.strip():
        return MatchResult(
            source,
            MatchOutcome.NO_MATCH,
            None,
            MatchMethod.META,
            0.0,
            (),
            reason="missing title or artist",
        )

    scored: dict[str, tuple[float, YtCandidate]] = {}

    def consider(cands: list[YtCandidate]) -> None:
        for c in cands:
            if not c.video_id:
                continue
            s = score_candidate(source, c, duration_tol_s=duration_tol_s)
            prev = scored.get(c.video_id)
            if prev is None or s > prev[0]:
                scored[c.video_id] = (s, c)

    # Stage 1: album-scoped candidates (highest precision when album is known).
    if source.album.strip():
        consider(_safe(search.album_candidates, source.artist, source.album))

    # Stage 2: song search.
    q = _query(source)
    consider(_safe(search.search_songs, q))

    best_score = max((s for s, _ in scored.values()), default=0.0)

    # Stage 3: fall back to videos only if nothing convincing turned up.
    if best_score < auto_accept:
        consider(_safe(search.search_videos, q))

    if not scored:
        return MatchResult(
            source,
            MatchOutcome.NO_MATCH,
            None,
            MatchMethod.META,
            0.0,
            (),
            reason="no candidates found",
        )

    ranked = sorted(scored.values(), key=lambda sc: sc[0], reverse=True)
    best_score, best = ranked[0]
    top_candidates = tuple(c for _, c in ranked[:5])

    if best_score >= auto_accept:
        return MatchResult(
            source,
            MatchOutcome.AUTO,
            best,
            MatchMethod.META,
            best_score,
            top_candidates,
        )
    if best_score >= review_floor:
        return MatchResult(
            source,
            MatchOutcome.QUARANTINE,
            best,
            MatchMethod.FUZZY,
            best_score,
            top_candidates,
            reason="low_conf",
        )
    return MatchResult(
        source,
        MatchOutcome.QUARANTINE,
        best,
        MatchMethod.FUZZY,
        best_score,
        top_candidates,
        reason="ambiguous",
    )
