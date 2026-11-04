# Architecture

A patient places a real voice call to an intake bot from the browser. The call is
recorded, turned into a [vCon](https://vcon.dev/) document, enriched by an AWS
pipeline, signed, encrypted and redacted, and then reviewed by a doctor (full
record) or an analyst (redacted record).

```
 Browser (patient)                LiveKit                 Agent process (local)
 frontend/call.html  --token-->  room  <--joins--  python -m vcon_agent.agent
        ^                          |                    builds vCon turn by turn
        | POST /api/token          | audio egress       starts egress, flushes on end
        |                          v                          |
 Backend (FastAPI)              S3 bucket  <---PUT skeleton---+
 python -m uvicorn ...          <room>/<session_start_iso>/   |
        |                          |                          | StartExecution
        | decrypt + verify         |                          v
        | (JWE / JWS)              |            Step Functions state machine
        v                          |   poll recording -> load skeleton -> Transcribe Medical
 Browser (doctor / analyst) <------+   -> Comprehend Medical | Bedrock -> merge -> seal
 frontend/doctor.html, analyst.html        (writes vcon-signed.jws, vcon-full.jwe,
                                            vcon-redacted.jws back to the same prefix)
```

## Components

| Component | Where | Runs | Role |
|---|---|---|---|
| Intake agent | `src/vcon_agent/agent.py` | Locally, as a LiveKit Agents worker | Voice conversation (Deepgram STT, ElevenLabs TTS, OpenAI LLM), builds the vCon, starts the audio egress, persists on session end. See [intake-agent.md](intake-agent.md). |
| vCon core | `src/vcon_agent/vcon.py` | Library | Framework-independent builder and key conventions (`run_key`, `recording_key`, `execution_name`). |
| Persistence | `src/vcon_agent/persistence.py` | In the agent process | One S3 PUT, optional `StartExecution`, local-file fallback. |
| Enrichment helpers | `src/vcon_agent/enrichment.py` | Library, tested offline | Pure logic that the inline Lambdas mirror by hand. See [enrichment-pipeline.md](enrichment-pipeline.md). |
| AWS backend | `infrastructure/template.yaml` | AWS | S3 bucket, Step Functions state machine, seven inline Lambdas, the packaged Seal Lambda, IAM. See [infrastructure.md](infrastructure.md). |
| Seal step | `infrastructure/functions/seal/` | AWS Lambda (SAM, container build) | Sign, encrypt, redact. See [sign-encrypt-redact.md](sign-encrypt-redact.md). |
| Demo backend | `src/vcon_agent/app/` | Locally, FastAPI + uvicorn | Mints LiveKit tokens, lists records, decrypts and verifies server-side, serves `frontend/`. See [demo-app.md](demo-app.md). |
| Frontend | `frontend/` | Browser | Vanilla multi-page UI: landing, patient call, doctor and analyst dashboards, record viewer. |

## Data flow

1. **Call.** The browser asks the backend for a LiveKit token (`POST /api/token`),
   joins a room, and publishes its microphone. The agent worker joins the same
   room and speaks first (the consent script).
2. **Record.** After connecting, the agent starts a LiveKit audio-only egress that
   writes one mixed `recording.ogg` straight to the S3 bucket.
3. **Build.** During the call the agent appends each committed conversation turn
   to an unsigned vCon (patient is party `0`, agent is party `1`).
4. **Flush and trigger.** When the session ends the agent PUTs
   `vcon-skeleton.json` under the same run prefix as the recording, then starts the
   Step Functions execution with the skeleton inlined as input. Without
   `BUCKET_NAME` it writes to `vcon-output/` instead and enrichment is skipped.
5. **Enrich.** The state machine waits for the recording, runs Transcribe Medical
   as an asynchronous batch job, then Comprehend Medical and Bedrock in parallel,
   and merges the results into `analysis[]`.
6. **Seal.** The Seal Lambda signs the enriched vCon (JWS), encrypts the signed
   copy (JWE), and writes a signed, redacted sibling that points back at the
   encrypted full copy.
7. **View.** The backend scans the bucket for `vcon-signed.jws`, decrypts or
   verifies the artifacts with the shared demo key, and returns readable JSON. The
   browser never sees the key.

## S3 layout

Every run writes under a unique prefix `<room_name>/<session_start_iso>/`. See
[infrastructure.md](infrastructure.md#s3-key-convention) for the object list.

## Design notes

- **No AWS in the agent process, apart from the upload.** Signing, encryption,
  redaction, and all AI services run in the pipeline. The agent's AWS touchpoints
  today are the S3 PUT and the `StartExecution` call; the target is a single PUT
  (see [production-considerations.md](production-considerations.md#triggering-and-persistence)).
- **Pure cores, hand-mirrored Lambdas.** Correctness-critical logic lives in pure
  modules that the pytest suite exercises with canned inputs. Inline Lambda code
  cannot import the repo package at AWS runtime, so it mirrors those modules; the
  Seal Lambda is the exception because it ships `sealing.py` in its package.
- **Dual transcript.** The live Deepgram transcript stays in `dialog[]`; the
  clinical Transcribe Medical transcript is added to `analysis[]` marked
  `authoritative`. See [enrichment-pipeline.md](enrichment-pipeline.md).
- **Server-side crypto.** The backend decrypts and verifies; the browser only
  renders JSON.
- **Readiness is polled, not pushed.** The pipeline polls S3 for the recording
  instead of reacting to an egress webhook.

## Compliance

This is a proof of concept. It demonstrates the mechanisms vCon provides for
consent, integrity, and redaction. It is not a HIPAA-certified deployment and
must not be used with real PHI. The demo shortcuts and their production
counterparts are collected in [production-considerations.md](production-considerations.md).
