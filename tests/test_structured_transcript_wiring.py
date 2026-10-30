"""Offline wiring tests for the structured-authoritative-transcript mirror.

FEAT-003 mirrors the FEAT-002 pure core (enrichment.segments_from_transcribe_result
+ enrichment.reconcile_segments_to_parties, stored on the authoritative transcript
analysis entry) into the inline ``Code.ZipFile`` Lambda bodies in
``infrastructure/template.yaml``. Those inline bodies cannot import the repo
package at AWS runtime, so this module guards the HAND-MIRROR: it extracts the
edited inline bodies as text and validates them against the authoritative pure
core.

Two kinds of check live here:

  * COMPILE-ONLY (always runs in-sandbox, needs no boto3): every edited inline
    body compiles. ``compile()`` does not execute the module-level ``import
    boto3`` at the top of several bodies, so this runs even where boto3 is not
    installed.
  * BEHAVIORAL PARITY: the Merge body imports NO boto3, so it is extractable and
    exec-able offline (via ``lambda service: None``); its reconciliation output
    is asserted byte-identical to the pure core on a shared canned input, and the
    authoritative-transcript ``turns``/flat-body/order behavior is asserted
    directly. The GetTranscription body imports boto3 at module top, so its
    exec-based behavioral test is guarded behind ``pytest.importorskip('boto3')``
    (mirroring tests/test_seal_pipeline_wiring.py); it skips cleanly in-sandbox.
"""

import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_REPO_ROOT, "infrastructure", "template.yaml")

# The five inline bodies FEAT-003 edits.
_EDITED_BODIES = [
    "StartTranscriptionFunction",
    "GetTranscriptionFunction",
    "ComprehendFunction",
    "BedrockFunction",
    "MergeFunction",
]


def _extract_zipfile_body(function_name):
    """Pull one inline ``Code.ZipFile`` body for a named CFN resource.

    Parses the template as text (not YAML) so the block-scalar body is captured
    verbatim with its original indentation, then dedents it. This is the same
    approach used by tests/test_seal_pipeline_wiring.py::_extract_zipfile_body,
    duplicated here so this module has no boto3/jwcrypto import gate at the top.
    """
    with open(_TEMPLATE) as fh:
        lines = fh.readlines()

    in_resource = False
    resource_indent = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not in_resource:
            if re.match(r"^\s*{}:\s*$".format(re.escape(function_name)), line):
                in_resource = True
                resource_indent = indent
            i += 1
            continue
        if stripped and indent <= resource_indent:
            break
        if stripped == "ZipFile: |":
            body_indent = indent + 2
            body_lines = []
            i += 1
            while i < n:
                bl = lines[i]
                if bl.strip() == "":
                    body_lines.append("")
                    i += 1
                    continue
                bl_indent = len(bl) - len(bl.lstrip())
                if bl_indent < body_indent:
                    break
                body_lines.append(bl[body_indent:].rstrip("\n"))
                i += 1
            return "\n".join(body_lines)
        i += 1
    raise AssertionError("ZipFile body not found for {}".format(function_name))


def _exec_body(function_name, client_factory=None):
    """Compile+exec an inline body, returning its module namespace.

    ``client_factory`` is only needed for bodies that call ``boto3.client`` at
    module top (StartTranscription / GetTranscription / Comprehend / Bedrock).
    The Merge body imports no boto3 and execs with ``client_factory=None``.
    """
    body = _extract_zipfile_body(function_name)
    namespace = {}
    if client_factory is not None:
        import boto3

        original = boto3.client
        boto3.client = client_factory
        try:
            exec(compile(body, "<{}>".format(function_name), "exec"), namespace)
        finally:
            boto3.client = original
    else:
        exec(compile(body, "<{}>".format(function_name), "exec"), namespace)
    return namespace


# --- canned Transcribe-Medical-shaped fixtures (shared with the pure core) ----


def _canned_result():
    """A two-speaker Transcribe-Medical result over a single sentence.

    spk_0 covers "I have a headache" and spk_1 "for three days." so the two
    segments carry distinct, correctly-grouped text with the trailing period
    glued onto the preceding word (no leading space).
    """
    return {
        "results": {
            "transcripts": [{"transcript": "I have a headache for three days."}],
            "items": [
                {"start_time": "0.0", "end_time": "0.2",
                 "alternatives": [{"content": "I"}]},
                {"start_time": "0.2", "end_time": "0.5",
                 "alternatives": [{"content": "have"}]},
                {"start_time": "0.5", "end_time": "0.7",
                 "alternatives": [{"content": "a"}]},
                {"start_time": "0.7", "end_time": "1.0",
                 "alternatives": [{"content": "headache"}]},
                {"start_time": "1.2", "end_time": "1.4",
                 "alternatives": [{"content": "for"}]},
                {"start_time": "1.4", "end_time": "1.7",
                 "alternatives": [{"content": "three"}]},
                {"start_time": "1.7", "end_time": "2.0",
                 "alternatives": [{"content": "days"}]},
                {"type": "punctuation", "alternatives": [{"content": "."}]},
            ],
            "speaker_labels": {
                "segments": [
                    {"speaker_label": "spk_0", "start_time": "0.0",
                     "end_time": "1.0"},
                    {"speaker_label": "spk_1", "start_time": "1.2",
                     "end_time": "2.0"},
                ]
            },
        }
    }


def _numeric_dialog():
    """Live dialog turns with NUMERIC starts spanning two parties so overlap
    reconciliation resolves spk_0 -> party 0 and spk_1 -> party 1."""
    return [
        {"type": "text", "party": 0, "body": "I have a headache", "start": 0.0},
        {"type": "text", "party": 1, "body": "for three days", "start": 1.2},
    ]


def _iso_dialog():
    """Live dialog turns with ISO-STRING starts (the real Deepgram shape); these
    coerce to None so reconciliation falls back to party None / spk labels."""
    return [
        {"type": "text", "party": 0, "body": "I have a headache",
         "start": "2024-01-01T00:00:00+00:00"},
        {"type": "text", "party": 1, "body": "for three days",
         "start": "2024-01-01T00:00:02+00:00"},
    ]


# --- COMPILE-ONLY: no boto3 needed (compile does not run module imports) -------


@pytest.mark.parametrize("function_name", _EDITED_BODIES)
def test_edited_inline_body_compiles(function_name):
    """Every edited inline Lambda body compiles cleanly (the orchestrator cannot
    run cfn-lint/sam in-sandbox, so compile() is the offline gate)."""
    body = _extract_zipfile_body(function_name)
    compile(body, "<{}>".format(function_name), "exec")


# --- MERGE behavioral parity (no boto3; runs in-sandbox) -----------------------


def _merge_namespace():
    return _exec_body("MergeFunction", client_factory=None)


def _branches(transcript_segments, dialog):
    """Parallel-state output: comprehend + bedrock envelopes. transcript_segments
    rides the comprehend branch (as the real GetTranscription -> Comprehend
    re-emit does); bedrock carries None to prove Merge reads from whichever branch
    has them."""
    vcon = {
        "vcon": "0.0.1",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "parties": [{"role": "patient"}, {"role": "agent"}],
        "dialog": dialog,
        "analysis": [],
    }
    return [
        {
            "vcon": vcon,
            "step": "comprehend",
            "recording_key": "room/sess/ts/recording.ogg",
            "entities": [],
            "entities_dialog_index": 0,
            "transcript_text": "I have a headache for three days.",
            "transcript_segments": transcript_segments,
            "entities_degraded": None,
        },
        {
            "vcon": vcon,
            "step": "bedrock",
            "recording_key": "room/sess/ts/recording.ogg",
            "summary_text": "Patient reports a headache.",
            "summary_dialog_indices": [0, 1],
            "transcript_segments": None,
            "summary_degraded": None,
        },
    ]


def _transcript_entry(merged):
    analysis = merged["vcon"]["analysis"]
    assert analysis[0]["type"] == "transcript"
    return analysis[0]


def test_merge_reconcile_matches_pure_core_on_numeric_dialog():
    """The Merge inline _reconcile_segments_to_parties mirror must produce output
    byte-identical to enrichment.reconcile_segments_to_parties on a shared input
    (parity of the hand-mirror against the authoritative pure core)."""
    from vcon_agent import enrichment

    ns = _merge_namespace()
    segments = enrichment.segments_from_transcribe_result(_canned_result())
    dialog = _numeric_dialog()

    mirror = ns["_reconcile_segments_to_parties"](segments, dialog)
    pure = enrichment.reconcile_segments_to_parties(segments, dialog)
    assert mirror == pure
    # And it actually resolved the parties by time overlap.
    assert [t["party"] for t in mirror] == [0, 1]
    assert [t["speaker"] for t in mirror] == ["spk_0", "spk_1"]


def test_merge_reconcile_matches_pure_core_on_iso_dialog():
    """ISO-string dialog starts coerce to None, so every party falls back to None
    and only spk labels survive. The mirror must match the pure core here too."""
    from vcon_agent import enrichment

    ns = _merge_namespace()
    segments = enrichment.segments_from_transcribe_result(_canned_result())
    dialog = _iso_dialog()

    mirror = ns["_reconcile_segments_to_parties"](segments, dialog)
    pure = enrichment.reconcile_segments_to_parties(segments, dialog)
    assert mirror == pure
    assert [t["party"] for t in mirror] == [None, None]
    assert [t["speaker"] for t in mirror] == ["spk_0", "spk_1"]


def test_merge_stores_turns_reconciled_to_parties_with_numeric_dialog():
    """With numeric dialog starts, Merge stores `turns` on the authoritative
    transcript entry reconciled to the fixed party indices, while preserving the
    flat body and the deterministic analysis order."""
    from vcon_agent import enrichment

    ns = _merge_namespace()
    handler = ns["handler"]
    segments = enrichment.segments_from_transcribe_result(_canned_result())

    merged = handler(_branches(segments, _numeric_dialog()), None)

    entry = _transcript_entry(merged)
    assert entry["body"] == "I have a headache for three days."
    assert entry["authoritative"] is True
    assert entry["vendor"] == "aws-transcribe-medical"
    assert entry["mimetype"] == "text/plain"
    assert "turns" in entry
    assert [t["party"] for t in entry["turns"]] == [0, 1]
    assert [t["speaker"] for t in entry["turns"]] == ["spk_0", "spk_1"]
    assert entry["turns"][0]["text"] == "I have a headache"
    assert entry["turns"][1]["text"] == "for three days."
    # Deterministic analysis order preserved, recording_key threaded.
    assert [a["type"] for a in merged["vcon"]["analysis"]] == [
        "transcript",
        "entities",
        "summary",
    ]
    assert merged["recording_key"] == "room/sess/ts/recording.ogg"
    assert merged["step"] == "merge"


def test_merge_turns_party_none_when_dialog_starts_are_iso():
    """With ISO-string dialog starts, Merge still stores `turns` but every party
    is None and the spk labels survive (the documented graceful fallback)."""
    from vcon_agent import enrichment

    ns = _merge_namespace()
    handler = ns["handler"]
    segments = enrichment.segments_from_transcribe_result(_canned_result())

    merged = handler(_branches(segments, _iso_dialog()), None)

    entry = _transcript_entry(merged)
    assert entry["body"] == "I have a headache for three days."
    assert "turns" in entry
    assert [t["party"] for t in entry["turns"]] == [None, None]
    assert [t["speaker"] for t in entry["turns"]] == ["spk_0", "spk_1"]


def test_merge_omits_turns_for_segmentless_envelope():
    """Older envelopes carry no transcript_segments; Merge must append the
    transcript entry exactly as before, with NO `turns` key (back-compat)."""
    ns = _merge_namespace()
    handler = ns["handler"]

    merged = handler(_branches(None, _numeric_dialog()), None)

    entry = _transcript_entry(merged)
    assert entry["body"] == "I have a headache for three days."
    assert "turns" not in entry
    assert [a["type"] for a in merged["vcon"]["analysis"]] == [
        "transcript",
        "entities",
        "summary",
    ]


# --- GETTRANSCRIPTION behavioral parity (needs boto3; skips in-sandbox) --------


def test_get_transcription_emits_grouped_transcript_segments():
    """GetTranscription (real COMPLETED path) must parse the result JSON into
    transcript_segments with correct speaker grouping, matching the pure core.

    GetTranscription's inline body imports boto3 at module top, so exec-ing it
    requires boto3; guard behind importorskip like test_seal_pipeline_wiring.py.
    """
    boto3 = pytest.importorskip(
        "boto3", reason="boto3 not installed; GetTranscription exec test skipped"
    )
    import json

    from vcon_agent import enrichment

    result_json = _canned_result()

    class _FakeTranscribe:
        def get_medical_transcription_job(self, MedicalTranscriptionJobName):  # noqa: N803
            return {
                "MedicalTranscriptionJob": {
                    "TranscriptionJobStatus": "COMPLETED",
                    "Transcript": {
                        "TranscriptFileUri": "s3://bkt/out/result.json"
                    },
                }
            }

    class _FakeBody:
        def read(self):
            return json.dumps(result_json).encode("utf-8")

    class _FakeS3:
        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": _FakeBody()}

    def factory(service):
        if service == "transcribe":
            return _FakeTranscribe()
        if service == "s3":
            return _FakeS3()
        raise AssertionError("unexpected client {}".format(service))

    ns = _exec_body("GetTranscriptionFunction", client_factory=factory)
    handler = ns["handler"]

    event = {
        "vcon": {"dialog": []},
        "recording_key": "room/sess/ts/recording.ogg",
        "job_name": "job-1",
    }
    out = handler(event, None)

    assert out["transcription_status"] == "COMPLETED"
    # transcript_text unchanged (flat join of results.transcripts).
    assert out["transcript_text"] == "I have a headache for three days."
    # transcript_segments parsed and byte-identical to the pure core.
    expected = enrichment.segments_from_transcribe_result(result_json)
    assert out["transcript_segments"] == expected
    assert [s["speaker"] for s in out["transcript_segments"]] == ["spk_0", "spk_1"]
    assert out["transcript_segments"][0]["text"] == "I have a headache"
    assert out["transcript_segments"][1]["text"] == "for three days."


def test_get_transcription_canned_path_emits_structured_segments():
    """The canned no-recording path must derive transcript_segments from the
    canned Transcribe-shaped fixture while keeping transcript_text the canned
    flat string (summary/comprehend source unchanged)."""
    pytest.importorskip(
        "boto3", reason="boto3 not installed; GetTranscription exec test skipped"
    )
    from vcon_agent import enrichment

    # Build the StartTranscription canned envelope from its own inline body so
    # the canned fixture shape is exercised exactly as the pipeline produces it.
    class _FakeTranscribe:
        def start_medical_transcription_job(self, **kwargs):
            return {}

    start_ns = _exec_body(
        "StartTranscriptionFunction",
        client_factory=lambda service: _FakeTranscribe(),
    )
    # No recording -> canned path.
    start_out = start_ns["handler"]({"vcon": {"dialog": []}}, None)
    assert start_out["canned"] is True
    assert "canned_result" in start_out
    assert start_out["canned_transcript"] == (
        "Patient reports a headache for three days and is taking ibuprofen."
    )

    get_ns = _exec_body(
        "GetTranscriptionFunction",
        client_factory=lambda service: None,
    )
    out = get_ns["handler"](start_out, None)

    assert out["transcription_status"] == "COMPLETED"
    # transcript_text stays the canned flat string verbatim.
    assert out["transcript_text"] == (
        "Patient reports a headache for three days and is taking ibuprofen."
    )
    # Structured segments derived from the canned fixture, matching pure core.
    expected = enrichment.segments_from_transcribe_result(
        start_out["canned_result"]
    )
    assert out["transcript_segments"] == expected
    assert [s["speaker"] for s in out["transcript_segments"]] == ["spk_0", "spk_1"]
