"""Unit tests for the pure enrichment shaping/provenance helpers.

Fully offline: no boto3 client and no network. Every helper is fed a canned
AWS-shaped dict so the logic the inline Lambdas mirror is exercised here. These
tests are meaningful: they fail if the corresponding logic is reverted.
"""

from vcon_agent.enrichment import (
    extract_min_entities,
    map_turns_by_timestamp_overlap,
    reconcile_segments_to_parties,
    s3_location_from_transcribe_uri,
    segments_from_transcribe_result,
    summary_prompt_from_dialog,
    summary_prompt_from_transcript,
    summary_text_from_bedrock_response,
    transcript_text_from_transcribe_result,
)


# ----------------------------------------------------------------------------
# map_turns_by_timestamp_overlap
# ----------------------------------------------------------------------------


def _turns(*starts):
    """Build dialog turns carrying only numeric ``start`` times."""
    return [{"type": "text", "party": 0, "body": "x", "start": s} for s in starts]


def test_overlap_maps_segment_to_covered_turns():
    # Turns occupy [0,10), [10,20), [20,inf). A segment spanning 5..15 overlaps
    # the first two turns.
    dialog = _turns(0, 10, 20)
    segments = [{"start": 5, "end": 15}]
    assert map_turns_by_timestamp_overlap(segments, dialog) == [[0, 1]]


def test_no_overlap_returns_empty_for_that_segment():
    dialog = _turns(0, 10)  # [0,10), [10,inf)
    # A segment before all turns overlaps nothing.
    segments = [{"start": -5, "end": -1}]
    assert map_turns_by_timestamp_overlap(segments, dialog) == [[]]


def test_boundary_is_half_open():
    # Turn 0 is [0,10), turn 1 is [10,20). A segment [10,12) touches the start of
    # turn 1 only (half-open), not the end of turn 0.
    dialog = _turns(0, 10, 20)
    segments = [{"start": 10, "end": 12}]
    assert map_turns_by_timestamp_overlap(segments, dialog) == [[1]]


def test_explicit_end_on_turn_is_respected():
    # When a turn carries an explicit end, a gap after it is honored.
    dialog = [
        {"type": "text", "start": 0, "end": 5},
        {"type": "text", "start": 20, "end": 25},
    ]
    # A segment in the gap (5..20) overlaps neither turn.
    assert map_turns_by_timestamp_overlap([{"start": 6, "end": 19}], dialog) == [[]]
    # A segment that straddles into turn 1 overlaps only turn 1.
    assert map_turns_by_timestamp_overlap([{"start": 6, "end": 21}], dialog) == [[1]]


def test_zero_length_segment_matches_containing_turn():
    dialog = _turns(0, 10, 20)
    assert map_turns_by_timestamp_overlap([{"start": 10, "end": 10}], dialog) == [[1]]


def test_turn_without_numeric_start_is_skipped():
    # A turn with an ISO (non-numeric) start cannot be time-aligned and is skipped.
    dialog = [
        {"type": "text", "start": "2024-01-01T00:00:00+00:00"},
        {"type": "text", "start": 10},
    ]
    assert map_turns_by_timestamp_overlap([{"start": 11, "end": 12}], dialog) == [[1]]


def test_multiple_segments_mapped_in_order():
    dialog = _turns(0, 10, 20)
    segments = [{"start": 1, "end": 2}, {"start": 21, "end": 22}]
    assert map_turns_by_timestamp_overlap(segments, dialog) == [[0], [2]]


# ----------------------------------------------------------------------------
# extract_min_entities
# ----------------------------------------------------------------------------


def test_extract_min_entities_filters_to_meds_and_conditions():
    response = {
        "Entities": [
            {"Category": "MEDICATION", "Text": "ibuprofen", "Score": 0.99},
            {"Category": "MEDICAL_CONDITION", "Text": "headache", "Score": 0.98},
            {"Category": "ANATOMY", "Text": "head", "Score": 0.9},
            {"Category": "PROTECTED_HEALTH_INFORMATION", "Text": "Jane", "Score": 0.9},
        ]
    }
    out = extract_min_entities(response)
    assert [e["category"] for e in out] == ["MEDICATION", "MEDICAL_CONDITION"]
    assert out[0]["text"] == "ibuprofen"
    assert out[0]["score"] == 0.99


def test_extract_min_entities_tolerates_missing_score():
    response = {"Entities": [{"Category": "MEDICATION", "Text": "aspirin"}]}
    out = extract_min_entities(response)
    assert out == [{"category": "MEDICATION", "text": "aspirin"}]
    assert "score" not in out[0]


def test_extract_min_entities_empty_when_no_entities():
    assert extract_min_entities({}) == []
    assert extract_min_entities({"Entities": []}) == []


# ----------------------------------------------------------------------------
# transcript_text_from_transcribe_result
# ----------------------------------------------------------------------------


def test_transcript_text_from_transcribe_result_single():
    result = {"results": {"transcripts": [{"transcript": "hello there"}]}}
    assert transcript_text_from_transcribe_result(result) == "hello there"


def test_transcript_text_from_transcribe_result_joins_multiple():
    result = {
        "results": {
            "transcripts": [{"transcript": "part one"}, {"transcript": "part two"}]
        }
    }
    assert transcript_text_from_transcribe_result(result) == "part one part two"


def test_transcript_text_from_transcribe_result_empty():
    assert transcript_text_from_transcribe_result({}) == ""
    assert transcript_text_from_transcribe_result({"results": {}}) == ""


# ----------------------------------------------------------------------------
# s3_location_from_transcribe_uri
# ----------------------------------------------------------------------------


def test_s3_location_from_s3_scheme_uri():
    assert s3_location_from_transcribe_uri(
        "s3://my-bucket/medical/job.json"
    ) == ("my-bucket", "medical/job.json")


def test_s3_location_from_path_style_https_uri():
    # Path style: s3.<region>.amazonaws.com/<bucket>/<key>
    assert s3_location_from_transcribe_uri(
        "https://s3.us-east-1.amazonaws.com/my-bucket/medical/job.json"
    ) == ("my-bucket", "medical/job.json")


def test_s3_location_from_virtual_hosted_https_uri():
    # Virtual-hosted style: <bucket>.s3.<region>.amazonaws.com/<key>
    assert s3_location_from_transcribe_uri(
        "https://my-bucket.s3.us-east-1.amazonaws.com/medical/job.json"
    ) == ("my-bucket", "medical/job.json")


def test_s3_location_strips_query_string():
    assert s3_location_from_transcribe_uri(
        "https://s3.us-east-1.amazonaws.com/my-bucket/medical/job.json?X-Amz-Signature=abc"
    ) == ("my-bucket", "medical/job.json")


def test_s3_location_returns_none_for_unrecognized():
    assert s3_location_from_transcribe_uri("") is None
    assert s3_location_from_transcribe_uri(None) is None
    assert s3_location_from_transcribe_uri("s3://only-bucket") is None
    assert s3_location_from_transcribe_uri("https://example.com/no/s3/here") is None


# ----------------------------------------------------------------------------
# summary_prompt_from_dialog
# ----------------------------------------------------------------------------


def test_summary_prompt_labels_speakers_and_skips_non_text():
    dialog = [
        {"type": "text", "party": 1, "body": "What brings you in?"},
        {"type": "text", "party": 0, "body": "A headache for two days."},
        {"type": "recording", "url": "s3://b/k/recording.ogg"},
    ]
    prompt = summary_prompt_from_dialog(dialog)
    assert "Agent: What brings you in?" in prompt
    assert "Patient: A headache for two days." in prompt
    # The recording dialog object is not spoken content and must be skipped.
    assert "recording.ogg" not in prompt
    # The instruction framing is present and deterministic.
    assert prompt.startswith("You are a clinical documentation assistant.")
    assert prompt.rstrip().endswith("Summary:")


# ----------------------------------------------------------------------------
# summary_prompt_from_transcript
# ----------------------------------------------------------------------------


def test_summary_prompt_from_transcript_contains_transcript_text():
    # The authoritative transcript blob must appear verbatim in the prompt, so a
    # clinical term the live STT could mis-hear (e.g. a medication name) is
    # carried through correctly.
    transcript = "Patient has been taking sertraline for anxiety for two weeks."
    prompt = summary_prompt_from_transcript(transcript)
    assert transcript in prompt
    assert "sertraline" in prompt


def test_summary_prompt_from_transcript_carries_physician_instruction():
    prompt = summary_prompt_from_transcript("some transcript text")
    assert prompt.startswith("You are a clinical documentation assistant.")
    assert prompt.rstrip().endswith("Summary:")


def test_summary_prompt_from_transcript_does_not_fabricate_speaker_labels():
    # The authoritative blob has no speaker attribution; the builder must take a
    # plain string and NOT invent Patient:/Agent: labels or depend on dialog
    # turns.
    transcript = "Chief complaint is a headache for two days. Taking ibuprofen."
    prompt = summary_prompt_from_transcript(transcript)
    assert "Patient:" not in prompt
    assert "Agent:" not in prompt


def test_summary_prompt_from_transcript_shares_instruction_with_dialog_builder():
    # Both builders wrap their body with the same instruction preamble so the
    # wording cannot drift between the two prompt sources.
    preamble = summary_prompt_from_dialog([]).split("\n\nConversation:\n")[0]
    transcript_preamble = summary_prompt_from_transcript("x").split(
        "\n\nConversation:\n"
    )[0]
    assert preamble == transcript_preamble


# ----------------------------------------------------------------------------
# summary_text_from_bedrock_response
# ----------------------------------------------------------------------------


def test_summary_text_from_anthropic_messages_shape():
    response = {"content": [{"type": "text", "text": "A concise summary."}]}
    assert summary_text_from_bedrock_response(response) == "A concise summary."


def test_summary_text_from_legacy_completion_shape():
    assert (
        summary_text_from_bedrock_response({"completion": "  legacy text  "})
        == "legacy text"
    )


def test_summary_text_from_outputs_shape():
    response = {"outputs": [{"text": "titan text"}]}
    assert summary_text_from_bedrock_response(response) == "titan text"


def test_summary_text_from_bedrock_response_empty():
    assert summary_text_from_bedrock_response({}) == ""


# ----------------------------------------------------------------------------
# segments_from_transcribe_result
# ----------------------------------------------------------------------------


def _word(content, start, end):
    """Build a Transcribe-shaped WORD item carrying start/end times."""
    return {
        "start_time": str(start),
        "end_time": str(end),
        "alternatives": [{"content": content}],
    }


def _punct(content):
    """Build a Transcribe-shaped PUNCTUATION item (no timestamps)."""
    return {"type": "punctuation", "alternatives": [{"content": content}]}


def _speaker_segment(label, start, end):
    """Build a results.speaker_labels.segments[] entry."""
    return {"speaker_label": label, "start_time": str(start), "end_time": str(end)}


def test_segments_two_speakers_group_words_and_punctuation():
    # Two speaker windows: spk_0 over [0, 2], spk_1 over [2, 4]. Words carry
    # numeric start/end; a punctuation item (no timestamps) must glue onto the
    # preceding word with NO leading space, and words join with a single space.
    result = {
        "results": {
            "transcripts": [{"transcript": "I have a headache. How long?"}],
            "items": [
                _word("I", 0.0, 0.3),
                _word("have", 0.4, 0.6),
                _word("a", 0.7, 0.8),
                _word("headache", 0.9, 1.5),
                _punct("."),
                _word("How", 2.1, 2.4),
                _word("long", 2.5, 2.9),
                _punct("?"),
            ],
            "speaker_labels": {
                "segments": [
                    _speaker_segment("spk_0", 0.0, 2.0),
                    _speaker_segment("spk_1", 2.0, 4.0),
                ]
            },
        }
    }
    segs = segments_from_transcribe_result(result)
    assert len(segs) == 2

    assert segs[0]["speaker"] == "spk_0"
    assert segs[0]["start"] == 0.0
    assert segs[0]["end"] == 2.0
    # Words space-joined, punctuation glued onto the preceding word.
    assert segs[0]["text"] == "I have a headache."

    assert segs[1]["speaker"] == "spk_1"
    assert segs[1]["start"] == 2.0
    assert segs[1]["end"] == 4.0
    assert segs[1]["text"] == "How long?"


def test_segments_fallback_to_flat_transcript_when_no_speaker_labels():
    # No speaker_labels -> a single spk_0 segment whose text is the flat transcript.
    result = {
        "results": {
            "transcripts": [{"transcript": "hello there"}],
            "items": [_word("hello", 0.0, 0.5), _word("there", 0.6, 1.0)],
        }
    }
    segs = segments_from_transcribe_result(result)
    assert segs == [
        {"speaker": "spk_0", "start": None, "end": None, "text": "hello there"}
    ]


def test_segments_last_resort_uses_flat_transcript_when_items_missing():
    # A speaker segment exists but results.items is absent: fall back to the flat
    # transcript text for that segment rather than emitting empty text.
    result = {
        "results": {
            "transcripts": [{"transcript": "whole call text"}],
            "speaker_labels": {"segments": [_speaker_segment("spk_0", 0.0, 3.0)]},
        }
    }
    segs = segments_from_transcribe_result(result)
    assert len(segs) == 1
    assert segs[0]["speaker"] == "spk_0"
    assert segs[0]["text"] == "whole call text"


def test_segments_empty_input_yields_single_empty_segment():
    # Garbage / empty input: no speaker_labels and no transcript -> one spk_0
    # segment with empty text (the documented single-segment fallback).
    segs = segments_from_transcribe_result({})
    assert segs == [{"speaker": "spk_0", "start": None, "end": None, "text": ""}]


# ----------------------------------------------------------------------------
# reconcile_segments_to_parties
# ----------------------------------------------------------------------------


# Order-based mapping (option 2a): the speaker label whose FIRST segment starts
# earliest is the AGENT (party 1), the other distinct label is the PATIENT
# (party 0). dialog_turns is accepted but intentionally IGNORED (party is derived
# from Transcribe segment order, not from cross-clock time overlap). A dummy
# dialog is passed to exercise that it has no effect on the outcome.
_DUMMY_DIALOG = [
    {"type": "text", "party": 0, "start": "2024-01-01T00:00:00+00:00"},
    {"type": "text", "party": 1, "start": "2024-01-01T00:00:05+00:00"},
]


def test_reconcile_assigns_parties_by_speaking_order():
    # spk_0 speaks first (earliest start) -> agent (1); spk_1 later -> patient (0).
    # The spk_N label and text/timing are preserved EXACTLY on each turn.
    segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 4.0, "text": "What brings you in?"},
        {"speaker": "spk_1", "start": 4.0, "end": 6.0, "text": "I have a headache."},
        {"speaker": "spk_0", "start": 7.5, "end": 9.0, "text": "How long?"},
    ]
    turns = reconcile_segments_to_parties(segments, _DUMMY_DIALOG)
    assert [t["party"] for t in turns] == [1, 0, 1]
    assert [t["speaker"] for t in turns] == ["spk_0", "spk_1", "spk_0"]
    assert turns[1]["text"] == "I have a headache."
    assert turns[0]["start"] == 0.0 and turns[0]["end"] == 4.0


def test_reconcile_maps_by_order_not_label_name():
    # ORDER, NOT LABEL NAME: here spk_1 has the EARLIEST start and spk_0 comes
    # later, so spk_1 must resolve to agent (1) and spk_0 to patient (0). This
    # proves the mapping keys off speaking order, not the numeric label suffix.
    segments = [
        {"speaker": "spk_1", "start": 0.0, "end": 3.0, "text": "What brings you in?"},
        {"speaker": "spk_0", "start": 3.0, "end": 6.0, "text": "I have a headache."},
    ]
    turns = reconcile_segments_to_parties(segments, _DUMMY_DIALOG)
    by_speaker = {t["speaker"]: t["party"] for t in turns}
    assert by_speaker["spk_1"] == 1  # earliest -> agent
    assert by_speaker["spk_0"] == 0  # later -> patient


def test_reconcile_single_speaker_yields_party_none():
    # Diarization collapsed to ONE distinct label: no order can be established,
    # so every turn's party is None and the spk label survives (generic
    # 'Speaker N' fallback). We do NOT guess agent vs. patient.
    segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 3.0, "text": "a"},
        {"speaker": "spk_0", "start": 3.0, "end": 6.0, "text": "b"},
    ]
    turns = reconcile_segments_to_parties(segments, _DUMMY_DIALOG)
    assert [t["party"] for t in turns] == [None, None]
    assert [t["speaker"] for t in turns] == ["spk_0", "spk_0"]


def test_reconcile_more_than_two_labels_earliest_agent_rest_patient():
    # Documented behavior for >2 distinct labels in a two-party intake: the single
    # earliest label -> agent (1), ALL other distinct labels -> patient (0).
    segments = [
        {"speaker": "spk_1", "start": 0.0, "end": 2.0, "text": "a"},  # earliest
        {"speaker": "spk_0", "start": 2.0, "end": 4.0, "text": "b"},
        {"speaker": "spk_2", "start": 4.0, "end": 6.0, "text": "c"},
    ]
    turns = reconcile_segments_to_parties(segments, _DUMMY_DIALOG)
    by_speaker = {t["speaker"]: t["party"] for t in turns}
    assert by_speaker["spk_1"] == 1
    assert by_speaker["spk_0"] == 0
    assert by_speaker["spk_2"] == 0


def test_reconcile_tie_on_earliest_yields_party_none():
    # Two labels share the SAME earliest first-start instant -> the order is
    # ambiguous, so the tied labels map to None rather than guessing.
    segments = [
        {"speaker": "spk_0", "start": 0.0, "end": 2.0, "text": "a"},
        {"speaker": "spk_1", "start": 0.0, "end": 2.0, "text": "b"},
    ]
    turns = reconcile_segments_to_parties(segments, _DUMMY_DIALOG)
    assert [t["party"] for t in turns] == [None, None]
    assert [t["speaker"] for t in turns] == ["spk_0", "spk_1"]


def test_reconcile_unorderable_start_yields_party_none():
    # A label whose first segment start is not a usable number (ISO string ->
    # None via _as_float) is unorderable and maps to None; the other, orderable
    # label still resolves (it is the only orderable label, so it is the earliest
    # -> agent).
    segments = [
        {"speaker": "spk_0", "start": "2024-01-01T00:00:00+00:00", "end": None,
         "text": "a"},
        {"speaker": "spk_1", "start": 1.0, "end": 2.0, "text": "b"},
    ]
    turns = reconcile_segments_to_parties(segments, _DUMMY_DIALOG)
    by_speaker = {t["speaker"]: t["party"] for t in turns}
    assert by_speaker["spk_0"] is None  # unorderable
    assert by_speaker["spk_1"] == 1  # only orderable label -> earliest -> agent
