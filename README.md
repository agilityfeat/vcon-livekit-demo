# vCon + LiveKit demo

A demo that pairs a local [LiveKit](https://livekit.io/) agent with an AWS
enrichment backend. The agent records calls locally, and the backend runs an
enrichment pipeline (transcription, medical entity / PHI detection, and
summarization) over the recordings, producing [vCon](https://vcon.dev/) JSON
documents.

## Components

- **Local LiveKit agent** — joins a room and records the call.
- **AWS enrichment backend** — an S3 bucket, a Step Functions state machine, the
  pipeline Lambda functions, and the IAM roles that tie them together, all
  provisioned by a single CloudFormation template.

## Infrastructure

The AWS backend is defined in one flat CloudFormation template
(`infrastructure/template.yaml`): an S3 bucket for recordings and vCon JSON, a
Step Functions state machine orchestrating the stubbed enrichment Lambdas, and
the IAM roles granting access to Transcribe Medical, Comprehend Medical,
Bedrock, and S3.

For full deploy, configuration, and teardown instructions, including the
one-command deploy, mapping stack Outputs into a local `.env` (copied from
`.env.example`), the manual Bedrock model-access prerequisite, and local
validation, see **[infrastructure/README.md](infrastructure/README.md)**.

## Intake agent (Spec 1)

The intake agent is a [LiveKit Agents](https://docs.livekit.io/agents/) worker
(`src/vcon_agent/agent.py`) that joins a room and runs a short, scripted patient
intake over a real-time voice leg:

- **Deepgram** for speech-to-text (STT),
- **ElevenLabs** for text-to-speech (TTS), and
- an **OpenAI** LLM driving the scripted conversation.

### Live-built vCon

As the call happens, the agent builds an unsigned [vCon](https://vcon.dev/) JSON
document **incrementally, turn by turn**, using the framework-independent core in
`src/vcon_agent/vcon.py`:

- **Two fixed parties:** index `0` is the patient, index `1` is the agent.
  Dialog turns reference parties by index, never by an embedded identity.
- **Consent first:** the agent speaks a hardcoded consent script as its opening
  utterance, and the consent attachment (a boolean plus the script text) is
  recorded up front, before any dialog turn is appended.
- **Dialog during the call:** on each `conversation_item_added` event, the
  committed message is translated into a dialog turn (`user` → party 0,
  `assistant` → party 1) and appended in order. That translation lives in the
  pure `handle_item(builder, role, text)` helper so it is unit-testable without a
  live session.

### Persist on session end

When the session ends, the agent flushes the finished vCon **exactly once**
(`src/vcon_agent/persistence.py`):

- If `BUCKET_NAME` is set, it PUTs the vCon JSON to S3 under the Spec 0
  unique-key-per-run convention
  `<room_name>/<session_start_iso>/<timestamp>/vcon-skeleton.json`, then (only if
  `STATE_MACHINE_ARN` is also set) triggers enrichment via a guarded Step
  Functions `StartExecution` with input `{"vcon": <doc>}`.
- If `BUCKET_NAME` is unset, it writes the same document to a local file under
  `vcon-output/` using the identical run-key layout and skips enrichment, so the
  agent is demoable standalone with no AWS backend.

### Setup

Run from the repo root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # then fill in the LiveKit/Deepgram/ElevenLabs/LLM keys
```

The `.env` file configures the LiveKit connection
(`LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`), the provider keys
(`DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `OPENAI_API_KEY`), and the optional
AWS backend (`BUCKET_NAME`, `STATE_MACHINE_ARN`, `AWS_REGION`). The AWS variables
are optional for Spec 1; leave them blank to use the local-file fallback.

### Run the agent (live)

The live voice path needs **real credentials** for LiveKit, Deepgram,
ElevenLabs, and the LLM provider, so it cannot be exercised offline. With `.env`
populated, run the worker in console mode (talk to it from your terminal):

```bash
python -m vcon_agent.agent console
```

or connect it to a LiveKit room in dev mode:

```bash
python -m vcon_agent.agent dev
```

### Run the tests

The vCon assembly, persistence, and conversation glue are all unit-tested with
fakes, so the suite runs with **no network and no credentials**:

```bash
. .venv/bin/activate
python3 -m pytest tests/ -q
```

### Acknowledged shortcuts (demo) vs. production

- 🩹 **Hardcoded consent script + boolean**, captured up front. → *Production:*
  versioned consent text bound to an authenticated identity, with the
  jurisdiction scope under which it was given.
- 🩹 **Single hardcoded patient/agent identity** seeded into the two parties. →
  *Production:* authenticated participant identities (a verified patient record
  and the agent's service account).
- 🩹 **One flush-to-S3 PUT on session end** (a single write of the whole vCon). →
  *Production:* incremental/streamed persistence with retries and idempotency,
  decoupled from the session lifecycle.

Out of scope for Spec 1: authentication/authorization, multi-party calls, and any
EHR integration.
