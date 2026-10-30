"""Pure, boto3-free shaping and provenance helpers for the enrichment pipeline.

This module is the AUTHORITATIVE, unit-tested copy of the correctness-critical
logic each enrichment step runs: mapping medical-transcript segments back to
dialog turns for provenance, shaping a Comprehend Medical response down to the
minimal entity set, parsing the Transcribe Medical output URI into an S3
location, pulling text out of a Transcribe Medical result, building the Bedrock
summary prompt, and pulling the summary text out of a Bedrock response.

Why a separate pure module? The real pipeline runs as inline-code Lambdas inside
the CloudFormation template (``Code.ZipFile``), which CANNOT import this repo
package at AWS runtime. The pipeline therefore MIRRORS this logic into those
inline Lambda bodies. Keeping the logic here as pure functions means it is
exercised by the offline pytest suite (with canned AWS-shaped inputs, no live
calls), and the inline copies stay small and checkable with py_compile. This
mirror/pure-core split matches the existing stub/pure-core convention used across
the repo (e.g. vcon.py is the framework-independent core).

Everything here is deterministic and dependency-free: no boto3, no network. Where
the pipeline would call a boto3 client, the caller passes the already-returned
dict into these shapers, so a canned response makes every path testable offline.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Comprehend Medical entity categories we keep. 🩹 Shortcut: the entity set is
# intentionally minimal (meds + conditions) so the demo stays small.
# → Production: the full Comprehend Medical entity set with RxNorm / ICD-10-CM
# linking and confidence thresholds.
MEDICATION_CATEGORY = "MEDICATION"
MEDICAL_CONDITION_CATEGORY = "MEDICAL_CONDITION"
_KEPT_CATEGORIES = frozenset({MEDICATION_CATEGORY, MEDICAL_CONDITION_CATEGORY})


def map_turns_by_timestamp_overlap(
    medical_segments: Sequence[Dict[str, Any]],
    dialog_turns: Sequence[Dict[str, Any]],
) -> List[List[int]]:
    """Map each medical-transcript segment to the dialog turn indices it covers.

    ``medical_segments`` is a list of segments carrying numeric ``start`` and
    ``end`` times (seconds). ``dialog_turns`` is the existing vCon ``dialog``
    list; each turn carries a numeric ``start`` (and optionally ``end``). This
    returns, for each segment (in order), the list of dialog turn indices whose
    [start, end) interval OVERLAPS the segment's [start, end) interval.

    A dialog turn with no explicit ``end`` is treated as extending up to the next
    turn's ``start`` (and the last turn as extending to +infinity), which is the
    simplest reasonable interpretation when only turn start times are recorded.
    Only turns whose ``start`` is a usable number participate; turns without a
    numeric start are skipped (they cannot be aligned by time).

    🩹 Shortcut: provenance is a simple timestamp-overlap mapping against
    turn start times. → Production: robust alignment between the diarized medical
    transcript and the live turns (word-level timing and speaker reconciliation).

    SCAFFOLDING NOTE: this is the production-direction, segment-level provenance
    mapping. It is intentionally NOT wired into the shipped demo pipeline yet.
    The inline ``MergeFunction`` / ``ComprehendFunction`` in
    ``infrastructure/template.yaml`` use coarser attribution (the transcript
    covers every text turn, entities pin to the first patient turn) because the
    canned/demo Transcribe path does not emit per-segment timings to feed this
    mapper. Keeping the helper here, unit-tested, documents and reserves the
    real mapping so the tested logic and the shipped behavior do not silently
    diverge: when the pipeline starts emitting real medical segments, the merge
    step feeds them through this function instead of the coarse attribution.
    """
    # Precompute each dialog turn's [start, end) interval once.
    intervals: List[Optional[tuple]] = []
    starts: List[Optional[float]] = []
    for turn in dialog_turns:
        starts.append(_as_float(turn.get("start")))
    for idx, turn in enumerate(dialog_turns):
        start = starts[idx]
        if start is None:
            intervals.append(None)
            continue
        end = _as_float(turn.get("end"))
        if end is None:
            # Extend to the next turn that has a usable start, else +infinity.
            end = float("inf")
            for later in starts[idx + 1:]:
                if later is not None and later > start:
                    end = later
                    break
        intervals.append((start, end))

    mapped: List[List[int]] = []
    for seg in medical_segments:
        seg_start = _as_float(seg.get("start"))
        seg_end = _as_float(seg.get("end"))
        if seg_start is None:
            mapped.append([])
            continue
        if seg_end is None:
            seg_end = seg_start
        covered: List[int] = []
        for idx, interval in enumerate(intervals):
            if interval is None:
                continue
            t_start, t_end = interval
            # Half-open overlap: seg_start < t_end and t_start < seg_end, with a
            # zero-length segment (seg_start == seg_end) still matching the turn
            # whose interval contains the instant.
            if seg_start == seg_end:
                if t_start <= seg_start < t_end:
                    covered.append(idx)
            elif seg_start < t_end and t_start < seg_end:
                covered.append(idx)
        mapped.append(covered)
    return mapped


def extract_min_entities(comprehend_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Shape a Comprehend Medical DetectEntitiesV2 response to the minimal set.

    Takes a dict shaped like the ``detect_entities_v2`` response (an ``Entities``
    list where each entity has ``Category``, ``Text`` and optionally ``Score``)
    and returns a minimal list of ``{"category", "text", "score"?}`` dicts
    filtered to MEDICATION and MEDICAL_CONDITION only. Entities in other
    categories are dropped. A missing ``Score`` is tolerated (the ``score`` key
    is simply omitted for that entity).

    This lets the inline Lambda's shaping logic be unit-tested against a canned
    Comprehend response with no live call.

    🩹 Shortcut: meds + conditions only, no code-system linking.
    → Production: full entity set with RxNorm / ICD-10-CM linking.
    """
    entities = comprehend_response.get("Entities") or []
    minimal: List[Dict[str, Any]] = []
    for ent in entities:
        category = ent.get("Category")
        if category not in _KEPT_CATEGORIES:
            continue
        shaped: Dict[str, Any] = {
            "category": category,
            "text": ent.get("Text"),
        }
        if "Score" in ent and ent.get("Score") is not None:
            shaped["score"] = ent.get("Score")
        minimal.append(shaped)
    return minimal


def transcript_text_from_transcribe_result(result_json: Dict[str, Any]) -> str:
    """Pull the full transcript text out of a Transcribe Medical result document.

    Transcribe (Medical) batch jobs write a result JSON shaped like
    ``{"results": {"transcripts": [{"transcript": "<text>"}, ...]}}``. This joins
    the ``transcript`` strings from every entry in ``results.transcripts`` (there
    is normally one) into a single string.

    🩹 Shortcut / stub option: a canned transcript dict of this exact shape can be
    fed in so the pipeline is runnable WITHOUT a real recording or batch job.
    → Production: the real Transcribe Medical result; or AWS HealthScribe for
    clinician-grade notes plus transcript in one service.
    """
    results = result_json.get("results") or {}
    transcripts = results.get("transcripts") or []
    parts = [t.get("transcript", "") for t in transcripts if t.get("transcript")]
    return " ".join(parts).strip()


def s3_location_from_transcribe_uri(uri: str) -> Optional[tuple]:
    """Parse a Transcribe Medical ``TranscriptFileUri`` into ``(bucket, key)``.

    When ``StartMedicalTranscriptionJob`` is called with ``OutputBucketName``
    set, the completed job's ``Transcript.TranscriptFileUri`` points at the
    result object in that customer bucket, and reading it requires SigV4-signed
    S3 access, NOT an unauthenticated HTTP fetch. This helper turns the URI into
    the ``(bucket, key)`` pair the pipeline uses to read the result through the
    S3 client (``get_object``). An unauthenticated ``urllib`` fetch of that URI
    would 403 on a real deployment.

    Two URI shapes are handled:
      * the S3 scheme form ``s3://<bucket>/<key>``
      * the HTTPS virtual-hosted / path-style forms AWS returns, e.g.
        ``https://s3.<region>.amazonaws.com/<bucket>/<key>`` and
        ``https://<bucket>.s3.<region>.amazonaws.com/<key>``

    Returns ``(bucket, key)`` or ``None`` if the URI is not a recognizable S3
    location. Pure and dependency-free so the parsing is testable offline.
    """
    if not isinstance(uri, str) or not uri:
        return None

    # s3://<bucket>/<key>
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        parts = rest.split("/", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return (parts[0], parts[1])
        return None

    if uri.startswith("https://") or uri.startswith("http://"):
        without_scheme = uri.split("://", 1)[1]
        host, _, path = without_scheme.partition("/")
        path = path.split("?", 1)[0]  # drop any query string
        if not host or not path:
            return None
        # Virtual-hosted style: <bucket>.s3... or <bucket>.s3-<region>...
        # The host's leading label is the bucket when the next label is "s3"
        # (or "s3-<region>").
        labels = host.split(".")
        if len(labels) >= 2 and (labels[1] == "s3" or labels[1].startswith("s3-")):
            bucket = labels[0]
            key = path
            if bucket and key:
                return (bucket, key)
            return None
        # Path style: s3.<region>.amazonaws.com/<bucket>/<key>
        if labels and (labels[0] == "s3" or labels[0].startswith("s3-")):
            parts = path.split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                return (parts[0], parts[1])
            return None
    return None


# The physician-summary instruction + framing is defined ONCE here and reused by
# both prompt builders (dialog-sourced and transcript-sourced) so the wording
# cannot drift between them. Keep this byte-identical to the inline mirror in
# infrastructure/template.yaml's BedrockFunction body.
_SUMMARY_INSTRUCTION = (
    "You are a clinical documentation assistant. Write a concise, factual "
    "physician-facing summary of the following patient intake conversation. "
    "Include the chief complaint, duration, and any medications mentioned. "
    "Do not add diagnoses or recommendations."
)


def _summary_prompt(transcript: str) -> str:
    """Wrap a transcript body between the shared instruction and the 'Summary:'
    marker. The single framing both builders share, so the exact wording is
    defined once.
    """
    return (
        f"{_SUMMARY_INSTRUCTION}\n\n"
        "Conversation:\n"
        f"{transcript}\n\n"
        "Summary:"
    )


def summary_prompt_from_dialog(dialog_turns: Sequence[Dict[str, Any]]) -> str:
    """Build the Bedrock physician-summary prompt from the dialog turns.

    Renders the text dialog turns (party 0 = Patient, party 1 = Agent) as a
    simple labeled transcript and wraps it with a short instruction asking for a
    concise physician-facing summary. Non-text turns (e.g. the recording dialog
    object) are skipped. Pure and deterministic so the exact prompt is testable.

    🩹 Shortcut: a single free-text instruction over the raw turns.
    → Production: a structured note template (HPI / assessment / plan), few-shot
    exemplars, and model/version pinning.
    """
    lines: List[str] = []
    for turn in dialog_turns:
        if turn.get("type") != "text":
            continue
        party = turn.get("party")
        speaker = "Patient" if party == 0 else "Agent" if party == 1 else "Unknown"
        body = turn.get("body", "")
        lines.append(f"{speaker}: {body}")
    transcript = "\n".join(lines)
    return _summary_prompt(transcript)


def summary_prompt_from_transcript(transcript_text: str) -> str:
    """Build the Bedrock physician-summary prompt from the AUTHORITATIVE transcript.

    Takes the single transcript string produced by
    ``transcript_text_from_transcribe_result`` (the Transcribe Medical result
    stored authoritative in analysis[]) and wraps it with the SAME
    physician-summary instruction used by ``summary_prompt_from_dialog``. The
    body is presented as the clinical transcript text verbatim: no fabricated
    Patient/Agent speaker labels, because the authoritative blob does not carry
    speaker attribution. Pure and deterministic so the exact prompt is testable.

    Why source the summary from the authoritative transcript instead of the live
    dialog turns? The live (Deepgram) transcript can mis-hear clinical terms
    (e.g. a medication name heard as a common word), and summarizing from it
    propagates that error. Transcribe Medical captures those terms correctly, so
    the clinical artifact (the summary) is built from the clinically-accurate
    source.

    🩹 Shortcut → Production: summarizing from the authoritative transcript blob
    is clinically correct for terms like medication names, but it LOSES the clean
    speaker attribution the live dialog turns provide (the blob is one
    undifferentiated string). Production should reconcile the diarized medical
    transcript with the live turns to get BOTH speaker structure AND clinical
    accuracy, e.g. via the existing ``map_turns_by_timestamp_overlap`` helper
    (unit-tested but intentionally not wired into the demo).
    """
    return _summary_prompt(transcript_text)


def summary_text_from_bedrock_response(response: Dict[str, Any]) -> str:
    """Pull the model's summary text out of a Bedrock InvokeModel response.

    Supports the common Anthropic-on-Bedrock Messages response shape
    ``{"content": [{"type": "text", "text": "<summary>"}, ...]}`` by joining the
    text blocks. Also tolerates the legacy ``{"completion": "<text>"}`` and the
    ``{"outputs": [{"text": "<text>"}]}`` shapes so a canned response of any of
    these forms is testable offline.

    🩹 Shortcut / stub option: a canned response dict of this shape lets the
    pipeline run without a live Bedrock call. → Production: the real InvokeModel
    response with a pinned model id and token accounting.
    """
    # Anthropic Messages shape.
    content = response.get("content")
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("text")
        ]
        if parts:
            return " ".join(parts).strip()

    # Legacy single-completion shape.
    completion = response.get("completion")
    if isinstance(completion, str) and completion.strip():
        return completion.strip()

    # Titan / "outputs" style shape.
    outputs = response.get("outputs")
    if isinstance(outputs, list):
        parts = [
            out.get("text", "")
            for out in outputs
            if isinstance(out, dict) and out.get("text")
        ]
        if parts:
            return " ".join(parts).strip()

    return ""


def _as_float(value: Any) -> Optional[float]:
    """Best-effort numeric coercion used by the timestamp-overlap mapping.

    Returns a ``float`` for ints/floats/numeric strings, else ``None``. ISO
    timestamp strings (which carry ':' and '-') are not numeric and yield
    ``None`` so they are skipped rather than mis-parsed; the overlap mapping is
    for numeric segment/turn times.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
