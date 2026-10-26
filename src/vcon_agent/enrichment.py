"""Pure, boto3-free shaping and provenance helpers for the Spec 3 pipeline.

This module is the AUTHORITATIVE, unit-tested copy of the correctness-critical
logic each enrichment step runs: mapping medical-transcript segments back to
dialog turns for provenance, shaping a Comprehend Medical response down to the
minimal entity set, pulling text out of a Transcribe Medical result, building the
Bedrock summary prompt, and pulling the summary text out of a Bedrock response.

Why a separate pure module? The real pipeline runs as inline-code Lambdas inside
the CloudFormation template (``Code.ZipFile``), which CANNOT import this repo
package at AWS runtime. FEAT-003 therefore MIRRORS this logic into those inline
Lambda bodies. Keeping the logic here as pure functions means it is exercised by
the offline pytest suite (with canned AWS-shaped inputs, no live calls), and the
inline copies stay small and checkable with py_compile. This mirror/pure-core
split matches the existing stub/pure-core convention already used across the repo
(e.g. vcon.py is the framework-independent core).

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
    return (
        "You are a clinical documentation assistant. Write a concise, factual "
        "physician-facing summary of the following patient intake conversation. "
        "Include the chief complaint, duration, and any medications mentioned. "
        "Do not add diagnoses or recommendations.\n\n"
        "Conversation:\n"
        f"{transcript}\n\n"
        "Summary:"
    )


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
