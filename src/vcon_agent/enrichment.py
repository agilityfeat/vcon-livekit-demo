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


def segments_from_transcribe_result(
    result_json: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Parse a Transcribe Medical result into speaker SEGMENTS.

    Transcribe Medical (with speaker labels enabled) writes a result JSON that
    carries both a flat transcript AND the structured pieces needed to recover
    speaker-attributed, timestamped turns:

        {
          "results": {
            "transcripts": [{"transcript": "<flat text>"}],
            "items": [
              {"start_time": "0.0", "end_time": "0.5",
               "alternatives": [{"content": "I"}]},
              ...
              {"type": "punctuation", "alternatives": [{"content": "."}]},
            ],
            "speaker_labels": {
              "segments": [
                {"speaker_label": "spk_0", "start_time": "0.0",
                 "end_time": "1.2", "items": [...]},
                ...
              ]
            }
          }
        }

    This returns one dict per ``results.speaker_labels.segments[]`` entry, each
    shaped ``{'speaker': 'spk_0'|'spk_1'|..., 'start': <float sec>,
    'end': <float sec>, 'text': <str>}``. The segment ``text`` is rebuilt by
    walking ``results.items[]`` and keeping the WORD items whose timestamps fall
    inside the segment's [start, end] window:

      * a WORD item carries ``start_time``/``end_time`` (coerced via
        :func:`_as_float`); words join with a single space;
      * a PUNCTUATION item has ``type == 'punctuation'`` and no timestamps, and
        is attached to the PRECEDING word WITHOUT a leading space;
      * each item's surface text is ``alternatives[0].content``.

    Robustness / fallbacks:
      * if ``results.speaker_labels`` is missing or has no segments, fall back to
        a SINGLE ``{'speaker': 'spk_0', 'start': None, 'end': None, 'text': <flat
        transcript>}`` segment (from :func:`transcript_text_from_transcribe_result`);
      * if ``results.items`` is missing but a speaker segment exists, use the flat
        transcript text as a last resort for that segment's text.

    Pure, deterministic, dependency-free so a canned Transcribe-shaped dict makes
    every path testable offline.

    🩹 Shortcut: segment text is reconstructed by bucketing word items into each
    speaker segment's [start, end] window and gluing punctuation onto the prior
    word. → Production: true word-level alignment plus robust diarization /
    channel separation (or let AWS HealthScribe emit speaker-attributed clinical
    turns directly) rather than reassembling from the batch result here.
    """
    results = result_json.get("results") or {}
    speaker_labels = results.get("speaker_labels") or {}
    segments = speaker_labels.get("segments") or []
    items = results.get("items") or []

    flat_text = transcript_text_from_transcribe_result(result_json)

    # Fallback: no diarization available -> one segment with the flat transcript.
    if not segments:
        return [{"speaker": "spk_0", "start": None, "end": None, "text": flat_text}]

    # Precompute (start, end, content, is_punctuation) for every item once. Only
    # word items (those carrying a numeric start) can be bucketed into a segment
    # window; punctuation rides along with its preceding word.
    parsed_items: List[Dict[str, Any]] = []
    for item in items:
        alternatives = item.get("alternatives") or []
        content = alternatives[0].get("content") if alternatives else ""
        is_punct = item.get("type") == "punctuation"
        start = _as_float(item.get("start_time"))
        end = _as_float(item.get("end_time"))
        parsed_items.append(
            {
                "start": start,
                "end": end,
                "content": content or "",
                "is_punct": is_punct,
            }
        )

    out: List[Dict[str, Any]] = []
    for seg in segments:
        speaker = seg.get("speaker_label", "spk_0")
        seg_start = _as_float(seg.get("start_time"))
        seg_end = _as_float(seg.get("end_time"))

        # Collect the word items whose [start, end] falls inside this segment's
        # window, attaching any punctuation that immediately follows a kept word.
        text = ""
        if parsed_items and seg_start is not None and seg_end is not None:
            text = _text_for_window(parsed_items, seg_start, seg_end)

        # Last resort: a segment exists but no items were bucketed into it (items
        # missing, or no numeric window). Use the flat transcript text.
        if not text:
            text = flat_text

        out.append(
            {
                "speaker": speaker,
                "start": seg_start,
                "end": seg_end,
                "text": text,
            }
        )
    return out


def _text_for_window(
    parsed_items: Sequence[Dict[str, Any]],
    seg_start: float,
    seg_end: float,
) -> str:
    """Join the word items whose timing falls in [seg_start, seg_end].

    Words join with a single space; a punctuation item (no timestamps) is glued
    onto the immediately preceding kept word with no leading space. Punctuation
    is only kept when the word right before it was kept (so trailing punctuation
    of a different segment's word never leaks in).
    """
    pieces: List[str] = []
    kept_previous_word = False
    for item in parsed_items:
        if item["is_punct"]:
            # Attach to the preceding word only if that word was kept.
            if kept_previous_word and pieces:
                pieces[-1] = pieces[-1] + item["content"]
            continue
        start = item["start"]
        end = item["end"]
        if start is None:
            kept_previous_word = False
            continue
        # A word belongs to the segment when its start falls within the window.
        # Using end (falling back to start) keeps a word that ends exactly at the
        # boundary.
        word_end = end if end is not None else start
        if seg_start <= start and word_end <= seg_end:
            pieces.append(item["content"])
            kept_previous_word = True
        else:
            kept_previous_word = False
    return " ".join(pieces).strip()


def reconcile_segments_to_parties(
    medical_segments: Sequence[Dict[str, Any]],
    dialog_turns: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Reconcile Transcribe speaker segments to the fixed party indices.

    Takes the speaker SEGMENTS produced by
    :func:`segments_from_transcribe_result` (each ``{'speaker', 'start', 'end',
    'text'}``) and the vCon ``dialog`` turns, and returns one structured TURN per
    segment, shaped ``{'party': <int|None>, 'speaker': 'spk_0'|..., 'start':
    <float|None>, 'end': <float|None>, 'text': <str>}``.

    Algorithm:
      1. :func:`map_turns_by_timestamp_overlap` gives, per segment, the dialog
         turn indices it overlaps in time.
      2. For every overlapped turn, read its party (``dialog_turns[idx]['party']``).
      3. Build a CONSISTENT ``spk_N -> party`` mapping by majority vote: for each
         speaker label, tally the parties of all turns that any of its segments
         overlap, and map the label to the majority party. A tie or no votes maps
         to ``None`` so one speaker label never gets two different parties.
      4. Emit one turn per segment carrying its label's reconciled party.

    CRITICAL FALLBACK: :func:`map_turns_by_timestamp_overlap` only aligns turns
    whose ``start`` is a usable NUMBER; an ISO-string start coerces to ``None``
    (via :func:`_as_float`) and that turn is skipped. So when NO dialog turn has a
    numeric start (the live Deepgram turns record ISO wall-clock starts, not
    per-turn second offsets), every segment overlaps nothing, every party resolves
    to ``None``, and each turn keeps ONLY its ``spk_N`` speaker label. We do NOT
    guess patient vs. agent in that case.

    This is design option (b): fall back to Transcribe's own ``spk_N`` labels
    rather than mis-assigning patient/agent from unalignable data. The proper fix
    (option that makes reconciliation resolve) is having the agent record NUMERIC
    per-turn offsets for the live dialog so time overlap can run; that is OUT OF
    SCOPE here and is flagged for production.

    Pure, deterministic, dependency-free.
    """
    overlaps = map_turns_by_timestamp_overlap(medical_segments, dialog_turns)

    # Tally, per speaker label, the parties of every overlapped dialog turn.
    votes: Dict[str, Dict[Any, int]] = {}
    for seg, covered in zip(medical_segments, overlaps):
        speaker = seg.get("speaker", "spk_0")
        tally = votes.setdefault(speaker, {})
        for idx in covered:
            party = dialog_turns[idx].get("party")
            if party is None:
                continue
            tally[party] = tally.get(party, 0) + 1

    # Resolve each speaker label to a single majority party (ties / none -> None).
    label_to_party: Dict[str, Optional[int]] = {}
    for speaker, tally in votes.items():
        label_to_party[speaker] = _majority_party(tally)

    turns: List[Dict[str, Any]] = []
    for seg in medical_segments:
        speaker = seg.get("speaker", "spk_0")
        turns.append(
            {
                "party": label_to_party.get(speaker),
                "speaker": speaker,
                "start": seg.get("start"),
                "end": seg.get("end"),
                "text": seg.get("text", ""),
            }
        )
    return turns


def _majority_party(tally: Dict[Any, int]) -> Optional[int]:
    """Return the single party with the strict majority of votes, else ``None``.

    An empty tally (no overlapped turns, or none carried a party) yields
    ``None``. A tie between two parties also yields ``None`` so one speaker label
    is never force-mapped to an arbitrary party.
    """
    if not tally:
        return None
    ordered = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
    if len(ordered) >= 2 and ordered[0][1] == ordered[1][1]:
        return None
    winner = ordered[0][0]
    try:
        return int(winner)
    except (TypeError, ValueError):
        return winner


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
