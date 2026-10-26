"""LiveKit Agents entrypoint for the scripted patient-intake agent (Spec 1).

This module is the only part of ``vcon_agent`` that depends on LiveKit. It wires
the real-time voice leg (Deepgram STT + ElevenLabs TTS + an OpenAI LLM) onto the
framework-independent core from FEAT-002:

* :class:`~vcon_agent.vcon.VConBuilder` builds the unsigned vCon incrementally,
  turn-by-turn, *during* the call. Party index 0 is the patient, index 1 is the
  agent.
* :func:`~vcon_agent.persistence.persist_vcon` flushes the finished document once
  on session end -- a single PUT to S3 (under the Spec 0 unique-key-per-run
  prefix, then a guarded Step Functions StartExecution) when ``BUCKET_NAME`` is
  set, or a local-file fallback otherwise.

Design note on testability: the one piece of glue that matters for correctness
-- translating a committed ``ChatMessage`` into a vCon dialog turn -- lives in the
pure :func:`handle_item` helper. That helper has no LiveKit/network dependency,
so ``tests/test_agent.py`` can exercise the mapping (user -> party 0, assistant
-> party 1, in order) without ever starting a real :class:`AgentSession` or using
credentials. The live voice path requires real LiveKit/Deepgram/ElevenLabs/LLM
credentials and is out of scope for automated verification.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram, elevenlabs, openai

from .persistence import persist_vcon
from .vcon import DEFAULT_CONSENT_SCRIPT, VConBuilder, role_to_index

logger = logging.getLogger("vcon_agent.agent")

# Roles that correspond to spoken dialog turns we record in the vCon. Other
# ChatContext roles (``system``/``developer``) are prompt scaffolding, not call
# content, so they are ignored by :func:`handle_item`.
_DIALOG_ROLES = ("user", "assistant")

# Scripted intake instructions for the agent. The agent speaks the hardcoded
# consent script FIRST (recorded up front via add_consent), then conducts a brief
# scripted intake. This is a demo script, not clinical guidance.
INTAKE_INSTRUCTIONS = (
    "You are a friendly medical intake assistant for a clinic. "
    "The very first thing you must do when the call starts is read this consent "
    "script verbatim to the patient and wait for their answer:\n\n"
    f'"{DEFAULT_CONSENT_SCRIPT}"\n\n'
    "After the patient responds about consent, conduct a brief intake: ask for "
    "the reason for today's visit, how long the symptoms have lasted, and any "
    "medications they are currently taking. Ask one short question at a time, "
    "acknowledge each answer, and keep your turns concise. Do not provide "
    "diagnoses or medical advice; you are only collecting intake information."
)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def handle_item(builder: VConBuilder, role: str, text: str) -> bool:
    """Translate one committed conversation item into a vCon dialog turn.

    This is the pure glue between LiveKit's ``conversation_item_added`` event and
    the FEAT-002 vCon model. It has no LiveKit or network dependency so it can be
    unit-tested directly by feeding a sequence of ``(role, text)`` items.

    ``role`` is mapped to a fixed party index via
    :func:`~vcon_agent.vcon.role_to_index` (``user`` -> 0 patient,
    ``assistant`` -> 1 agent) and the turn is appended in order. Non-dialog roles
    (``system``/``developer``) and empty/whitespace-only text are skipped.

    Returns ``True`` if a dialog turn was appended, ``False`` if the item was
    skipped.
    """
    if role is None:
        return False
    normalized = role.strip().lower()
    if normalized not in _DIALOG_ROLES:
        return False
    if text is None or not text.strip():
        return False
    builder.add_dialog_turn(role_to_index(normalized), text)
    return True


async def entrypoint(ctx: JobContext) -> None:
    """LiveKit worker entrypoint: run one scripted intake call.

    Builds the AgentSession (Deepgram STT + ElevenLabs TTS + OpenAI LLM), records
    consent up front, appends a vCon dialog turn on each committed conversation
    item, and persists the finished vCon exactly once on session end.
    """
    # Config (LiveKit/Deepgram/ElevenLabs/LLM keys + optional AWS vars) is read
    # from the process environment; load a local .env if present. Plugins and the
    # worker read their credentials straight from os.environ -- no secrets here.
    load_dotenv()

    room_name = ctx.room.name
    session_start_iso = _utc_now_iso()

    # Build the unsigned vCon incrementally during the call and record consent
    # BEFORE any dialog turn is appended (the agent speaks the consent script as
    # its first utterance; the boolean attachment is captured up front per the
    # acknowledged shortcut).
    #
    # SHORTCUT: ``granted`` is hardcoded to True and is NOT yet derived from the
    # patient's actual spoken answer. The agent reads the consent script and the
    # patient's yes/no reply arrives later as an ordinary conversation item that
    # lands in dialog[]; nothing wires that reply back into this boolean, and
    # there is no consent-denied/abort path. Binding the recorded boolean to the
    # patient's real response (and refusing to proceed on denial) is a Production
    # concern per the spec's acknowledged consent shortcut.
    builder = VConBuilder()
    builder.add_consent(granted=True, script=DEFAULT_CONSENT_SCRIPT)

    session = AgentSession(
        stt=deepgram.STT(),
        tts=elevenlabs.TTS(),
        llm=openai.LLM(),
    )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event: ConversationItemAddedEvent) -> None:
        # Translate the committed ChatMessage into a vCon dialog turn DURING the
        # call. Delegates to the pure helper so the mapping is unit-testable.
        item = event.item
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None)
        try:
            handle_item(builder, role, text)
        except Exception:  # pragma: no cover - defensive; never break the call
            logger.exception("failed to record dialog turn (role=%r)", role)

    # Persist ONCE on session end: the single flush-to-S3 PUT (acknowledged
    # shortcut), with the local-file fallback when the Spec 0 env is absent and a
    # guarded StartExecution when STATE_MACHINE_ARN is set.
    async def _flush_vcon() -> None:
        try:
            result = persist_vcon(
                builder.to_dict(),
                room_name,
                session_start_iso,
            )
            if result.s3_fallback_to_local:
                logger.warning(
                    "S3 flush failed; vCon salvaged to local file %s (%s)",
                    result.location,
                    result.destination,
                )
            else:
                logger.info(
                    "persisted vCon to %s (%s); enrichment_triggered=%s",
                    result.location,
                    result.destination,
                    result.enrichment_triggered,
                )
        except Exception:  # pragma: no cover - defensive; log and swallow
            logger.exception("failed to persist vCon on session end")

    ctx.add_shutdown_callback(_flush_vcon)

    await ctx.connect()
    await session.start(
        agent=Agent(instructions=INTAKE_INSTRUCTIONS),
        room=ctx.room,
    )

    # Speak first: the agent opens the call by reading the consent script, then
    # proceeds with the scripted intake described in INTAKE_INSTRUCTIONS.
    await session.generate_reply(
        instructions=(
            "Greet the patient briefly, then read the consent script verbatim "
            "and ask whether they consent."
        ),
    )


def main() -> None:
    """Run the agent as a LiveKit worker (dev/console/start modes via the CLI)."""
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    main()
