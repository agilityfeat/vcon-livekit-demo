"""Unit tests for the pure enrichment shaping/provenance helpers.

Fully offline: no boto3 client and no network. Every helper is fed a canned
AWS-shaped dict so the logic the inline Lambdas mirror is exercised here. These
tests are meaningful: they fail if the corresponding logic is reverted.
"""

from vcon_agent.enrichment import (
    extract_min_entities,
    map_turns_by_timestamp_overlap,
    s3_location_from_transcribe_uri,
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
