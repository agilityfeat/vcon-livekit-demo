"""Unit tests for the pure redaction + full↔redacted linking helpers.

Fully offline: canned dict inputs, no boto3 and no network (mirrors the
tests/test_enrichment.py style). These tests are meaningful: they fail if the
redaction logic, the structure/provenance preservation, or the link shape is
reverted. The last two tests load the COMMITTED viewer/samples fixtures and
assert they stay consistent with the helper so the static assets cannot drift.
"""

import json
import os

from vcon_agent.redaction import (
    LINK_TYPE_REDACTED,
    LINK_TYPE_REDACTED_FROM,
    REDACTION_MARKER,
    build_redacted_pair,
    link_redacted,
    redact_vcon,
)

_SAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer", "samples"
)


def _full_doc():
    """A canned FULL enriched vCon in the enriched shape (numeric timing)."""
    return {
        "vcon": "0.0.1",
        "uuid": "full-uuid-0001",
        "subject": "Patient intake call",
        "created_at": "2024-01-01T15:04:05+00:00",
        "parties": [
            {"name": "Dana Rivera", "role": "patient"},
            {"name": "Intake Agent", "role": "agent"},
        ],
        "dialog": [
            {
                "type": "text",
                "party": 1,
                "body": "What brings you in today?",
                "start": 0.0,
                "duration": 4.0,
                "mimetype": "text/plain",
            },
            {
                "type": "text",
                "party": 0,
                "body": "A headache for two days; I took ibuprofen.",
                "start": 4.0,
                "duration": 6.5,
                "mimetype": "text/plain",
            },
            {
                "type": "recording",
                "url": "recording.sample.ogg",
                "start": 0.0,
                "duration": 10.5,
                "mimetype": "audio/ogg",
            },
        ],
        "analysis": [
            {
                "type": "transcript",
                "vendor": "aws-transcribe-medical",
                "authoritative": True,
                "dialog": [0, 1],
                "body": "Agent: What brings you in today? Patient: A headache ...",
                "mimetype": "text/plain",
            },
            {
                "type": "entities",
                "vendor": "aws-comprehend-medical",
                "dialog": [1],
                "body": [
                    {"category": "MEDICAL_CONDITION", "text": "headache", "score": 0.99},
                    {"category": "MEDICATION", "text": "ibuprofen", "score": 0.98},
                ],
            },
            {
                "type": "summary",
                "vendor": "aws-bedrock",
                "dialog": [0, 1],
                "body": "Patient reports a two-day headache, self-treated with ibuprofen.",
                "mimetype": "text/plain",
            },
        ],
        "attachments": [
            {
                "type": "consent",
                "party": 0,
                "granted": True,
                "script": "This call will be recorded ...",
                "timestamp": "2024-01-01T15:04:05+00:00",
            }
        ],
    }


# ----------------------------------------------------------------------------
# redact_vcon: no mutation
# ----------------------------------------------------------------------------


def test_redact_vcon_does_not_mutate_input():
    full = _full_doc()
    before = json.dumps(full, sort_keys=True)
    redact_vcon(full, redacted_uuid="red-uuid-0001")
    after = json.dumps(full, sort_keys=True)
    assert before == after  # input untouched (deep copy inside)


# ----------------------------------------------------------------------------
# redact_vcon: PHI free text masked
# ----------------------------------------------------------------------------


def test_redact_masks_party_names():
    red = redact_vcon(_full_doc(), redacted_uuid="red-uuid-0001")
    assert [p["name"] for p in red["parties"]] == [REDACTION_MARKER, REDACTION_MARKER]
    # Role (non-PHI) is preserved so the viewer can still label speakers.
    assert [p["role"] for p in red["parties"]] == ["patient", "agent"]


def test_redact_masks_dialog_text_bodies_only():
    red = redact_vcon(_full_doc(), redacted_uuid="red-uuid-0001")
    text_turns = [d for d in red["dialog"] if d["type"] == "text"]
    assert all(t["body"] == REDACTION_MARKER for t in text_turns)
    # The recording dialog object's url is a storage reference, not PHI text.
    recording = [d for d in red["dialog"] if d["type"] == "recording"][0]
    assert recording["url"] == "recording.sample.ogg"


def test_redact_masks_transcript_and_summary_bodies():
    red = redact_vcon(_full_doc(), redacted_uuid="red-uuid-0001")
    by_type = {a["type"]: a for a in red["analysis"]}
    assert by_type["transcript"]["body"] == REDACTION_MARKER
    assert by_type["summary"]["body"] == REDACTION_MARKER


def test_redact_masks_consent_script():
    red = redact_vcon(_full_doc(), redacted_uuid="red-uuid-0001")
    consent = [a for a in red["attachments"] if a["type"] == "consent"][0]
    assert consent["script"] == REDACTION_MARKER
    # Structure around it is preserved.
    assert consent["granted"] is True
    assert consent["party"] == 0


# ----------------------------------------------------------------------------
# redact_vcon: structure / indices / provenance preserved
# ----------------------------------------------------------------------------


def test_redact_preserves_structure_and_timing():
    full = _full_doc()
    red = redact_vcon(full, redacted_uuid="red-uuid-0001")

    # Same version/subject/created_at.
    assert red["vcon"] == full["vcon"]
    assert red["subject"] == full["subject"]
    assert red["created_at"] == full["created_at"]

    # Same dialog count and order of types.
    assert len(red["dialog"]) == len(full["dialog"])
    assert [d["type"] for d in red["dialog"]] == [d["type"] for d in full["dialog"]]

    # Numeric start/duration preserved exactly (audio seek depends on this).
    for rd, fd in zip(red["dialog"], full["dialog"]):
        assert rd["start"] == fd["start"]
        assert isinstance(rd["start"], (int, float))
        assert rd["duration"] == fd["duration"]
        assert isinstance(rd["duration"], (int, float))
        assert rd.get("party") == fd.get("party")

    # Same analysis order/types and identical dialog turn-index provenance.
    assert [a["type"] for a in red["analysis"]] == [a["type"] for a in full["analysis"]]
    for ra, fa in zip(red["analysis"], full["analysis"]):
        assert ra["dialog"] == fa["dialog"]


def test_redact_keeps_entities_analysis_intact():
    red = redact_vcon(_full_doc(), redacted_uuid="red-uuid-0001")
    entities = [a for a in red["analysis"] if a["type"] == "entities"][0]
    cats = {e["category"] for e in entities["body"]}
    texts = {e["text"] for e in entities["body"]}
    assert cats == {"MEDICATION", "MEDICAL_CONDITION"}
    assert texts == {"ibuprofen", "headache"}


# ----------------------------------------------------------------------------
# linking: redacted uuid + back-reference
# ----------------------------------------------------------------------------


def test_redact_sets_distinct_uuid_and_backreference():
    full = _full_doc()
    red = redact_vcon(full, redacted_uuid="red-uuid-0001")
    assert red["uuid"] == "red-uuid-0001"
    assert red["uuid"] != full["uuid"]
    assert red["redacted"] == {"uuid": full["uuid"], "type": LINK_TYPE_REDACTED_FROM}


def test_redact_generates_uuid_when_not_given():
    red = redact_vcon(_full_doc())
    assert red["uuid"] and red["uuid"] != _full_doc()["uuid"]


def test_link_redacted_sets_forward_and_back_links_without_mutation():
    full = _full_doc()
    red = redact_vcon(full, redacted_uuid="red-uuid-0001")
    full_before = json.dumps(full, sort_keys=True)

    linked_full, linked_red = link_redacted(full, red)

    # Inputs not mutated.
    assert json.dumps(full, sort_keys=True) == full_before
    assert "redacted" not in full

    # Forward link on the full doc names the redacted uuid + relative url.
    assert linked_full["redacted"]["uuid"] == "red-uuid-0001"
    assert linked_full["redacted"]["type"] == LINK_TYPE_REDACTED
    assert linked_full["redacted"]["url"] == "vcon-redacted.sample.json"

    # Back-reference on the redacted doc names the full uuid.
    assert linked_red["redacted"]["uuid"] == full["uuid"]
    assert linked_red["redacted"]["type"] == LINK_TYPE_REDACTED_FROM


def test_build_redacted_pair_round_trips_links():
    full = _full_doc()
    linked_full, linked_red = build_redacted_pair(full, redacted_uuid="red-uuid-0001")
    assert linked_full["redacted"]["uuid"] == linked_red["uuid"]
    assert linked_red["redacted"]["uuid"] == linked_full["uuid"]


# ----------------------------------------------------------------------------
# committed fixtures stay consistent with the helper
# ----------------------------------------------------------------------------


def _load_sample(name):
    with open(os.path.join(_SAMPLES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def test_committed_fixtures_are_linked_and_consistent():
    full = _load_sample("vcon-full.sample.json")
    red = _load_sample("vcon-redacted.sample.json")

    # Full forward link references the redacted doc's uuid; back-reference agrees.
    assert full["redacted"]["uuid"] == red["uuid"]
    assert full["redacted"]["type"] == LINK_TYPE_REDACTED
    assert full["redacted"]["url"] == "vcon-redacted.sample.json"
    assert red["redacted"]["uuid"] == full["uuid"]
    assert red["redacted"]["type"] == LINK_TYPE_REDACTED_FROM
    assert red["uuid"] != full["uuid"]


def test_committed_full_fixture_has_numeric_timing_and_pinned_entities():
    full = _load_sample("vcon-full.sample.json")
    text_turns = [d for d in full["dialog"] if d.get("type") == "text"]
    assert text_turns and all(
        isinstance(d["start"], (int, float)) and isinstance(d["duration"], (int, float))
        for d in text_turns
    )
    # The entities analysis pins to a real text turn with a numeric start.
    entities = [a for a in full["analysis"] if a["type"] == "entities"][0]
    turn_index = entities["dialog"][0]
    pinned = full["dialog"][turn_index]
    assert pinned["type"] == "text"
    assert isinstance(pinned["start"], (int, float))


def test_committed_redacted_fixture_matches_helper_output():
    full = _load_sample("vcon-full.sample.json")
    red = _load_sample("vcon-redacted.sample.json")
    # Re-running the helper with the committed redacted uuid reproduces the file.
    _, regenerated = build_redacted_pair(full, redacted_uuid=red["uuid"])
    assert regenerated == red
