"""Pure prompt-building / verdict-parsing tests for the batched LLM quarantine judge."""

from __future__ import annotations

import json

from s2y.core.classify import build_batch_prompt, parse_batch_verdicts
from s2y.core.models import ClassifyDecision, ClassifyItem, SongInfo, YtCandidate

CANDS_A = (
    YtCandidate("vA1", "Archangel", "Burial", album="Untrue", duration_s=251),
    YtCandidate("vA2", "Archangel (slowed)", "Burial", duration_s=330, is_song=False),
)
CANDS_B = (YtCandidate("vB1", "Ghost Voices", "Virtual Self", duration_s=300),)

ITEM_A = ClassifyItem(SongInfo("Archangel", "Burial", "Untrue", duration_ms=251000), CANDS_A)
ITEM_B = ClassifyItem(SongInfo("Ghost Voices", "Virtual Self", duration_ms=300000), CANDS_B)


def _one(obj: dict, candidates=CANDS_A):
    """Parse a single-element batch (index 1) and return just that verdict."""
    item = ClassifyItem(SongInfo("Archangel", "Burial", "Untrue", duration_ms=251000), candidates)
    return parse_batch_verdicts(json.dumps([{**obj, "index": 1}]), (item,))[0]


# --------------------------------------------------------------------------- #
# build_batch_prompt
# --------------------------------------------------------------------------- #
def test_build_batch_prompt_renders_all_tracks_and_candidates():
    prompt = build_batch_prompt((ITEM_A, ITEM_B))
    # Both source tracks are present, each under a numbered TRACK header…
    assert "TRACK [1]" in prompt and "TRACK [2]" in prompt
    assert "Archangel" in prompt and "Ghost Voices" in prompt
    assert "Untrue" in prompt
    assert "4:11" in prompt  # 251s formatted mm:ss
    # …and every candidate's video_id is shown so the model can copy it verbatim.
    assert "video_id=vA1" in prompt and "video_id=vA2" in prompt
    assert "video_id=vB1" in prompt
    # It demands a strict JSON array keyed by index.
    assert "JSON array" in prompt
    assert '"decision"' in prompt and '"index"' in prompt
    assert "2 source track(s)" in prompt


def test_build_batch_prompt_handles_unknown_duration_and_album():
    item = ClassifyItem(SongInfo("Mystery", "Anon"), (YtCandidate("v9", "Mystery", "Anon"),))
    prompt = build_batch_prompt((item,))
    assert "unknown" in prompt  # no album / no duration both render as "unknown"


def test_build_batch_prompt_marks_track_with_no_candidates():
    item = ClassifyItem(SongInfo("Lost", "Nobody"), ())
    prompt = build_batch_prompt((item,))
    assert "none found" in prompt


# --------------------------------------------------------------------------- #
# parse_batch_verdicts — single-element happy paths
# --------------------------------------------------------------------------- #
def test_parse_accept():
    v = _one({"decision": "accept", "video_id": "vA1", "confidence": 0.9,
              "reasoning": "same recording"})
    assert v.decision is ClassifyDecision.ACCEPT
    assert v.video_id == "vA1"
    assert v.confidence == 0.9
    assert v.reasoning == "same recording"


def test_parse_reject():
    v = _one({"decision": "reject", "video_id": None, "confidence": 0.8})
    assert v.decision is ClassifyDecision.REJECT
    assert v.video_id is None
    assert v.confidence == 0.8


# --------------------------------------------------------------------------- #
# parse_batch_verdicts — robustness (per-element rules)
# --------------------------------------------------------------------------- #
def test_parse_code_fenced_array():
    text = '```json\n[{"index":1,"decision":"accept","video_id":"vA2","confidence":0.7}]\n```'
    v = parse_batch_verdicts(text, (ITEM_A,))[0]
    assert v.decision is ClassifyDecision.ACCEPT and v.video_id == "vA2"


def test_parse_prose_wrapped_and_trailing_text():
    text = (
        "Sure, here's my answer:\n"
        '[{"index": 1, "decision": "reject", "video_id": null, "confidence": 0.6}]\n'
        "Hope that helps!"
    )
    v = parse_batch_verdicts(text, (ITEM_A,))[0]
    assert v.decision is ClassifyDecision.REJECT and v.confidence == 0.6


def test_parse_hallucinated_video_id_is_uncertain():
    v = _one({"decision": "accept", "video_id": "v999", "confidence": 0.99})
    assert v.decision is ClassifyDecision.UNCERTAIN
    assert v.video_id is None


def test_parse_accept_without_video_id_is_uncertain():
    v = _one({"decision": "accept", "video_id": None, "confidence": 0.9})
    assert v.decision is ClassifyDecision.UNCERTAIN


def test_parse_reject_forces_no_video_id():
    # Even if the model names a candidate while rejecting, we don't carry it.
    v = _one({"decision": "reject", "video_id": "vA1", "confidence": 0.5})
    assert v.decision is ClassifyDecision.REJECT and v.video_id is None


def test_parse_unknown_decision_is_uncertain():
    v = _one({"decision": "maybe", "video_id": "vA1", "confidence": 0.9})
    assert v.decision is ClassifyDecision.UNCERTAIN


def test_parse_garbage_is_uncertain():
    for text in ["", "not json at all", "[broken", "null", "{}", "   "]:
        verdicts = parse_batch_verdicts(text, (ITEM_A,))
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v.decision is ClassifyDecision.UNCERTAIN
        assert v.confidence == 0.0
        assert v.video_id is None


def test_parse_confidence_is_clamped():
    assert _one({"decision": "reject", "confidence": 1.7}).confidence == 1.0
    assert _one({"decision": "reject", "confidence": -1}).confidence == 0.0
    assert _one({"decision": "reject", "confidence": "high"}).confidence == 0.0


def test_parse_reasoning_is_truncated():
    v = _one({"decision": "reject", "confidence": 0.5, "reasoning": "x" * 1000})
    assert len(v.reasoning) <= 280


# --------------------------------------------------------------------------- #
# parse_batch_verdicts — batch semantics
# --------------------------------------------------------------------------- #
def test_returns_one_verdict_per_item_routed_by_index():
    items = (ITEM_A, ITEM_B)
    # Out-of-order array; each element validated against ITS OWN item's candidates.
    text = json.dumps([
        {"index": 2, "decision": "accept", "video_id": "vB1", "confidence": 0.88},
        {"index": 1, "decision": "accept", "video_id": "vA1", "confidence": 0.91},
    ])
    out = parse_batch_verdicts(text, items)
    assert len(out) == 2
    assert out[0].decision is ClassifyDecision.ACCEPT and out[0].video_id == "vA1"
    assert out[1].decision is ClassifyDecision.ACCEPT and out[1].video_id == "vB1"


def test_cross_item_video_id_is_rejected_per_item():
    # Element for track 1 names track 2's video_id — not in track 1's candidates.
    text = json.dumps([{"index": 1, "decision": "accept", "video_id": "vB1", "confidence": 0.9}])
    out = parse_batch_verdicts(text, (ITEM_A, ITEM_B))
    assert out[0].decision is ClassifyDecision.UNCERTAIN
    assert out[1].decision is ClassifyDecision.UNCERTAIN  # track 2 had no element


def test_missing_index_is_uncertain_others_unaffected():
    text = json.dumps([
        {"index": 1, "decision": "accept", "video_id": "vA1", "confidence": 0.9},
        # index 2 omitted entirely
    ])
    out = parse_batch_verdicts(text, (ITEM_A, ITEM_B))
    assert out[0].decision is ClassifyDecision.ACCEPT
    assert out[1].decision is ClassifyDecision.UNCERTAIN


def test_duplicate_index_is_uncertain():
    # Two elements claim track 1 — ambiguous, so we don't trust either.
    text = json.dumps([
        {"index": 1, "decision": "accept", "video_id": "vA1", "confidence": 0.9},
        {"index": 1, "decision": "reject", "video_id": None, "confidence": 0.4},
    ])
    out = parse_batch_verdicts(text, (ITEM_A,))
    assert len(out) == 1 and out[0].decision is ClassifyDecision.UNCERTAIN


def test_positional_fallback_when_no_indices_present():
    # A well-formed array of the right length with no usable index keys is mapped
    # positionally (per-item video_id validation still guards correctness).
    text = json.dumps([
        {"decision": "accept", "video_id": "vA1", "confidence": 0.9},
        {"decision": "accept", "video_id": "vB1", "confidence": 0.8},
    ])
    out = parse_batch_verdicts(text, (ITEM_A, ITEM_B))
    assert out[0].video_id == "vA1" and out[1].video_id == "vB1"


def test_length_always_matches_items():
    # More elements than items, and fewer — output length tracks the item count.
    long = json.dumps([{"index": i, "decision": "reject", "confidence": 0.5}
                       for i in range(1, 6)])
    assert len(parse_batch_verdicts(long, (ITEM_A, ITEM_B))) == 2
    short = json.dumps([{"index": 1, "decision": "reject", "confidence": 0.5}])
    assert len(parse_batch_verdicts(short, (ITEM_A, ITEM_B))) == 2


def test_empty_items_returns_empty():
    assert parse_batch_verdicts("[]", ()) == ()
    assert parse_batch_verdicts("anything", ()) == ()
