# Intake agent

The intake agent is a [LiveKit Agents](https://docs.livekit.io/agents/) worker
(`src/vcon_agent/agent.py`) that joins a room and runs a short, scripted patient
intake over a real-time voice leg:

- **Deepgram** for speech-to-text (STT),
- **ElevenLabs** for text-to-speech (TTS), and
- an **OpenAI** LLM driving the scripted conversation.

It speaks first: it greets the patient, reads the consent script, and then runs the
intake described by `INTAKE_INSTRUCTIONS`.

To run it, see [getting-started.md](getting-started.md#running-things). The live
voice path needs real credentials and cannot be exercised offline.

## Live-built vCon

As the call happens, the agent builds an unsigned vCon incrementally, turn by turn,
using the framework-independent core in `src/vcon_agent/vcon.py` (`VConBuilder`):

- **Two fixed parties.** Index `0` is the patient (`role: patient`), index `1` is the
  agent (`role: agent`). Dialog turns reference parties by index, never by an
  embedded identity.
- **Consent first.** The consent attachment (`type: consent`, `party: 0`, a boolean,
  the script text, and a timestamp) is recorded before any dialog turn is appended;
  `add_dialog_turn` raises if it is not. The recorded boolean is **hardcoded to
  `True`** and is not derived from the patient's answer (see
  [production-considerations.md](production-considerations.md#consent)).
- **Dialog during the call.** On each `conversation_item_added` event the committed
  message becomes a dialog turn (`user` maps to party 0, `assistant` to party 1),
  appended in order. The mapping lives in the pure `handle_item(builder, role,
  text)` helper so it is unit-testable without a live session.
- **Timeline.** Each turn's `start` is the numeric offset in seconds from the start
  of the session, and the wall-clock instant is kept in `start_iso`. A numeric
  `start` lets downstream code and the viewer place turns on a timeline.

## Persist on session end

When the session ends, a shutdown callback flushes the finished vCon exactly once
(`src/vcon_agent/persistence.py`, run in a worker thread so it does not block the
event loop):

- **`BUCKET_NAME` set.** PUT the vCon JSON to the bucket at
  `<room_name>/<session_start_iso>/vcon-skeleton.json`. Then, only if
  `STATE_MACHINE_ARN` is also set, call Step Functions `StartExecution` with input
  `{"vcon": <doc>}` and a deterministic execution name (see
  [enrichment-pipeline.md](enrichment-pipeline.md#trigger-and-idempotency)).
- **`BUCKET_NAME` unset.** Write the same document to `vcon-output/<room_name>/<session_start_iso>/vcon-skeleton.json`
  in the working directory and skip enrichment, so the agent is demoable with no
  AWS backend.
- **Fail-safe.** The single PUT is the call's only persistence opportunity. If the S3
  PUT or the guarded `StartExecution` raises, the vCon is salvaged to the same
  `vcon-output/` layout and a warning is logged, instead of being lost.

The agent does not pass a `skeleton_key` to the pipeline; it inlines the skeleton by
value. See
[production-considerations.md](production-considerations.md#triggering-and-persistence)
for why the `LoadSkeleton` state is kept anyway.

## Call recording

The agent captures the call audio to S3 and records its location on the vCon so the
pipeline can find and transcribe it.

Right after `ctx.connect()`, the agent starts a LiveKit **audio-only egress** with
the unified `StartEgress` API and a `TemplateSource(audio_only=True)` source. It
writes a single mixed OGG recording for the whole room directly to the bucket.
(`StartEgress` with a template source replaces the deprecated
`StartRoomCompositeEgress`; the audio-only template keeps the one-mixed-file model.)

### Where the recording lands

The egress writes to the same run prefix the vCon flush uses:

```
<room_name>/<session_start_iso>/recording.ogg
<room_name>/<session_start_iso>/vcon-skeleton.json
```

`session_start_iso` is a microsecond-precision UTC instant captured once on entry
and shared by both keys, so `recording_key(...)` and `run_key(...)` always resolve
to the same prefix. The unique key plus the bucket's versioning neutralize the
failure mode where a later egress overwrites an earlier file. The full key list is
in [infrastructure.md](infrastructure.md#s3-key-convention).

LiveKit Egress also writes a sibling `EG_<id>.json` manifest next to `recording.ogg`.
It is kept on purpose for provenance (egress id, started and ended timings, resolved
location). The pipeline keys off `recording.ogg` and ignores the manifest; set
`disable_manifest=True` on the egress `FileOutput` if you want a bucket holding only
`recording.ogg`.

### Recording location on the vCon

The location is recorded as a **recording dialog object** (`type: "recording"`) via
`VConBuilder.add_recording_dialog(url, ...)`. The audio is an external reference: the
dialog carries `s3://<bucket>/<key>` in `url` and never inlines the bytes. It
represents the whole call, so it is not tied to a party index. The dialog is added
before the shutdown flush, so the persisted `vcon-skeleton.json` already carries it.
It is added even if starting the egress raised, so the pipeline still has a key to
poll for.

### Readiness is decoupled from egress webhooks

Readiness is poll-based: the pipeline polls S3 for the object at the known recording
key. This sidesteps the duplicate, early, and missing webhook failure modes. The
agent may subscribe to `egress_ended` for logging only; it is not on the critical
path and triggers no processing.

### Env-optional

Live egress needs `BUCKET_NAME` and the LiveKit credentials (`LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`); `AWS_REGION` is passed to the S3 upload
configuration. If `BUCKET_NAME` or any LiveKit credential is missing, egress is
skipped with a log line, no recording dialog is added, and the call is never broken.
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are used as the S3 upload credentials
when present and otherwise egress falls back to the instance role.

The live voice and egress paths are not part of automated verification. The egress
call sits behind an injectable seam, so the recording-key helper, the recording dialog
object, and the egress-request builder are unit-tested offline with fakes.

## Tests

`tests/test_agent.py`, `tests/test_vcon.py`, and `tests/test_persistence.py` cover the
turn mapping, builder rules, key helpers, and the persistence branches (including the
salvage path) with fakes. See [getting-started.md](getting-started.md#running-the-tests).

Out of scope for the demo: authentication and authorization, multi-party calls, and
any EHR integration. Shortcuts and their production counterparts are listed in
[production-considerations.md](production-considerations.md).
