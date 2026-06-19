"""Pure prompt-building and verdict-parsing for the LLM quarantine judge.

This module is the *pure* half of the ``claude -p`` classifier: it turns a batch of
quarantined tracks (each with its candidate list) into a single prompt string, and
turns the model's text reply — a JSON array — back into a tuple of typed
:class:`ClassifyVerdict`, one per track. It performs no I/O — the subprocess lives in
:mod:`s2y.services.claude_classify` — so it is exhaustively unit-testable.

:func:`parse_batch_verdicts` is the single trust boundary for model output: it never
raises, returns exactly one verdict per input item, and validates each chosen
``video_id`` against the candidates actually shown *for that track* (so a hallucinated
or misindexed id can't resolve a quarantine), clamps confidence, and collapses
anything it can't make sense of into an ``UNCERTAIN`` verdict.
"""

from __future__ import annotations

import json

from .models import ClassifyDecision, ClassifyItem, ClassifyVerdict, YtCandidate

_MAX_REASONING = 280  # keep model rationale short for logs / the TUI


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


_RULES = [
    "Rules:",
    "- A live, acoustic, remix, sped-up/slowed, cover, or instrumental version is",
    "  NOT a match for the studio original (and vice-versa).",
    "- A length difference of more than a few seconds means a different recording.",
    "- A different artist is not a match, even if the title is identical.",
    '- When unsure, prefer "reject" over a wrong match.',
]


def _candidate_line(label: str, c: YtCandidate) -> str:
    kind = "song" if c.is_song else "video"
    return (
        f'    [{label}] video_id={c.video_id} | "{c.title}" — {c.artist} | '
        f"album: {c.album or 'unknown'} | length: {_fmt_duration(c.duration_s)} | {kind}"
    )


def build_batch_prompt(items: tuple[ClassifyItem, ...]) -> str:
    """Render the single-shot instruction for a whole batch of quarantined tracks.

    Each track is given a stable 1-based ``index``; the model must return one JSON
    object per track (keyed back by that index) choosing a candidate or rejecting.
    Embeds s2y's own matching philosophy (a live/remix/cover/sped-up take is a
    *different* recording; a large length gap means a different recording) so the
    judge applies the same bar the scorer does, and demands the chosen ``video_id``
    be copied verbatim from one of *that track's* candidates.
    """
    n = len(items)
    lines = [
        "You are matching songs from one music service to the correct recording on "
        "another. For EACH source track below, decide which candidate, if any, is the "
        "SAME RECORDING as the source track.",
        "",
        *_RULES,
        "",
        f"There are {n} source track(s), numbered [1]..[{n}].",
    ]
    for idx, item in enumerate(items, 1):
        src = item.source
        lines += [
            "",
            "========================================",
            f"TRACK [{idx}]",
            "  Source (from Spotify):",
            f"    Title:  {src.title}",
            f"    Artist: {src.artist}",
            f"    Album:  {src.album or 'unknown'}",
            f"    Length: {_fmt_duration(src.duration_s)}",
            "  Candidates (from YouTube Music):",
        ]
        if item.candidates:
            for j, c in enumerate(item.candidates, 1):
                lines.append(_candidate_line(f"{idx}.{j}", c))
        else:
            lines.append("    (none found — you can only reject this track)")
    lines += [
        "",
        "========================================",
        "Respond with ONLY a JSON array — no prose, no code fence — containing exactly "
        "one object per track:",
        '[{"index": <track number>, "decision": "accept" | "reject", '
        '"video_id": "<id of the chosen candidate, or null>", '
        '"confidence": <number 0.0-1.0>, "reasoning": "<one short sentence>"}, ...]',
        '- "index" MUST equal the track number ([1]..[N]) shown above.',
        '- "video_id" MUST be copied exactly from one of THAT track\'s candidates, '
        "or null when you reject.",
        "- Include every track exactly once.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing (robust; never raises)
# --------------------------------------------------------------------------- #
def _extract_json_array(text: str) -> list | None:
    """Return the first balanced ``[...]`` array in ``text`` as a list, or None.

    Tolerates surrounding prose and `````json`` fences by scanning for the first
    bracket and tracking depth (while respecting string literals so a ``]`` inside a
    value can't end the array early). Braces inside don't affect bracket depth.
    """
    if not text:
        return None
    try:  # the happy path: the whole reply is the array
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass

    start = text.find("[")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break  # malformed; try the next '['
                    return obj if isinstance(obj, list) else None
        start = text.find("[", start + 1)
    return None


def _clamp_confidence(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _uncertain() -> ClassifyVerdict:
    return ClassifyVerdict(ClassifyDecision.UNCERTAIN, None, 0.0)


def _verdict_from_obj(obj, candidates: tuple[YtCandidate, ...]) -> ClassifyVerdict:
    """Turn one decision object into a typed verdict — the per-track trust boundary.

    An unknown decision, or an ACCEPT whose ``video_id`` is not among *these*
    candidates, collapses to ``UNCERTAIN`` — so a confused or hallucinating model
    leaves the track quarantined rather than mis-resolving it.
    """
    if not isinstance(obj, dict):
        return _uncertain()

    decision_raw = str(obj.get("decision", "")).strip().lower()
    confidence = _clamp_confidence(obj.get("confidence"))
    reasoning = str(obj.get("reasoning", "") or "")[:_MAX_REASONING]

    if decision_raw == "reject":
        return ClassifyVerdict(ClassifyDecision.REJECT, None, confidence, reasoning)

    if decision_raw == "accept":
        video_id = obj.get("video_id")
        valid_ids = {c.video_id for c in candidates}
        if isinstance(video_id, str) and video_id in valid_ids:
            return ClassifyVerdict(ClassifyDecision.ACCEPT, video_id, confidence, reasoning)
        # Hallucinated / missing id — don't trust an accept we can't verify.
        return _uncertain()

    return _uncertain()


def parse_batch_verdicts(text: str, items: tuple[ClassifyItem, ...]) -> tuple[ClassifyVerdict, ...]:
    """Parse the model's JSON-array reply into one verdict per item. Never raises.

    Returns a tuple of exactly ``len(items)`` verdicts, positionally aligned with the
    input. Elements are matched to items by their ``index`` field (1-based); an item
    with no matching element, an ambiguous duplicate, or unparseable output becomes
    ``UNCERTAIN``. Each matched element is validated against *its own* item's
    candidates, so a misindexed or hallucinated ``video_id`` cannot resolve the wrong
    track.
    """
    n = len(items)
    if n == 0:
        return ()

    arr = _extract_json_array(text)
    if not arr:
        return tuple(_uncertain() for _ in range(n))

    # Bucket elements by their (in-range) index. First collect, then assemble.
    buckets: dict[int, list] = {}
    for elem in arr:
        if not isinstance(elem, dict):
            continue
        raw = elem.get("index")
        if not isinstance(raw, (int, float, str)):
            continue
        try:
            i = int(raw)
        except ValueError:
            continue
        if 1 <= i <= n:
            buckets.setdefault(i, []).append(elem)

    # Fallback: a well-formed array with no usable indices but the right length is
    # mapped positionally (per-track video_id validation still guards correctness).
    if not buckets and len(arr) == n and all(isinstance(e, dict) for e in arr):
        return tuple(_verdict_from_obj(arr[pos], items[pos].candidates) for pos in range(n))

    out = []
    for pos in range(n):
        bucket = buckets.get(pos + 1, [])
        if len(bucket) == 1:  # exactly one element claimed this index
            out.append(_verdict_from_obj(bucket[0], items[pos].candidates))
        else:  # missing (0) or ambiguous duplicate (>1)
            out.append(_uncertain())
    return tuple(out)
