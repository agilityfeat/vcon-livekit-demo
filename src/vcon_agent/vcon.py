"""Incremental, framework-independent vCon model for the intake agent.

This module builds an unsigned vCon document turn-by-turn as a call happens.
It has NO LiveKit / Deepgram / ElevenLabs / AWS dependency on purpose, so the
vCon-assembly logic is fully unit-testable with no live network. FEAT-003 wires
the LiveKit callbacks into this core; the persistence layer flushes the finished
document to S3 / a local file.

Document shape (matches what the Spec 0 infrastructure template consumes):

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

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# vCon spec version consumed by the Spec 0 pipeline.
VCON_VERSION = "0.0.1"

# Fixed party indices. The whole model (dialog turns, consent attachment) refers
# to parties by these indices rather than embedding identities inline.
PATIENT_INDEX = 0
AGENT_INDEX = 1

# Default subject for the scripted patient-intake call.
DEFAULT_SUBJECT = "Patient intake call"

# Hardcoded consent script (acknowledged shortcut). Production would use a
# versioned consent text, bind it to an authenticated identity, and scope it to
# the applicable jurisdiction rather than shipping a single literal string here.
DEFAULT_CONSENT_SCRIPT = (
    "This call will be recorded and transcribed to assist with your intake. "
    "Do you consent to recording and processing of this conversation?"
)

# Map of LLM / transcript role strings to the fixed party index. FEAT-003 uses
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


def run_key(room_name: str, session_start_iso: str, timestamp: Optional[str] = None) -> str:
    """Build the unique-key-per-run S3 object key for the vCon skeleton.

    Layout (exactly as documented in infrastructure/README.md):

        <room_name>/<session_start_iso>/<timestamp>/vcon-skeleton.json

    ``timestamp`` defaults to the current UTC time so each flush lands under a
    distinct prefix even if the room name and session start repeat.
    """
    if timestamp is None:
        timestamp = _utc_now_iso()
    return f"{room_name}/{session_start_iso}/{timestamp}/vcon-skeleton.json"


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

    def to_dict(self) -> Dict[str, Any]:
        """Return the current in-memory vCon document."""
        return self._doc
