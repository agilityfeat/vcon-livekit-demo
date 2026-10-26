"""LiveKit Agents entrypoint for the scripted patient-intake agent.

This module is the only part of ``vcon_agent`` that depends on LiveKit. It wires
the real-time voice leg (Deepgram STT + ElevenLabs TTS + an OpenAI LLM) onto the
framework-independent core in ``vcon.py`` and ``persistence.py``:

* :class:`~vcon_agent.vcon.VConBuilder` builds the unsigned vCon incrementally,
  turn-by-turn, *during* the call. Party index 0 is the patient, index 1 is the
  agent.
* :func:`~vcon_agent.persistence.persist_vcon` flushes the finished document once
  on session end -- a single PUT to S3 (under the unique-key-per-run
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
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from dotenv import load_dotenv

from livekit import api
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
from .vcon import DEFAULT_CONSENT_SCRIPT, VConBuilder, recording_key, role_to_index

logger = logging.getLogger("vcon_agent.agent")

# Environment variable names for the egress seam (bucket/region mirror
# persistence.py; the LiveKit creds are the same ones the worker/plugins read).
ENV_BUCKET_NAME = "BUCKET_NAME"
ENV_AWS_REGION = "AWS_REGION"
ENV_AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
ENV_AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
ENV_LIVEKIT_URL = "LIVEKIT_URL"
ENV_LIVEKIT_API_KEY = "LIVEKIT_API_KEY"
ENV_LIVEKIT_API_SECRET = "LIVEKIT_API_SECRET"

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
    the vCon model in ``vcon.py``. It has no LiveKit or network dependency so it can be
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


def build_egress_request(
    room_name: str,
    bucket: str,
    region: Optional[str],
    filepath: str,
    access_key: Optional[str],
    secret: Optional[str],
) -> "api.RoomCompositeEgressRequest":
    """Build the LiveKit room-composite AUDIO egress request (pure helper).

    Constructs a :class:`api.RoomCompositeEgressRequest` with ``audio_only=True``
    and a single OGG :class:`api.EncodedFileOutput` whose ``filepath`` is the
    per-run recording key, uploaded to S3 via :class:`api.S3Upload`. This is a
    pure value builder with no network call, so tests can assert the request
    shape (audio-only, OGG, filepath, bucket/region) without touching LiveKit.

    SHORTCUT (acknowledged): audio only (no video) and plain SSE-S3 via the
    S3Upload credentials (no CMK). Production would make audio/video configurable
    per clinical workflow and use SSE-KMS with a customer-managed key.
    """
    return api.RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.OGG,
                filepath=filepath,
                s3=api.S3Upload(
                    bucket=bucket,
                    region=region or "",
                    access_key=access_key or "",
                    secret=secret or "",
                ),
            )
        ],
    )


def recording_url(bucket: str, key: str) -> str:
    """Return the canonical ``s3://<bucket>/<key>`` url for the recording object.

    Used to thread the recording location into the vCon's recording dialog. When
    an :class:`api.EgressInfo` result is available its ``file_results[].location``
    can be preferred, but the derived ``s3://`` url is always well defined from
    the bucket and the per-run recording key.
    """
    return f"s3://{bucket}/{key}"


async def _default_start_egress(
    url: str,
    api_key: str,
    api_secret: str,
    request: "api.RoomCompositeEgressRequest",
) -> Any:
    """Live egress start: the one place that talks to LiveKit over the network.

    Kept behind the injectable ``start_egress`` seam in :func:`entrypoint` so
    unit tests pass a fake and never open a connection. This live path (real
    LiveKit/AWS) is NOT automatically verifiable in the sandbox, consistent with
    the live voice path, which also is not automatically verifiable.
    """
    client = api.LiveKitAPI(url, api_key, api_secret)
    try:
        return await client.egress.start_room_composite_egress(request)
    finally:
        await client.aclose()


async def _maybe_start_recording_egress(
    builder: VConBuilder,
    room_name: str,
    session_start_iso: str,
    timestamp: str,
    *,
    env: Optional[dict] = None,
    start_egress: Optional[
        Callable[[str, str, str, "api.RoomCompositeEgressRequest"], Awaitable[Any]]
    ] = None,
) -> Optional[str]:
    """Start the audio egress and thread the recording url into the vCon.

    Reads config from ``env`` (defaults to ``os.environ``). If ``BUCKET_NAME`` or
    the LiveKit creds are absent, egress is skipped gracefully (logged) so the
    standalone local demo still runs -- mirroring persistence.py's env-optional
    behavior. The whole live call is guarded so a failure never breaks the call.

    The recording key is derived from the SAME ``(room_name, session_start_iso,
    timestamp)`` triple used for the vCon flush, so ``recording.ogg`` and
    ``vcon-skeleton.json`` share the run prefix. Returns the recording ``s3://``
    url when a recording dialog was added, else ``None``.

    ``start_egress`` is an injectable async callable (defaults to the live
    :func:`_default_start_egress`) so tests drive a fake and stay fully offline.
    """
    if env is None:
        env = os.environ
    if start_egress is None:
        start_egress = _default_start_egress

    bucket = env.get(ENV_BUCKET_NAME)
    lk_url = env.get(ENV_LIVEKIT_URL)
    lk_key = env.get(ENV_LIVEKIT_API_KEY)
    lk_secret = env.get(ENV_LIVEKIT_API_SECRET)

    # Env-optional, like persistence.py: no bucket or no LiveKit creds -> skip
    # egress (logged) and keep the standalone local demo working.
    if not bucket or not lk_url or not lk_key or not lk_secret:
        logger.info(
            "egress skipped: BUCKET_NAME/LiveKit creds not fully configured "
            "(standalone local demo mode)"
        )
        return None

    key = recording_key(room_name, session_start_iso, timestamp=timestamp)
    url = recording_url(bucket, key)
    try:
        request = build_egress_request(
            room_name=room_name,
            bucket=bucket,
            region=env.get(ENV_AWS_REGION),
            filepath=key,
            access_key=env.get(ENV_AWS_ACCESS_KEY_ID),
            secret=env.get(ENV_AWS_SECRET_ACCESS_KEY),
        )
        info = await start_egress(lk_url, lk_key, lk_secret, request)

        # Prefer the egress-reported location when present; otherwise fall back
        # to the derived s3:// url for the per-run recording key.
        file_results = getattr(info, "file_results", None) or []
        if file_results:
            location = getattr(file_results[0], "location", None)
            if location:
                url = location
        egress_id = getattr(info, "egress_id", None)
        logger.info(
            "started room-composite audio egress (egress_id=%s) -> %s",
            egress_id,
            url,
        )
    except Exception:  # defensive: never break the call on an egress error
        logger.exception("failed to start recording egress; continuing call")
        # Still record the intended recording location on the vCon so the
        # enrichment pipeline's S3 poll has a key to look for even if the start
        # call hiccuped.

    # Thread the recording location into the vCon as an external reference.
    builder.add_recording_dialog(url)
    return url


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
    # Compute ONE timestamp per run and share it between the recording egress key
    # and the vCon flush so recording.ogg and vcon-skeleton.json land under the
    # SAME run prefix for this call.
    run_timestamp = _utc_now_iso()

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
    # concern per the acknowledged consent shortcut.
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
    # shortcut), with the local-file fallback when the AWS backend env is absent and a
    # guarded StartExecution when STATE_MACHINE_ARN is set.
    async def _flush_vcon() -> None:
        try:
            result = persist_vcon(
                builder.to_dict(),
                room_name,
                session_start_iso,
                timestamp=run_timestamp,
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

    # Start the room-composite AUDIO egress now that the agent has joined. The
    # recording.ogg key shares the run prefix with the vCon flush (same
    # room/session_start/timestamp triple), and the resulting s3:// url is
    # threaded into the vCon as a recording dialog object BEFORE the shutdown
    # flush, so the persisted vCon carries the recording location. Egress is
    # skipped gracefully when BUCKET_NAME/LiveKit creds are absent and guarded so
    # it never breaks the call.
    #
    # NOT on the critical path: we deliberately do NOT drive processing from an
    # ``egress_ended`` webhook/event. The enrichment pipeline polls S3 at the
    # recording key for readiness instead, which sidesteps the duplicate / early
    # / missing-webhook
    # failure modes. A real deployment MAY still subscribe to egress status for
    # logging/observability only, but that is never the trigger for enrichment.
    await _maybe_start_recording_egress(
        builder,
        room_name,
        session_start_iso,
        run_timestamp,
    )

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
