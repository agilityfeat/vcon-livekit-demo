# Production considerations

This is a proof of concept. Every deliberate shortcut is marked in the code and docs
with 🩹 and paired with its production counterpart: **🩹 Shortcut → Production**. This
page collects them in one place, grouped by area.

**Compliance.** The demo shows the mechanisms vCon provides for consent, integrity, and
redaction. It is not a HIPAA-certified deployment and must not be used with real PHI.

Out of scope for the demo entirely: authentication and authorization of real users,
multi-party calls, and any EHR integration.

## Consent

- 🩹 **Hardcoded consent script and boolean**, captured up front. → *Production:*
  versioned consent text bound to an authenticated identity, with the jurisdiction scope
  under which it was given.
- 🩹 **The consent boolean is not derived from the patient's spoken answer.** It is
  hardcoded to `True`, with no consent-denied or abort path and no binding of the
  patient's reply back to the attachment. → *Production:* parse the patient's actual
  response, record the real decision, and refuse to proceed (and avoid persisting
  clinical dialog) on denial.
- 🩹 **A single hardcoded patient and agent identity** seeded into the two parties
  (`Patient`, `Intake Agent`). → *Production:* authenticated participant identities (a
  verified patient record and the agent's service account).

## Triggering and persistence

- 🩹 **One flush-to-S3 PUT on session end**, a single write of the whole vCon, with a
  last-resort local-file salvage under `vcon-output/` if that PUT (or the guarded
  `StartExecution`) fails, so the one shot is fail-safe rather than fail-open. →
  *Production:* incremental or streamed persistence with retries and idempotency,
  decoupled from the session lifecycle.
- 🩹 **The agent triggers enrichment directly.** It calls Step Functions
  `StartExecution` from the agent process right after the single S3 PUT, guarded on
  `STATE_MACHINE_ARN`. → *Production:* event-driven persistence, described next.

### Planned direction: event-driven trigger

The acknowledged production pattern (a future "Spec 3") is for the agent's **only** AWS
touchpoint to be the single S3 PUT, with the state machine triggered by the object-created
event: S3 notification → EventBridge → Step Functions, or S3 → Lambda → `StartExecution`.
That:

- decouples the agent, which then needs no `states:StartExecution` permission and no
  state-machine ARN;
- better matches the "no AWS in the agent process" design note;
- removes the current failure mode where the PUT succeeds but `StartExecution` fails,
  so enrichment silently never fires.

### The `LoadSkeleton` state is a reserved seam for that trigger

The `LoadSkeleton` state and the dormant `skeleton_key` branch in its inline Lambda
(`load-skeleton-step`) are **one** reserved seam for the event-driven trigger:

- **Today it is a pass-through.** The agent inlines the skeleton by value (input
  `{"vcon": <doc>}`) and does **not** pass `skeleton_key`, so the Lambda just forwards
  the vCon from the envelope. The state is redundant under the current wiring.
- **It becomes load-bearing under the event-driven trigger,** where the pipeline would
  receive only an object **key**. The existing branch is the loader: when the event
  carries `skeleton_key` and the bucket is configured, it `get_object`s that key from S3,
  parses the JSON, and uses it if it is a dict with a `vcon` field, falling back to the
  envelope copy otherwise.
- **When building that trigger,** pass `skeleton_key` (and the recording key) instead of
  inlining the skeleton, and use this branch as the loader. Do **not** delete it as dead
  code.

This is a deliberate speculative-generality trade-off: a small, intentionally unused
branch is kept now so the later trigger change does not have to touch the state machine
shape.

## Call recording

- 🩹 **Plain SSE-S3, no customer-managed KMS key** for the recordings. → *Production:*
  SSE-KMS with a CMK, a bucket policy, and VPC endpoints. (JWE is still demonstrated on
  the vCon itself.)
- 🩹 **Poll-based readiness.** The pipeline polls S3 at the recording key with a simple
  retry loop. → *Production:* event-driven readiness with dedupe on `egressId`,
  reconciliation for missing events, and handling of early or truncated egress.
- 🩹 **Audio only, no video** egress. → *Production:* configurable, with video egress if
  the clinical workflow needs it.
- 🩹 **The recording location is recorded even if starting the egress failed**, and the
  poll step reports "ready" when there is no recording key at all, so the canned path can
  run. → *Production:* require a real recording key and fail closed if the object never
  appears within the SLA.

## Enrichment pipeline

- 🩹 **Deterministic idempotency key = `room_name + session start`**, hashed into the
  execution name (a truncated SHA-256). → *Production:* a stable, server-assigned
  call or session id (not a client-chosen room name), and a dedicated idempotency store
  or the full digest if volume demands it.
- 🩹 **The transcription wait has no stall guard.** The state machine polls the Transcribe
  Medical job on a fixed interval and waits as long as the real job takes, which is
  correct for long conversations. But a job that hangs, vanishes, or errors persistently
  would poll indefinitely, and a persistent `GetTranscription` error currently looks
  identical to "in progress". A genuinely `FAILED` job is already handled. → *Production:*
  stall or liveness detection (bounded non-progressing polls), retry-on-transient-error,
  and a `Catch` to a visible failure state, while a healthy long job keeps waiting.
- 🩹 **Optional canned-transcript stub.** If there is no real recording (or starting the
  job fails), `start-transcription-step` falls back to a canned transcript so the pipeline
  runs end to end; the fallback is logged and recorded as `transcription_canned_reason`.
  → *Production:* always run the real job, or use AWS HealthScribe for clinician-grade
  notes and transcript in one service.
- 🩹 **Speaker attribution rides the scripted "agent speaks first" invariant.** The
  structured transcript `turns` map Transcribe's `spk_N` labels to parties by speaking
  order, because the recording and live-turn clocks have different origins. A single
  speaker, a tie, or an unorderable start leaves `party` as `None`. → *Production:*
  channel separation (agent and patient on separate audio channels) or robust diarization
  with speaker identity.
- 🩹 **Coarse provenance.** The transcript entry references every text turn, and the
  summary likewise. The segment-level mapper `enrichment.map_turns_by_timestamp_overlap`
  is the production-direction logic and is unit-tested, but it is intentionally **not**
  wired into the pipeline. → *Production:* robust alignment between the diarized medical
  transcript and the live turns (word-level timing, speaker reconciliation), fed through
  that mapper.
- 🩹 **Single-turn entity attribution.** All Comprehend entities are pinned to the first
  patient text turn. → *Production:* per-entity character offsets mapped to the turn each
  entity actually came from.
- 🩹 **The summary is built from the flat authoritative transcript.** That is clinically
  right for terms like medication names, but the Bedrock prompt gets one undifferentiated
  string and loses the clean Patient/Agent attribution of the live turns (the structured
  `turns` exist but the prompt does not use them). → *Production:* summarize from the
  diarized transcript reconciled with the live turns, to get both speaker structure and
  clinical accuracy.
- 🩹 **Comprehend Medical is kept to meds and conditions.** → *Production:* the full
  entity set with RxNorm / ICD-10-CM linking and confidence thresholds.
- 🩹 **Analysis fails open with a `degraded` marker.** Comprehend or Bedrock failures
  return an empty result plus a `degraded` reason, keeping the demo runnable while the
  failure stays visible. → *Production:* a `Catch` to a visible alarm state plus retries,
  so operators are paged rather than reading a silently empty result.

## Sign, encrypt and redact

- 🩹 **Self-signed demo keypair** loaded from an env var (or generated), not a managed
  key, and the same key is used for the JWS and the JWE. → *Production:* AWS KMS / ACM
  PCA, key rotation, HSM-backed signing, a separate JWE key with least privilege, and
  never shipping keys with the app. (Ties to the AWS-plugin long-term-credential
  caveat, #1719.)
- 🩹 **Redaction is naive PII removal.** A regex pass (emails, phone numbers, SSN-like
  digit runs) plus a Comprehend Medical `DetectPHI` pass, applied deny-by-default over
  free text. → *Production:* vetted de-identification against the full PHI surface,
  reviewed for the applicable Safe Harbor or expert-determination standard. See
  [sign-encrypt-redact.md](sign-encrypt-redact.md#redaction-scope-and-limitations) for
  what is and is not stripped and why.
- 🩹 **The Seal step fails open.** A `DetectPHI` failure degrades to regex-only redaction
  and an S3 PUT failure is recorded in `seal_degraded`, instead of failing the run. →
  *Production:* a `Catch` to a visible alarm state, retries with backoff, and a hard
  failure path surfaced to operators.
- 🩹 **JWE encryption with a demo key, stored in the same bucket** as the other vCon
  artifacts. → *Production:* SSE-KMS, separate access scopes for full versus redacted,
  least privilege.

## Infrastructure

- 🩹 **Broad-ish IAM**: service-level actions, a single shared Lambda role, a single
  bucket. → *Production:* least privilege per action and resource, condition keys, and a
  separate role per Lambda. (The Bedrock policy uses `Resource: '*'` so cross-region
  inference profiles work; a least-privilege pass must not break that routing.)
- 🩹 **Inline Lambda code for seven of the eight steps**, restating the pure
  `enrichment.py` and `vcon.py` logic because inline code cannot import the repo package.
  The Seal step is the exception: it is packaged, so its crypto is not hand-mirrored. →
  *Production:* CI/CD, versioned artifacts in S3/ECR, a shared layer or package for all
  steps instead of copied logic, and separate infra and app pipelines.
- 🩹 **SSE-S3 default encryption**, no customer-managed KMS key. → *Production:* SSE-KMS
  with a CMK, a key policy, and separate access scopes for full versus redacted vCons.
- 🩹 **Bedrock model access is a one-time account-level enablement** (for Anthropic, a
  first-time-use agreement) done out of band, not provisioned by the template. →
  *Production:* provisioned through account setup or a landing zone.
- 🩹 **Single region, no data-residency controls.** → *Production:* region pinning, BAAs,
  and VPC endpoints for the AWS service calls.

## Demo application

- 🩹 **No authentication.** The patient, doctor, and analyst "roles" are just which page
  you open. The sign-in forms are presentational, and which report the record viewer shows
  is set by the `?view=` URL parameter, so anyone who can reach the backend can read
  either view. → *Production:* real authN/authZ (OIDC with per-role scopes), with the full
  (decrypted) view limited to authorized clinicians and audit-logged.
- 🩹 **Single-process local app.** One `uvicorn` process serves the API and the static
  frontend. → *Production:* a separately deployed, horizontally scalable API behind a
  gateway, with the frontend on a CDN or static host.
- 🩹 **The backend holds the demo key and does all crypto server-side.** It verifies the
  JWS and decrypts the JWE so the browser never sees the key. → *Production:* a managed
  key service with scoped, audited decryption, and the full record released only to
  authorized, authenticated clinicians.
- 🩹 **The live human doctor is stubbed.** The intake is patient and bot only; the doctor
  is an asynchronous viewer of the produced record, and the dashboard's "Schedule a call"
  form saves nothing. → *Production:* real live consultations and/or a clinician queue and
  scheduling service, with the record still produced for the audit trail.
- 🩹 **A demo key ships with the repo.** `frontend/fixtures/demo-insecure-do-not-use.jwk`
  is committed so the fixture pair stays consistent. It protects nothing real. →
  *Production:* never ship keys with the app.
- 🩹 **Records are discovered by scanning** the bucket for `vcon-signed.jws`, using a single
  `list_objects_v2` page with no pagination (or the single offline fixture). →
  *Production:* a queryable records index (for example DynamoDB) written by the pipeline,
  with pagination, filtering, and per-role access scoping.
- 🩹 **The doctor dashboard reuses the public records list.** There is no per-clinician
  roster. → *Production:* the authenticated clinician's own patients and records from a
  role-gated service.
- 🩹 **Offline mode ignores the run prefix** and returns the one committed sample for any
  request. → *Production:* key fixtures by prefix before adding a second one, so a wrong
  URL cannot silently resolve to the sample.
- 🩹 **The LiveKit browser client is CDN-loaded** (ESM) for a no-build demo, and the
  patient identity and room are chosen client-side with a broad publish/subscribe grant. →
  *Production:* a pinned, bundled client served from your own origin, and server-minted
  tokens for an authenticated patient, bound to a verified record, with a tightly scoped
  grant and short TTL.
- 🩹 **The audio presign is role-agnostic and un-gated.** `GET /api/records/{run}/audio`
  mints a short-lived presigned URL for the un-redacted recording with no authZ, so the
  frontend is responsible for requesting it only in the doctor view. The recording
  object name is a fixed `recording.ogg`. → *Production:* gate the presign behind per-role
  authZ, audit-log it, and derive the object key from the vCon.
- 🩹 **CORS is wide open** on the backend for local development. → *Production:*
  restrict origins to the known frontend origin(s).
