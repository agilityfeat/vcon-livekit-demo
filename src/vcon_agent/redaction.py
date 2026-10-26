"""Pure, boto3-free redaction + full↔redacted linking for the viewer.

The enrichment pipeline stops after the Bedrock summary: the merged vCon has NO
redaction step and NO ``redacted`` link (the infrastructure README mentions a
vcon-redacted.json key but nothing ever emits it). The viewer needs a REAL
full+redacted pair so it can show the analyst the de-identified record and prove it is the SAME
vCon, linked via the standard ``redacted`` property. This module introduces both
pieces as pure, unit-tested helpers, mirroring the framework-independent core
convention already used in :mod:`vcon_agent.vcon` and
:mod:`vcon_agent.enrichment` (no LiveKit, no boto3, no network).

What redaction does (and, deliberately, does NOT) touch:

  * PHI-bearing FREE TEXT is masked: party identifying ``name`` fields, dialog
    text-turn ``body`` strings, the transcript and summary analysis ``body``
    text, and the consent attachment's free-text ``script``.
  * STRUCTURE and turn INDICES are preserved exactly: same vCon version/subject/
    created_at, same parties/dialog/analysis arrays in the same order, same
    number of dialog turns, and every analysis entry keeps its ``dialog``
    turn-index provenance. This is what lets the viewer's medication →
    audio-seek click-through keep working on the redacted record: an entity
    still resolves to its analysis entry's dialog turn index, and that turn
    still carries its numeric ``start`` / ``duration``.
  * The ENTITIES analysis is kept INTACT (category + text of the meds and
    conditions). Those are the non-PHI clinical payload the analyst actually
    needs; dropping them would defeat the point of the redacted view.

🩹 Shortcut: demo-grade, field-level masking driven by a fixed field list and a
single ``[REDACTED]`` marker, applied to the known enriched vCon shape.
→ Production: a vetted de-identification service (e.g. Comprehend Medical
``DetectPHI``-driven redaction) with a configurable policy, so PHI embedded in
otherwise-clinical text is found and removed rather than relying on masking a
hand-enumerated set of fields.

The ``redacted`` link shape (per vcon.dev, and accepted as a first-class property
by the ``vcon`` PyPI library's ``Vcon.redacted``):

  * On the FULL doc::

        "redacted": {
            "uuid": "<redacted doc uuid>",
            "type": "redacted",
            "url": "vcon-redacted.sample.json"   # relative, sibling file
        }

    The relative ``url`` lets the static viewer fetch the redacted sibling file
    offline without any server-side resolution.
  * On the REDACTED doc, a back-reference names the origin so each side can name
    the other::

        "redacted": {"uuid": "<full doc uuid>", "type": "redacted_from"}

    The redacted doc always gets its OWN distinct ``uuid``.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, Optional

# The single marker used wherever PHI free text is removed. A sentinel string
# (not an empty value) so a consumer can tell "this was redacted" apart from
# "this field was never populated".
REDACTION_MARKER = "[REDACTED]"

# Default relative filename for the redacted sibling the viewer fetches. The
# ``redacted`` link on the full doc carries this as a relative ``url`` so the
# static, no-server viewer can load the pair from the same directory.
DEFAULT_REDACTED_URL = "vcon-redacted.sample.json"

# Link-object ``type`` markers. ``redacted`` points the full doc AT its redacted
# version; ``redacted_from`` is the back-reference on the redacted doc naming the
# origin full doc.
LINK_TYPE_REDACTED = "redacted"
LINK_TYPE_REDACTED_FROM = "redacted_from"

# Analysis entry types whose free-text ``body`` is masked. The ``entities``
# analysis is intentionally absent here: its body is the meds/conditions payload
# the analyst needs, and it carries no PHI free text.
_MASKED_ANALYSIS_TYPES = frozenset({"transcript", "summary"})


def redact_vcon(
    full_doc: Dict[str, Any],
    *,
    redacted_uuid: Optional[str] = None,
    redacted_url: str = DEFAULT_REDACTED_URL,
) -> Dict[str, Any]:
    """Return a NEW redacted copy of ``full_doc`` without mutating the input.

    The input is deep-copied first, so the caller's document is never changed in
    place. The copy is then de-identified per the module docstring (party names,
    dialog text bodies, transcript/summary analysis bodies, and the consent
    script are masked with :data:`REDACTION_MARKER`) while structure, ordering,
    dialog count, numeric ``start``/``duration`` on dialog turns, analysis
    order/types, and every analysis entry's ``dialog`` turn-index provenance are
    preserved. The ``entities`` analysis is kept intact.

    The returned redacted doc gets its OWN distinct ``uuid`` (``redacted_uuid``
    if provided, else a fresh uuid4) and a ``redacted`` back-reference naming the
    origin full doc's uuid (see :func:`link_redacted`). The full doc is NOT
    linked here (this function must not mutate its input); call
    :func:`link_redacted` to attach the forward link to a full-doc copy, or use
    :func:`build_redacted_pair` to get a linked ``(full, redacted)`` pair.

    🩹 Shortcut: masking is applied to a hand-enumerated set of known PHI fields
    in the demo vCon shape. → Production: policy-driven de-identification that
    detects PHI anywhere in free text (e.g. Comprehend Medical ``DetectPHI``).
    """
    redacted = copy.deepcopy(full_doc)

    # Distinct uuid for the redacted record. Each side of the pair is its own
    # vCon with its own identity; they are tied together only via the link.
    redacted["uuid"] = redacted_uuid or str(uuid.uuid4())

    # Parties: mask any identifying free-text ``name``. The ``role`` is kept (it
    # is not PHI and the viewer labels speakers by role). We keep the array and
    # its indices so dialog ``party`` references still resolve.
    for party in redacted.get("parties", []):
        if "name" in party:
            party["name"] = REDACTION_MARKER

    # Dialog: mask the free-text ``body`` of text turns only. Everything the
    # viewer needs to seek audio and resolve provenance is preserved:
    # ``type``, ``party``, ``start``, ``duration``, ``mimetype`` (and a recording
    # dialog object's ``url`` is NOT touched; it is a storage reference, not PHI
    # free text). The number and order of dialog turns is unchanged.
    for turn in redacted.get("dialog", []):
        if turn.get("type") == "text" and "body" in turn:
            turn["body"] = REDACTION_MARKER

    # Analysis: mask the transcript/summary free-text bodies; keep the entities
    # analysis (meds/conditions) intact. Every entry keeps its ``dialog``
    # turn-index provenance untouched so the viewer's click-through still maps an
    # entity to its dialog turn.
    for entry in redacted.get("analysis", []):
        if entry.get("type") in _MASKED_ANALYSIS_TYPES and "body" in entry:
            entry["body"] = REDACTION_MARKER

    # Attachments: mask the consent attachment's free-text ``script`` while
    # keeping its structure (type/party/granted/timestamp) so the redacted record
    # still documents THAT consent was captured, just not the verbatim text.
    for attachment in redacted.get("attachments", []):
        if attachment.get("type") == "consent" and "script" in attachment:
            attachment["script"] = REDACTION_MARKER

    # Back-reference: name the origin full doc on the redacted side so each side
    # can name the other. The full doc's forward link is attached separately so
    # this function never mutates its input.
    full_uuid = full_doc.get("uuid")
    redacted["redacted"] = {"uuid": full_uuid, "type": LINK_TYPE_REDACTED_FROM}

    return redacted


def link_redacted(
    full_doc: Dict[str, Any],
    redacted_doc: Dict[str, Any],
    *,
    redacted_url: str = DEFAULT_REDACTED_URL,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return linked COPIES of ``(full_doc, redacted_doc)``.

    Neither input is mutated (both are deep-copied first). The returned full copy
    gets the standard forward ``redacted`` link pointing at the redacted doc's
    uuid, plus a relative ``url`` so the static viewer can fetch the redacted
    sibling file offline. The returned redacted copy gets a ``redacted``
    back-reference naming the full doc's uuid (``redacted_from``). Per vcon.dev
    the ``redacted`` property points from a vCon to its redacted version; the
    ``vcon`` PyPI library accepts it as a first-class ``Vcon.redacted`` property.
    """
    full = copy.deepcopy(full_doc)
    red = copy.deepcopy(redacted_doc)

    full["redacted"] = {
        "uuid": red.get("uuid"),
        "type": LINK_TYPE_REDACTED,
        # Relative URL so the no-server viewer loads the sibling file offline.
        "url": redacted_url,
    }
    red["redacted"] = {"uuid": full.get("uuid"), "type": LINK_TYPE_REDACTED_FROM}
    return full, red


def build_redacted_pair(
    full_doc: Dict[str, Any],
    *,
    redacted_uuid: Optional[str] = None,
    redacted_url: str = DEFAULT_REDACTED_URL,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a fully linked ``(full, redacted)`` pair from a full vCon.

    Convenience wrapper over :func:`redact_vcon` + :func:`link_redacted`: it
    redacts ``full_doc`` (without mutating it) and returns a NEW full copy
    carrying the forward ``redacted`` link alongside the redacted copy carrying
    the back-reference. This is the single call the fixture author / CLI uses so
    the committed redacted sample stays consistent with the helper.
    """
    redacted = redact_vcon(
        full_doc, redacted_uuid=redacted_uuid, redacted_url=redacted_url
    )
    return link_redacted(full_doc, redacted, redacted_url=redacted_url)
