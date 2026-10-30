"""Incremental, framework-independent vCon model for the intake agent.

This module builds an unsigned vCon document turn-by-turn as a call happens.
It has NO LiveKit / Deepgram / ElevenLabs / AWS dependency on purpose, so the
vCon-assembly logic is fully unit-testable with no live network. The LiveKit
entrypoint in ``agent.py`` wires the LiveKit callbacks into this core; the
persistence layer flushes the finished document to S3 / a local file.

Document shape (matches what the AWS infrastructure template consumes):

    {
      "vcon": "0.0.1",
      "uuid": "<uuid4>",
      "subject": "Patient intake call",
      "created_at": "<ISO 8601 UTC>",
      "parties": [ {patient}, {agent} ],
      "dialog": [ ... ],
      "analysis": [],
      "attachments": [ {consent}, ... ]
    }

Party indices are fixed: index 0 = patient, index 1 = agent. Dialog and
attachment entries reference parties by index (never an embedded identity) so
downstream enrichment can hang provenance off the party index.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

# vCon specification version consumed by the enrichment pipeline.
VCON_VERSION = "0.0.1"

# Fixed party indices. The whole model (dialog turns, consent attachment) refers
# to parties by these indices rather than embedding identities inline.
PATIENT_INDEX = 0
AGENT_INDEX = 1

# Default subject for the scripted patient-intake call.
DEFAULT_SUBJECT = "Patient intake call"

# Step Functions execution-name constraints (see the StartExecution API docs):
# the name must be 1..80 characters and is restricted to a safe charset. We
# allow letters, digits, hyphen, and underscore only, which is a strict subset
# of what the API accepts, so any name we build is always valid. The raw
# session_start_iso carries ':' and '.' (e.g. '2024-01-01T00:00:00.123+00:00')
# which are NOT in this charset, hence the sanitize-and-hash below.
_EXECUTION_NAME_MAX_LEN = 80
# Characters NOT in [A-Za-z0-9_-] get collapsed to a single '-' in the readable
# prefix. The full key is still hashed so distinctness never depends on the
# (lossy) sanitized prefix.
_EXECUTION_NAME_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]+")
# Length of the sha256 hex digest suffix kept for uniqueness. 16 hex chars
# (64 bits) is ample collision resistance for one clinic's call volume.
_EXECUTION_NAME_HASH_LEN = 16

# Hardcoded consent script (demo shortcut). Production would use a
# versioned consent text, bind it to an authenticated identity, and scope it to
# the applicable jurisdiction rather than shipping a single literal string here.
DEFAULT_CONSENT_SCRIPT = (
    "This call will be recorded and transcribed to assist with your intake. "
    "Do you consent to recording and processing of this conversation?"
)

# Map of LLM / transcript role strings to the fixed party index. The agent uses
# this to translate LiveKit ChatMessage roles ('user'/'assistant') into indices.
_ROLE_TO_INDEX = {
    "user": PATIENT_INDEX,
    "patient": PATIENT_INDEX,
    "assistant": AGENT_INDEX,
    "agent": AGENT_INDEX,
}


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def role_to_index(role: str) -> int:
    """Map a role string to a fixed party index.

    'user' / 'patient' -> 0 (patient), 'assistant' / 'agent' -> 1 (agent).
    The lookup is case-insensitive and tolerant of surrounding whitespace.
    """
    if role is None:
        raise ValueError("role must not be None")
    key = role.strip().lower()
    try:
        return _ROLE_TO_INDEX[key]
    except KeyError:
        raise ValueError(f"unknown role: {role!r}") from None


def run_key(room_name: str, session_start_iso: str) -> str:
    """Build the unique-key-per-run S3 object key for the vCon skeleton.

    Layout (exactly as documented in infrastructure/README.md):

        <room_name>/<session_start_iso>/vcon-skeleton.json

    ``session_start_iso`` is a microsecond-precision UTC ISO instant captured
    once at entry, so it uniquely identifies the run on its own; combined with
    the bucket's versioning this neutralizes any accidental overwrite. It is the
    single run segment shared with :func:`recording_key`.
    """
    return f"{room_name}/{session_start_iso}/vcon-skeleton.json"


def recording_key(room_name: str, session_start_iso: str) -> str:
    """Build the unique-key-per-run S3 object key for the call recording.

    Layout (exactly as documented in infrastructure/README.md, parallel to
    :func:`run_key`):

        <room_name>/<session_start_iso>/recording.ogg

    The per-run key has a SINGLE time segment: ``session_start_iso`` (a
    microsecond-precision UTC ISO instant captured once at entry) is the whole
    run identity. Passing the same ``(room_name, session_start_iso)`` pair to
    BOTH this helper and :func:`run_key` keeps ``recording.ogg`` and
    ``vcon-skeleton.json`` under the identical run prefix. This is deliberately
    NOT an overload of :func:`run_key`: the two objects live side by side under
    the shared prefix but have distinct object names (recording.ogg vs.
    vcon-skeleton.json).
    """
    return f"{room_name}/{session_start_iso}/recording.ogg"


def execution_name(room_name: str, session_start_iso: str) -> str:
    """Build a deterministic, idempotent Step Functions execution name.

    The local agent triggers enrichment by calling ``StartExecution`` directly
    (no webhook on the critical path). To make a RETRIED start a no-op rather
    than a duplicate pipeline run, the execution name is derived DETERMINISTICALLY
    from the idempotency key ``room_name + '|' + session_start_iso``: the same
    inputs always produce the same name, and Step Functions rejects a second
    ``StartExecution`` with an already-used name, so the retry collapses into the
    original run.

    The returned name always satisfies the StartExecution constraints: it is
    1..80 characters and contains only ``[A-Za-z0-9_-]`` (so it never carries the
    ':' or '.' that appear in the raw ISO ``session_start_iso``). It is built as
    a readable sanitized room-name prefix plus a short sha256 hash of the FULL
    key, joined with '-'. Distinctness is guaranteed by the hash of the full key,
    not by the (lossy) sanitized prefix, so two rooms that sanitize to the same
    prefix still get different names.

    🩹 Shortcut: the idempotency key is just ``room_name + session start``, and
    uniqueness rides on a truncated sha256. → Production: derive the key from a
    stable, server-assigned call/session id (not a client-chosen room name), and
    consider the full digest or a dedicated idempotency store if call volume ever
    makes a 64-bit truncation uncomfortable.
    """
    key = f"{room_name}|{session_start_iso}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_EXECUTION_NAME_HASH_LEN]

    # Readable prefix: sanitize the room name to the allowed charset and trim any
    # leading/trailing hyphens left by the collapse so the join stays tidy.
    prefix = _EXECUTION_NAME_DISALLOWED.sub("-", room_name).strip("-")

    # Reserve room for the '-' separator and the fixed-length hash suffix so the
    # whole string is guaranteed <= 80 characters.
    max_prefix_len = _EXECUTION_NAME_MAX_LEN - _EXECUTION_NAME_HASH_LEN - 1
    if len(prefix) > max_prefix_len:
        prefix = prefix[:max_prefix_len].strip("-")

    if not prefix:
        # Degenerate room name (empty or all-disallowed): the hash alone is a
        # valid, deterministic name.
        return digest
    return f"{prefix}-{digest}"


class VConBuilder:
    """Assembles an unsigned vCon document incrementally during a call.

    Usage mirrors the call lifecycle:

        builder = VConBuilder()
        builder.add_consent(granted=True)   # BEFORE any clinical content
        builder.add_dialog_turn(0, "I have a headache.")
        builder.add_dialog_turn(1, "How long has that been going on?")
        doc = builder.to_dict()
    """

    def __init__(self, subject: str = DEFAULT_SUBJECT) -> None:
        # Seed the parties list with the fixed indices: 0 = patient, 1 = agent.
        #
        # Acknowledged shortcut: a single hardcoded patient/agent identity.
        # Production would populate these from authenticated participant
        # identities (e.g. a verified patient record and the agent's service
        # account) rather than static placeholder names.
        parties: List[Dict[str, Any]] = [
            {"name": "Patient", "role": "patient"},
            {"name": "Intake Agent", "role": "agent"},
        ]
        self._doc: Dict[str, Any] = {
            "vcon": VCON_VERSION,
            "uuid": str(uuid.uuid4()),
            "subject": subject,
            "created_at": _utc_now_iso(),
            "parties": parties,
            "dialog": [],
            "analysis": [],
            "attachments": [],
        }
        # Guard: consent must be recorded before any dialog is appended.
        self._consent_recorded = False

    @property
    def consent_recorded(self) -> bool:
        """Whether consent has been captured yet (via :meth:`add_consent`)."""
        return self._consent_recorded

    def add_consent(
        self,
        granted: bool,
        script: str = DEFAULT_CONSENT_SCRIPT,
    ) -> Dict[str, Any]:
        """Record consent as an attachments entry BEFORE any clinical content.

        Consent is captured as an attachment that references the patient party
        (index 0). It must be recorded before any dialog turn is appended; this
        ordering requirement is enforced by :meth:`add_dialog_turn`.

        Acknowledged shortcut: a hardcoded consent script plus a simple boolean
        flag. Production would store versioned consent text, bind consent to an
        authenticated identity, and capture the jurisdiction scope under which
        it was given.
        """
        entry: Dict[str, Any] = {
            "type": "consent",
            "party": PATIENT_INDEX,
            "granted": bool(granted),
            "script": script,
            "timestamp": _utc_now_iso(),
        }
        self._doc["attachments"].append(entry)
        self._consent_recorded = True
        return entry

    def add_dialog_turn(
        self,
        party_index: int,
        text: str,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a dialog turn referencing a party by index.

        The dialog object references ``party_index`` (0 or 1), never an embedded
        identity, so provenance can hang off the party index later. ``start``
        defaults to the current UTC time.

        Consent must be recorded first (ordering requirement): calling this
        before :meth:`add_consent` raises ``RuntimeError``.
        """
        if not self._consent_recorded:
            raise RuntimeError(
                "consent must be recorded via add_consent() before appending dialog"
            )
        if party_index not in (PATIENT_INDEX, AGENT_INDEX):
            raise ValueError(
                f"party_index must be {PATIENT_INDEX} (patient) or "
                f"{AGENT_INDEX} (agent), got {party_index!r}"
            )
        if start is None:
            start = _utc_now_iso()
        entry: Dict[str, Any] = {
            "type": "text",
            "party": party_index,
            "body": text,
            "start": start,
            "mimetype": "text/plain",
        }
        self._doc["dialog"].append(entry)
        return entry

    def add_dialog_turn_by_role(
        self,
        role: str,
        text: str,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convenience wrapper: append a dialog turn given a role string.

        Translates ``role`` ('user'/'patient'/'assistant'/'agent') to a party
        index via :func:`role_to_index`, then delegates to
        :meth:`add_dialog_turn`.
        """
        return self.add_dialog_turn(role_to_index(role), text, start=start)

    def add_recording_dialog(
        self,
        url: str,
        start: Optional[str] = None,
        mimetype: str = "audio/ogg",
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a 'recording' dialog object pointing at the call audio in S3.

        The recording is whole-call audio captured by LiveKit Egress and stored
        in S3. It is carried as an EXTERNAL REFERENCE: the ``url`` field points at
        the S3 object (e.g. ``s3://<bucket>/<key>``) and the audio bytes are
        NEVER inlined into the vCon. Unlike :meth:`add_dialog_turn`, this is not
        patient/agent speech, so it does NOT reference a single party index; it
        represents the whole call.

        SHORTCUT: audio only (no video); plain SSE-S3 (no CMK);
        readiness is handled by polling S3 at the recording key, not
        event-driven here. Production would support configurable video egress,
        SSE-KMS with a CMK, and event-driven readiness with proper dedupe on the
        egress id and reconciliation for missing/early/truncated egress events.
        """
        if start is None:
            start = _utc_now_iso()
        entry: Dict[str, Any] = {
            "type": "recording",
            # External reference ONLY: the url locates the audio in S3; the
            # audio bytes are never embedded in the vCon document.
            "url": url,
            "start": start,
            "mimetype": mimetype,
        }
        if filename is not None:
            entry["filename"] = filename
        self._doc["dialog"].append(entry)
        return entry

    def add_transcript_analysis(
        self,
        transcript_text: str,
        dialog_indices: Sequence[int],
        vendor: str = "aws-transcribe-medical",
        authoritative: bool = True,
        turns: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Append a 'transcript' analysis object with turn-index provenance.

        This is the CLINICAL transcript produced by Transcribe Medical. It is
        stored as an ``analysis[]`` entry (not a new dialog turn) that REFERENCES
        the dialog turn indices it covers via ``dialog``, so it sits ALONGSIDE
        the live Deepgram transcript already in ``dialog[]`` rather than replacing
        it. The two producers serve different needs (latency-tuned live STT vs.
        clinically accurate batch transcription); keeping both with provenance is
        the concrete payoff of the dual-transcript design, and vCon models it
        natively. The clinical copy is marked ``authoritative`` so downstream
        consumers know which transcript to trust for clinical use.

        Structured ``turns``: Transcribe Medical can emit speaker-labeled,
        timestamped segments, so the authoritative transcript need not stay a
        flat blob. When ``turns`` is provided and non-empty, the speaker-attributed
        turns (each ``{'party'?, 'speaker', 'start', 'end', 'text'}``, as produced
        by enrichment.reconcile_segments_to_parties) are stored on the SAME entry
        under a new ``turns`` key (copied defensively) so the record view can
        render the clinical transcript as turns like the live transcript. The flat
        ``body`` text is ALWAYS preserved: it is the back-compat shape older
        consumers read and the source the Bedrock summary is built from. When
        ``turns`` is ``None`` or empty, the entry is byte-identical to the
        flat-only shape (no ``turns`` key).

        🩹 Shortcut: provenance is a flat list of dialog turn indices mapped by
        timestamp overlap (see enrichment.map_turns_by_timestamp_overlap), and the
        structured ``turns`` carry whatever party reconciliation could resolve
        (``party`` may be ``None`` when the live turns have no numeric starts to
        align against). → Production: robust alignment between the diarized medical
        transcript and the live turns (word-level timing, speaker reconciliation).
        """
        entry: Dict[str, Any] = {
            "type": "transcript",
            "vendor": vendor,
            "authoritative": bool(authoritative),
            # Turn-index provenance: which dialog turns this transcript covers.
            "dialog": [int(i) for i in dialog_indices],
            "body": transcript_text,
            "mimetype": "text/plain",
        }
        # Structured speaker turns are additive: store them only when present so
        # the flat-only entry shape stays byte-identical to before.
        if turns:
            entry["turns"] = [dict(t) for t in turns]
        self._doc["analysis"].append(entry)
        return entry

    def add_entities_analysis(
        self,
        entities: Sequence[Dict[str, Any]],
        dialog_index: int,
        vendor: str = "aws-comprehend-medical",
        degraded: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an 'entities' analysis object scoped to one dialog turn.

        Comprehend Medical extracts clinical entities from a single dialog turn;
        the resulting entity list is stored as an ``analysis[]`` entry that
        references that turn via ``dialog`` (a single turn index) for provenance.
        Each entity carries at least ``category`` and ``text`` (and ``score`` when
        the shaper kept it). The whole vCon still references parties/turns by
        index, never embedded identities.

        When the Comprehend step fails open (model access denied, throttling)
        it passes a ``degraded`` reason string; it is attached to the entry so a
        consumer can distinguish a real empty entity list from a silent failure.
        A ``None`` reason omits the key entirely (the healthy case).

        🩹 Shortcut: the entity set is kept minimal to meds + conditions
        (MEDICATION + MEDICAL_CONDITION), shaped by
        enrichment.extract_min_entities. → Production: the full Comprehend Medical
        entity set with RxNorm / ICD-10-CM linking and confidence thresholds.

        🩹 Shortcut: a failed analysis fails OPEN to an empty list with a
        ``degraded`` marker so the demo pipeline still finishes. → Production: a
        Catch to a visible alarm state plus retries, so an operator is paged
        rather than reading a silently empty result.
        """
        entry: Dict[str, Any] = {
            "type": "entities",
            "vendor": vendor,
            # Turn-index provenance: the single dialog turn these entities came
            # from. Kept as a one-element list so every analysis entry exposes
            # provenance under the same ``dialog`` key.
            "dialog": [int(dialog_index)],
            "body": [dict(e) for e in entities],
        }
        if degraded:
            entry["degraded"] = degraded
        self._doc["analysis"].append(entry)
        return entry

    def add_summary_analysis(
        self,
        summary_text: str,
        dialog_indices: Sequence[int],
        vendor: str = "aws-bedrock",
        degraded: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a 'summary' analysis object with turn-index provenance.

        Bedrock produces a physician-facing summary of the call. It is stored as
        an ``analysis[]`` entry that references the dialog turn indices it was
        built from via ``dialog`` so the summary is traceable back to the exact
        turns it summarizes.

        When the Bedrock step fails open (model not enabled, throttled) it passes
        a ``degraded`` reason string; it is attached to the entry so a consumer
        can distinguish an intentionally empty summary from a silent failure. A
        ``None`` reason omits the key entirely (the healthy case).

        🩹 Shortcut: a single free-text summary over the whole dialog.
        → Production: structured clinical note sections (HPI, assessment, plan)
        with per-section provenance and model/version pinning.

        🩹 Shortcut: a failed call fails OPEN to an empty summary with a
        ``degraded`` marker so the demo pipeline still finishes. → Production: a
        Catch to a visible alarm state plus retries, so an operator is paged
        rather than reading a silently empty result.
        """
        entry: Dict[str, Any] = {
            "type": "summary",
            "vendor": vendor,
            "dialog": [int(i) for i in dialog_indices],
            "body": summary_text,
            "mimetype": "text/plain",
        }
        if degraded:
            entry["degraded"] = degraded
        self._doc["analysis"].append(entry)
        return entry

    def to_dict(self) -> Dict[str, Any]:
        """Return the current in-memory vCon document."""
        return self._doc
