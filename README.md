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
- **Demo application** — a single, locally-runnable app (FastAPI backend +
  vanilla multi-page frontend) where a human patient places a real intake call
  to the bot from the browser, and doctor / analyst roles review the produced
  vCon records. See **[docs/demo-app.md](docs/demo-app.md)** for the
  end-to-end run story and the demo shortcuts.

## Infrastructure

The AWS backend is defined in one flat CloudFormation template
(`infrastructure/template.yaml`): an S3 bucket for recordings and vCon JSON, a
Step Functions state machine orchestrating the real enrichment pipeline
(Transcribe Medical, Comprehend Medical, and Bedrock), and the IAM roles
granting access to those services and S3.

For full deploy, configuration, and teardown instructions, including the
one-command deploy, mapping stack Outputs into a local `.env` (copied from
`.env.example`), the manual Bedrock model-access prerequisite, and local
validation, see **[infrastructure/README.md](infrastructure/README.md)**.

## Intake agent

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
  recorded up front, before any dialog turn is appended. Note the recorded
  boolean is **hardcoded to `True`** and is **not yet derived from the patient's
  actual answer**: the patient's yes/no reply arrives as an ordinary dialog turn
  and is not bound back to the consent attachment, and there is no
  consent-denied/abort path. Wiring the boolean to the real response (and
  refusing to proceed on denial) is a Production concern (see shortcuts below).
- **Dialog during the call:** on each `conversation_item_added` event, the
  committed message is translated into a dialog turn (`user` → party 0,
  `assistant` → party 1) and appended in order. That translation lives in the
  pure `handle_item(builder, role, text)` helper so it is unit-testable without a
  live session.

### Persist on session end

When the session ends, the agent flushes the finished vCon **exactly once**
(`src/vcon_agent/persistence.py`):

- If `BUCKET_NAME` is set, it PUTs the vCon JSON to the AWS enrichment backend's
  S3 bucket under the unique-key-per-run convention
  `<room_name>/<session_start_iso>/vcon-skeleton.json`, then (only if
  `STATE_MACHINE_ARN` is also set) triggers enrichment via a guarded Step
  Functions `StartExecution` with input `{"vcon": <doc>}`.
- If `BUCKET_NAME` is unset, it writes the same document to a local file under
  `vcon-output/` using the identical run-key layout and skips enrichment, so the
  agent is demoable standalone with no AWS backend.

Because the one-PUT model gives the call a single persistence opportunity, the
S3 path is **fail-safe**: if the S3 PUT (or the guarded `StartExecution`) raises,
the vCon is salvaged to a last-resort local file under `vcon-output/` (same
run-key layout) instead of being lost, and the flush is logged as a warning.

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
are optional; leave them blank to use the local-file fallback.

#### Development environment (Nix + direnv)

The recommended way to get a working dev environment is the committed Nix flake,
which pins a Python with reliable prebuilt wheels for the LiveKit/numpy native
stack and provides the native system libraries those wheels need (notably
`libstdc++`), so you avoid the manual virtualenv and system-library setup.

With [Nix](https://nixos.org/download) (flakes enabled) and
[direnv](https://direnv.net/) installed, run `direnv allow` once in the repo
root. From then on, `cd`-ing into the project drops you into a shell that:

- exports the native libraries onto `LD_LIBRARY_PATH` so the pip-installed
  wheels load their C-extensions,
- **creates** a `.venv` and installs the project (`pip install -e '.[dev]'`) on
  first entry if one doesn't exist, and **activates** the existing `.venv`
  otherwise.

So `python -m vcon_agent.agent console` and `pytest` work immediately. Without
direnv, run `nix develop` for the same shell. The first entry also generates
`flake.lock` if it isn't present yet.

The manual setup above remains the non-Nix fallback.

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

### Shortcuts (demo) vs. production

- 🩹 **Hardcoded consent script + boolean**, captured up front. → *Production:*
  versioned consent text bound to an authenticated identity, with the
  jurisdiction scope under which it was given.
- 🩹 **Single hardcoded patient/agent identity** seeded into the two parties. →
  *Production:* authenticated participant identities (a verified patient record
  and the agent's service account).
- 🩹 **Hardcoded consent boolean**, captured up front and **not derived from the
  patient's spoken answer** (no consent-denied/abort path, no binding of the
  reply back to the attachment). → *Production:* parse the patient's actual
  response, record the real decision, and refuse to proceed (and avoid
  persisting clinical dialog) on denial.
- 🩹 **One flush-to-S3 PUT on session end** (a single write of the whole vCon),
  with a last-resort local-file salvage if that PUT fails so the one shot is
  fail-safe rather than fail-open. → *Production:* incremental/streamed
  persistence with retries and idempotency, decoupled from the session
  lifecycle.
- 🩹 **Agent triggers enrichment directly** by calling Step Functions
  `StartExecution` from the agent process right after the single S3 PUT (guarded
  on `STATE_MACHINE_ARN`). → *Production:* event-driven persistence where the
  agent's only AWS touchpoint is the single S3 PUT and the enrichment state
  machine is triggered by the S3 object-created event (S3 notification →
  EventBridge → Step Functions, or S3 → Lambda → `StartExecution`). This
  decouples the agent (no `states:StartExecution` permission or state-machine
  ARN awareness), better matches the "no AWS in the agent process" design note,
  and removes the current failure mode where the PUT succeeds but the subsequent
  `StartExecution` fails, so enrichment silently never fires.

Out of scope: authentication/authorization, multi-party calls, and any
EHR integration.

## Call recording

The agent captures the **call audio** to S3 and surfaces its location on the vCon so
the enrichment pipeline can find and transcribe it. When the agent
joins a room (right after `ctx.connect()`), it starts a LiveKit
**audio-only egress** using the unified `StartEgress` API with a
`TemplateSource(audio_only=True)` source, which writes a single mixed OGG
recording for the whole room directly to the S3 bucket. (`StartEgress` with a
template source is the current replacement for the deprecated
`StartRoomCompositeEgress` call; the audio-only template keeps the one-mixed-file
output model.)

### Where the recording lands

The egress writes to the **same unique run prefix** the vCon flush uses, so the
recording and the skeleton vCon sit side by side:

```
<room_name>/<session_start_iso>/recording.ogg
<room_name>/<session_start_iso>/vcon-skeleton.json
```

The per-run key has a **single time segment**: `session_start_iso` is a
microsecond-precision UTC instant captured once at entry and shared between the
recording key and the vCon flush, so `recording_key(...)` and `run_key(...)`
always resolve to the same run prefix. This layout matches the S3
unique-key-per-run convention documented in
**[infrastructure/README.md](infrastructure/README.md)**; the unique key plus
the bucket's **versioning** together neutralize the "a later egress overwrites
the earlier file" failure mode.

LiveKit Egress also writes a sibling `EG_<id>.json` **manifest** object next to
`recording.ogg`. This is **intentionally retained** for provenance: it carries
the egress id, the started_at/ended_at timings, and the resolved object
location. The enrichment pipeline keys off `recording.ogg` and can ignore the
manifest; set `disable_manifest=True` on the egress `FileOutput` if you prefer a
bucket that holds only `recording.ogg`.

### Recording location on the vCon

The recording S3 location is recorded on the vCon as a **recording dialog
object** (`type: "recording"`) via `VConBuilder.add_recording_dialog(url, ...)`
in the framework-independent `src/vcon_agent/vcon.py`. The audio is referenced
**externally**: the dialog carries the `s3://<bucket>/<key>` location as a `url`
field and **never inlines the audio bytes**. Unlike patient/agent dialog turns,
the recording object represents the whole-call audio, so it is not tied to a
party index. The agent threads this url onto the vCon **before** the session-end
flush, so the persisted `vcon-skeleton.json` already carries the recording
location.

### Readiness is decoupled from egress webhooks

Rather than relying on the LiveKit `egress_ended` webhook to drive downstream
processing, readiness is **poll-based**: the enrichment pipeline polls S3 for the object at the
known recording key. This sidesteps the duplicate / early / missing-webhook
failure modes. The agent may subscribe to `egress_ended` for
logging/observability only; it is explicitly **not on the critical path** and
triggers no processing.

### Env-optional

Live egress needs `BUCKET_NAME` + `AWS_REGION` **and** the LiveKit credentials
(`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`). When `BUCKET_NAME` or
the LiveKit creds are absent, the agent **skips egress gracefully** (logged, the
call is never broken) so the standalone local demo still runs, mirroring the
env-optional behavior of the persistence layer. `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` are used for the S3 upload credentials if present and
otherwise fall back to the instance role.

Like the live voice path, the **live egress path is not part of automated
verification**: it needs real LiveKit and AWS credentials and cannot be
exercised offline. The egress call sits behind an injectable/mockable seam, so
the recording-key helper, the recording dialog object, and the egress-request
builder are all unit-tested offline with fakes (no live LiveKit/AWS).

### Shortcuts (demo) vs. production

- 🩹 **Plain SSE-S3, no customer-managed KMS key** for the recordings. →
  *Production:* SSE-KMS with a CMK, bucket policy, and VPC endpoints. (JWE is
  still demonstrated on the vCon itself.)
- 🩹 **Poll-based readiness** (the enrichment pipeline polls S3 at the recording key with a simple
  timeout/retry). → *Production:* event-driven readiness with proper dedupe on
  `egressId`, reconciliation for missing events, and handling of early/truncated
  egress.
- 🩹 **Audio only, no video** egress. → *Production:* configurable, with video
  egress if the clinical workflow needs it.

## Enrichment pipeline

The enrichment pipeline runs the **AI enrichment** after the call and writes the results into the
vCon `analysis[]` with **turn-level provenance**. The backend is a Step Functions
state machine (`infrastructure/template.yaml`) whose steps mirror the pure,
offline-tested logic in `src/vcon_agent/enrichment.py` and
`src/vcon_agent/vcon.py`.

### Deterministic, idempotent trigger

The local agent starts the pipeline itself: on session end it PUTs the skeleton
vCon to S3 and calls Step Functions `StartExecution` directly (boto3, local AWS
creds), passing input `{"vcon": <doc>}` and a **deterministic execution name**
derived from the idempotency key `room_name + session start`
(`vcon.execution_name`). Because the name is deterministic and Step Functions
rejects a duplicate name, a retried start is a **no-op** rather than a second
run. There is **no EventBridge or webhook** on the critical path.

Step Functions is chosen over a single Lambda because Transcribe Medical batch is
asynchronous and can exceed Lambda's 15-minute timeout. The state machine owns
the waiting (a `Wait` state plus a `GetMedicalTranscriptionJob` poll plus a
`Choice` loop) and the retry/catch, so no Lambda ever sleeps past its timeout.

### What the pipeline does

1. **Poll S3 for the recording** before transcribing, so the pipeline never runs
   on a missing or partial file. A `Choice` + `Wait` loop retries until the
   object is present.
2. **Transcribe Medical (batch, authoritative).** The recording is transcribed
   and the result is stored as an `analysis[]` **transcript** object marked
   `authoritative`, referencing the dialog turns it covers. It is kept
   **alongside** the live Deepgram transcript already in `dialog[]`.
3. **Comprehend Medical (meds + conditions).** Clinical entities are extracted
   and stored as an `analysis[]` **entities** object referencing the dialog turn
   index they came from.
4. **Bedrock (physician summary).** A concise physician-facing summary is stored
   as an `analysis[]` **summary** object referencing the dialog turns it was
   built from.

Comprehend and Bedrock run as a **parallel** branch; a final merge step writes
the three analysis entries in a deterministic order: `transcript`, `entities`,
`summary`.

Transcription fails **closed** (a dedicated `Fail` state), but Comprehend and
Bedrock fail **open**: if a model call is denied, throttled, or the Bedrock
model is not enabled, the step returns an empty result and attaches a `degraded`
reason string to its `analysis[]` entry instead of failing the run. The marker
is present only on a failure, so an empty entities list or summary with a
`degraded` marker is distinguishable from a genuinely empty result.

### Dual transcript: a feature, not a bug

This is where "why not just the live transcript?" gets answered. The real-time
agent STT (Deepgram) is tuned for **latency**; Transcribe Medical is tuned for
**clinical accuracy**. Rather than discard one, vCon holds **both** with
provenance: the live transcript stays in `dialog[]`, and the clinical transcript
is an `analysis[]` entry marked `authoritative`. The two producers serve
different needs, and vCon models both natively over the same dialog, so nothing
has to reconcile a Deepgram record and an AWS record by hand.

### Offline verification

The pipeline needs live AWS to run, so it is verified **offline** the same way
the rest of the repo is: the correctness-critical shaping and provenance logic
lives in pure, importable helpers (`src/vcon_agent/enrichment.py` and the
`add_*_analysis` helpers in `vcon.py`) unit-tested with canned AWS-shaped inputs,
and the template is checked with `cfn-lint` plus `python3 -m py_compile` on each
inline Lambda body. The live end-to-end check is
`infrastructure/scripts/validate-live.sh` (requires real AWS credentials).

### Shortcuts (demo) vs. production

- 🩹 **Deterministic idempotency key = `room_name + session start`**, hashed into
  the execution name. → *Production:* a stable server-assigned call/session id
  (not a client-chosen room name), and a dedicated idempotency store if volume
  demands it.
- 🩹 **The transcription wait has no stall guard.** The state machine polls the
  Transcribe Medical job on a fixed interval and waits as long as the real job
  takes -- correct for long conversations -- but a job that hangs, vanishes, or
  errors persistently would poll indefinitely (a persistent `GetTranscription`
  error currently looks identical to "in progress"). A genuinely `FAILED` job is
  already handled. → *Production:* stall/liveness detection (bounded
  non-progressing polls) plus retry-on-transient-error and a `Catch` to a visible
  failure state, while a healthy long job keeps waiting.
- 🩹 **Optional canned-transcript stub** so the pipeline is runnable without a
  real recording. → *Production:* the real job, or AWS HealthScribe for
  clinician-grade notes + transcript in one service.
- 🩹 **Provenance = simple turn-index references** mapped by timestamp overlap.
  The segment-level mapper (`enrichment.map_turns_by_timestamp_overlap`) is the
  production-direction logic and is unit-tested, but it is intentionally NOT
  wired into the demo pipeline yet (the inline steps use coarser attribution
  because the canned Transcribe path emits no per-segment timings). →
  *Production:* robust alignment between the diarized medical transcript and the
  live turns, fed through that mapper.
- 🩹 **Single-turn entity attribution** (all entities pinned to the first
  patient text turn). → *Production:* per-entity offsets mapped to the turn each
  entity came from.
- 🩹 **Comprehend Medical kept to meds + conditions.** → *Production:* the full
  entity set with RxNorm / ICD-10-CM linking.
- 🩹 **Analysis fails open with a `degraded` marker** (Comprehend / Bedrock
  failures return an empty result plus a `degraded` reason, keeping the demo
  runnable while the failure stays visible). → *Production:* a `Catch` to a
  visible alarm state plus retries, so operators are paged rather than reading a
  silently empty result.

## Sign, encrypt & redact

The seal step turns the **enriched** vCon into the signed / sealed record
and produces a **redacted sibling** linked back to the full version through the
vCon `redacted` chain. The correctness-critical logic lives in the pure,
offline-tested core `infrastructure/functions/seal/sealing.py`. Unlike the other
pipeline steps (which are inline Lambdas that mirror their pure core by hand),
the Seal Lambda **imports the sealing core packaged alongside its handler**: it
is a standalone, container-built function (packaged with AWS SAM so it can ship
the `jwcrypto` dependency) wired after the pipeline's merge step. The pytest
suite imports the same file.

### What the seal step does

1. **Sign the completed vCon (JWS).** The enriched vCon is serialized to
   canonical JSON and signed with a demo RSA keypair (`PS256`), producing a JWS.
   The signed document is the **authoritative, integrity-protected** record;
   `verify_vcon` recovers the exact payload and fails loudly on a tampered
   payload or wrong key.
2. **Encrypt the full copy (JWE).** The full vCon (or its JWS) is encrypted with
   `RSA-OAEP-256` key wrap + `A256GCM` content encryption, so the complete,
   un-redacted record is stored **encrypted at rest** and only a holder of the
   private key can read it.
3. **Produce the redacted sibling.** A copy of the vCon is created with PII
   stripped from the `dialog` turn bodies and the `analysis` bodies (free-text
   transcripts / summaries and the `entities` entity `text`), **keeping the
   document structure intact** — the same top-level keys, the same
   `parties` / `dialog` / `analysis` / `attachments` shape and ordering, and the
   same party indices. Redacted spans are replaced with a stable `[REDACTED]`
   placeholder rather than dropped, so a consumer can see where redaction
   happened.
4. **Link via the vCon `redacted` pointer.** The redacted copy is itself a
   **signed vCon (JWS)**, and it carries a top-level `redacted` reference back to
   the **full encrypted copy** (`vcon-full.jwe`). The pointer carries the full
   vCon `uuid`, a content `type` (`application/vcon+jws`), the `url` of the
   encrypted full copy, and a `content_hash` of that copy, matching the repo's
   by-`uuid` / by-index convention (never embedded identities). Only the redacted
   copy carries this pointer; the full copy never does. See
   [Redaction scope & limitations](#redaction-scope--limitations) for the
   signed-sibling and `content_hash` conformance detail.

### Why this runs in the pipeline, not the agent

Signing, encryption, and redaction all happen in the **enrichment pipeline**
after the merge step, never in the agent process. This keeps the team's "no AWS
in the agent process" design note intact: the agent's only AWS touchpoint stays
the single S3 PUT of the skeleton vCon. The redaction step's AWS Comprehend
Medical `DetectPHI` pass is likewise confined to the packaged Seal Lambda's glue;
the pure core in `sealing.py` never calls AWS and instead accepts an injected
`detect_phi`
callable (or pre-computed `phi_spans`), so it stays fully offline-testable with
canned spans.

### Library choice: jwcrypto

The seal uses [`jwcrypto`](https://jwcrypto.readthedocs.io/), a pure-Python
JWS / JWE / JWK library on top of `cryptography`. The vCon spec models a signed
vCon as a JWS and an encrypted vCon as a JWE over the JSON document, and the vCon
reference Python library (`python-vcon`) itself uses `jwcrypto` for its
signing / encryption. Taking `python-vcon` as a direct dependency would pull a
large, heavier tree that is less reliably installable across
`requires-python >=3.9`; `jwcrypto` gives the exact JOSE primitives needed to
implement the vCon JWS / JWE / redacted structure directly while keeping the
dependency surface small and offline-testable. It is declared as a main
dependency because the pure core imports it at runtime.

### Offline verification

The seal is verified the same way as the rest of the repo: the sign/verify,
encrypt/decrypt, and redaction logic are pure functions in `sealing.py`,
unit-tested in `tests/test_sealing.py` with a demo keypair generated in-memory,
canned PHI spans, and no live AWS or network. The crypto-dependent tests are
guarded with `pytest.importorskip('jwcrypto')` so they SKIP cleanly (rather than
failing collection) if the JOSE library could not be installed.

### Shortcuts (demo) vs. production

- 🩹 **Self-signed demo keypair** generated / loaded locally (env / local), not a
  managed key. → *Production:* AWS KMS / ACM PCA, key rotation, HSM-backed
  signing; never ship keys with the app. (Ties to the AWS-plugin
  long-term-credential caveat — #1719.)
- 🩹 **Redaction = naive PII removal** (regex over known fields such as emails,
  phone numbers, and SSN-like digit runs, plus an optional Comprehend Medical
  `DetectPHI` pass over known fields). → *Production:* vetted de-identification
  against the full PHI surface, reviewed for the applicable safe-harbor /
  expert-determination standard. See
  [Redaction scope & limitations](#redaction-scope--limitations) below for what
  is and is not stripped and why.
- 🩹 **JWE encryption with a demo key, stored in the same bucket** as the rest of
  the vCon artifacts. → *Production:* SSE-KMS, separate access scopes for full
  vs. redacted, least privilege.

### Redaction scope & limitations

The redacted sibling strips **HIPAA identifiers**, not the clinical narrative.
Two layers do the stripping:

- A **naive local regex** pass over known fields catches emails, SSN-like digit
  runs, and phone numbers.
- An optional **AWS Comprehend Medical `DetectPHI`** pass over the same fields
  detects the nine HIPAA IDENTIFIER entity types it supports: `AGE`, `DATE`,
  `NAME`, `PHONE_OR_FAX`, `EMAIL`, `ID`, `URL`, `ADDRESS`, and `PROFESSION` (see
  the [DetectPHI documentation](https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-phi.html)).

What the redacted copy does **not** strip is the **clinical narrative** itself:
medical conditions and medications stay in cleartext. That is deliberate. Those
entities come from a **different** Comprehend Medical API, `DetectEntitiesV2`
(categories `MEDICAL_CONDITION` and `MEDICATION`; see the
[DetectEntitiesV2 documentation](https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-entitiesv2.html)),
and they are not "PHI" in Comprehend's model. So `DetectPHI` leaving the clinical
narrative in place is the service **working as designed**, not a defect. The vCon
container draft also leaves the redaction method out of scope:
[draft-ietf-vcon-vcon-container](https://datatracker.ietf.org/doc/html/draft-ietf-vcon-vcon-container)
section 4.1.6 states that "The method of redaction is out of scope of this
document".

**Production direction.** Vetted de-identification is multi-layered rather than a
single `DetectPHI` call. The AWS
[prescriptive guidance for Comprehend Medical](https://docs.aws.amazon.com/prescriptive-guidance/latest/generative-ai-nlp-healthcare/comprehend-medical-rag.html)
combines Comprehend Medical PHI and entity detection with human review and/or
LLM guardrails. A production pipeline would add confidence-threshold tuning and
review against the HIPAA **Safe Harbor** 18-identifier standard or **expert
determination**. AWS gives its own caveat on the `DetectPHI` page: the detected
entities "don't map 1:1 to the list specified by the Safe Harbor method", and
AWS recommends that you "use additional human review or other methods to confirm
the accuracy of detected PHI". The demo keeps redaction naive on purpose and
defers the vetted de-identification surface to that production direction.

**vCon conformance of the redacted sibling.** Two behaviors align the redacted
sibling with the container draft's section 4.1.6:

- The redacted sibling is a **signed vCon (JWS)**, signed with the same demo RSA
  key (`PS256`) used for the full signed copy. This follows the draft's "the
  redactor SHOULD sign the redacted version of the vCon".
- The top-level `redacted` pointer carries a **`content_hash`** of the referenced
  encrypted full copy (`vcon-full.jwe`), alongside its `uuid`, `type`, and `url`.
  This follows the draft's "url SHOULD be included unless redacted and
  content_hash MUST be included". The hash format is `sha512-<base64url>`: the
  literal prefix `sha512-` followed by the unpadded base64url encoding of the
  SHA-512 digest of the exact stored JWE bytes, matching the draft's Externally
  Referenced Files examples.

## End-to-end demo application

Everything above is tied together by a single, locally-runnable **demo
application** that shows the whole vCon value chain from one app that looks like
a real product. A **human patient** places a **real** intake call to the bot
from the browser, and **doctor** and **analyst** roles asynchronously review the
vCon records the pipeline produces. The backend does all the crypto server-side
(**verifies the JWS and decrypts the JWE** with the demo signing key) and serves
readable vCon JSON, so the browser never touches the key.

See **[docs/demo-app.md](docs/demo-app.md)** for the run story (live + offline),
the frontend↔backend layout, fixture regeneration, and the acknowledged
shortcuts.

## Compliance

this PoC demonstrates the mechanisms vCon provides for consent, integrity, and redaction. It is not a HIPAA-certified deployment and must not be used with real PHI.
