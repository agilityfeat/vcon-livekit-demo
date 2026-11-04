# Enrichment pipeline

After the call, a Step Functions state machine (`infrastructure/template.yaml`)
runs the AI enrichment and writes the results into the vCon `analysis[]` with
turn-level provenance. Its steps mirror the pure, offline-tested logic in
`src/vcon_agent/enrichment.py` and `src/vcon_agent/vcon.py`. Deploying and
inspecting it is covered in [infrastructure.md](infrastructure.md).

## Trigger and idempotency

Today the local agent starts the pipeline itself. On session end it PUTs the
skeleton vCon to S3 and calls Step Functions `StartExecution` (boto3, local AWS
credentials) with input `{"vcon": <doc>}` and a **deterministic execution name**
from `vcon.execution_name`. The name is a sanitized prefix of the room name plus
the first 16 hex characters of a SHA-256 over `room_name|session_start_iso`, capped
at 80 characters. Step Functions rejects a duplicate name, so a retried start is a
no-op rather than a second run.

There is no EventBridge rule or webhook on the critical path. The planned
event-driven alternative is described in
[production-considerations.md](production-considerations.md#triggering-and-persistence).

**Why Step Functions rather than one Lambda.** Transcribe Medical batch is
asynchronous and can outlast Lambda's 15-minute limit. The state machine owns the
waiting (a `Wait` state, a poll of the job, and a `Choice` loop) and the retry and
catch behavior, so no Lambda sleeps past its timeout. Each Lambda does one short
unit of work and returns.

## States

```
WaitForRecording      poll S3 (HeadObject) for the recording object
  -> IsRecordingReady (Choice): not ready -> WaitBeforeRecording (5s) -> WaitForRecording
LoadSkeleton          load the skeleton vCon for this run (pass-through today)
StartTranscription    start the Transcribe Medical BATCH job (async), return the job name
  -> WaitForTranscription (Wait 5s)
  -> GetTranscription   (poll the job once)
  -> IsTranscriptionComplete (Choice):
       COMPLETED -> Analyze
       FAILED    -> TranscriptionFailed (Fail)
       otherwise -> WaitForTranscription   (the state machine owns the wait)
Analyze               Parallel: Comprehend (meds + conditions) | Bedrock (summary)
Merge                 write analysis[] entries with dialog turn-index provenance
Seal                  sign (JWS), encrypt (JWE), redact; see sign-encrypt-redact.md
Done                  Succeed
```

The Lambda behind each state is named `<ProjectName>-<Environment>-<step>-step`
(`poll-recording`, `load-skeleton`, `start-transcription`, `get-transcription`,
`comprehend`, `bedrock`, `merge`, `seal`).

## What the pipeline does

1. **Poll S3 for the recording.** `poll-recording-step` takes the recording key from
   the vCon's recording dialog object and `HeadObject`s it, so the pipeline never
   transcribes a missing or partial file. A `Choice` plus `Wait` loop retries every
   5 seconds. If there is no recording key at all (for example the canned input used
   by `validate-live.sh`), it reports ready so the canned path can run.
2. **Load the skeleton.** `load-skeleton-step` forwards the vCon already on the
   envelope. It also has a dormant `skeleton_key` branch (see
   [production-considerations.md](production-considerations.md#triggering-and-persistence)).
3. **Transcribe Medical (batch, authoritative).** `start-transcription-step` starts a
   `PRIMARYCARE` / `CONVERSATION` job with speaker labels (`MaxSpeakerLabels: 2`).
   The state machine polls until it completes. The result is stored as an
   `analysis[]` **transcript** entry marked `authoritative`, covering the text
   dialog turns, and kept **alongside** the live Deepgram transcript in `dialog[]`.
   When the result carries speaker segments, the entry also gets structured `turns`
   (`{party, speaker, start, end, text}`) next to the flat `body`.
4. **Comprehend Medical (meds + conditions).** Clinical entities are extracted from
   the authoritative transcript and filtered to the `MEDICATION` and
   `MEDICAL_CONDITION` categories. They are stored as an **entities** entry.
5. **Bedrock (physician summary).** A concise physician-facing summary is built from
   the authoritative transcript text, not the live `dialog[]` turns, and stored as a
   **summary** entry. The live STT can mis-hear a clinical term (a medication name
   heard as a common word); Transcribe Medical captures it correctly, so the summary
   does too. The model id is the `BedrockModelId` parameter (see
   [infrastructure.md](infrastructure.md#bedrock-model-access)).

Comprehend and Bedrock run as a **parallel** branch. The merge step writes the three
analysis entries in a fixed order: `transcript`, `entities`, `summary`.

### Failure behavior

- **Transcription fails closed.** A job that reports `FAILED` goes to the
  `TranscriptionFailed` state.
- **Comprehend and Bedrock fail open.** If a model call is denied, throttled, or the
  Bedrock model is not enabled, the step returns an empty result and attaches a
  `degraded` reason string to its `analysis[]` entry instead of failing the run. The
  key is present only on a failure, so a genuinely empty result is distinguishable
  from a silent failure. On a healthy run `validate-live.sh` asserts it is absent.

### Dual transcript: a feature, not a bug

The real-time agent STT (Deepgram) is tuned for latency; Transcribe Medical is tuned
for clinical accuracy. Rather than discard one, the vCon holds both with provenance:
the live transcript stays in `dialog[]`, and the clinical transcript is an
`analysis[]` entry marked `authoritative`. Nothing has to reconcile a Deepgram record
and an AWS record by hand.

### Provenance

- **Transcript**: `dialog` lists every text dialog turn index.
- **Entities**: `dialog` is a single index, the first patient text turn.
- **Summary**: `dialog` lists every text dialog turn index.
- **Speaker attribution of transcript `turns`**: the segment times are relative to
  the recording start while live dialog `start` values are relative to the session
  connect, so the two clocks have different origins and time overlap resolves
  wrongly. `enrichment.reconcile_segments_to_parties` instead maps Transcribe's
  `spk_N` labels to parties by speaking order: the label that speaks first is the
  agent (the scripted bot opens with the consent script), and the other is the
  patient. A single label, a tie, or an unorderable start leaves `party` as `None`
  and the viewer shows a generic speaker label.

`enrichment.map_turns_by_timestamp_overlap` is unit-tested but not wired into the
pipeline.

## Offline verification

The pipeline needs live AWS to run, so it is verified offline like the rest of the
repo. The shaping and provenance logic lives in pure helpers
(`src/vcon_agent/enrichment.py` and the `add_*_analysis` helpers in `vcon.py`) that
are unit-tested with canned AWS-shaped inputs. The inline Lambda bodies are extracted
from the template and compiled, and the Merge body, which imports no `boto3`, is
executed and compared to the pure core (`tests/test_structured_transcript_wiring.py`,
`tests/test_seal_pipeline_wiring.py`). The live end-to-end check is
`infrastructure/scripts/validate-live.sh`; see [validation.md](validation.md).

Shortcuts for this pipeline (stall guard, canned transcript stub, entity attribution,
and so on) are listed in
[production-considerations.md](production-considerations.md#enrichment-pipeline).
