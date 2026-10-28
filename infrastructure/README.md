# Cloud infrastructure (CloudFormation)

A single, flat CloudFormation template (`infrastructure/template.yaml`) that
provisions the minimal AWS backend the vCon + LiveKit demo consumes. It deploys
with one command, exposes the identifiers the local app needs as stack
**Outputs**, and tears down cleanly.

Most Lambda code is inlined, IAM is kept broad for readability, and the single
exception is the **Seal** step, which is a packaged (container-built)
`AWS::Serverless::Function` so it can ship its native `jwcrypto` / `cryptography`
dependency and import the authoritative sealing core directly. The template now
carries the SAM transform (`Transform: AWS::Serverless-2016-10-31`) for that one
function, so it is deployed with AWS SAM (`sam build` + `sam deploy`) rather than
a bare `aws cloudformation deploy`. The shortcuts and their production
counterparts are listed at the end.

**Why Step Functions over a single Lambda.** Transcribe Medical batch is an
asynchronous job that can run longer than Lambda's hard 15-minute timeout. If a
single Lambda tried to start the job and then sleep until it finished, it would
time out. Instead the state machine OWNS the waiting: a `Wait` state plus a
`GetMedicalTranscriptionJob` poll plus a `Choice` loop keep checking until the
job reaches `COMPLETED` or `FAILED`, and the retry/catch lives in the state
machine too. Each Lambda does one short unit of work and returns, so no Lambda
ever sleeps past its timeout.

> All commands below are written to be run from the **repository root** (the
> parent of this `infrastructure/` folder), which is why paths are prefixed
> with `infrastructure/`.

## What the template creates

| Resource | Purpose |
|---|---|
| **S3 bucket** (`AWS::S3::Bucket`) | Recordings from LiveKit Egress + skeleton / enriched / redacted vCon JSON. Versioning on, SSE-S3 default encryption, all public access blocked. |
| **Inline Lambda functions** (`AWS::Lambda::Function`) | The seven boto3-only pipeline steps invoked by the state machine, inline Python: `poll-recording-step` (poll S3 for the recording), `load-skeleton-step` (load the skeleton vCon), `start-transcription-step` (start the Transcribe Medical batch job), `get-transcription-step` (poll the job once), `comprehend-step` (Comprehend Medical meds + conditions), `bedrock-step` (physician summary), and `merge-step` (merge results into `analysis[]` with turn-index provenance). Each body mirrors the pure, offline-tested logic in `src/vcon_agent/enrichment.py` + `vcon.py` (inline code cannot import the repo package at AWS runtime). These seven import only `boto3`, which the Lambda runtime provides, so they need no build step. |
| **Packaged Seal Lambda** (`AWS::Serverless::Function`) | The `seal-step`, wired after `merge-step`: it signs the enriched vCon (JWS), encrypts the full copy (JWE), and writes a redacted sibling, persisting the three objects to S3. Unlike the seven inline steps, this one is **not** inlined: it is a container-built SAM function (`CodeUri: functions/seal/`, `Handler: seal_handler.handler`, `Runtime: python3.12`, `Metadata.BuildMethod: python3.12`) that **imports the sealing core packaged alongside it (`functions/seal/sealing.py`)** directly instead of re-mirroring the crypto by hand. It depends on `jwcrypto` -> `cryptography`, which ships compiled native binaries, so it requires `sam build --use-container` (see Deploy below). |
| **Step Functions state machine** (`AWS::StepFunctions::StateMachine`) | The enrichment orchestrator. Polls S3 for the recording, runs Transcribe Medical as an async batch whose completion the state machine awaits, then runs Comprehend Medical and Bedrock as a parallel branch, then merges into `analysis[]`. Invoked by the local app via a deterministic, idempotent `StartExecution`. |
| **IAM roles** (`AWS::IAM::Role`) | A Step Functions execution role (invokes all seven pipeline Lambdas + writes execution logs) and a Lambda execution role granting access to Transcribe Medical, Comprehend Medical, Bedrock, S3, and CloudWatch Logs. |

### Explicitly NOT created (and why)

- **Transcribe Medical, Comprehend Medical, Bedrock** — these are managed
  services you *call*, not resources you create. The template only grants IAM
  permission to use them.
- **Bedrock model access** -- a one-time, account-level enablement (for
  Anthropic, a first-time-use agreement), not a CloudFormation resource. See the
  manual prerequisite below.
- **No EventBridge / webhook endpoint** — enrichment is triggered by the local
  app via `StartExecution`, so no inbound event plumbing is needed.
- **No compute for the agent** (no ECS / EC2 / Fargate) — the agent runs
  locally.

## Manual prerequisite: Bedrock model access (Anthropic first-time use)

Bedrock model access is a one-time, account-level enablement, not a
CloudFormation resource, so you must set it up once before the `bedrock-step`
can call Bedrock. The old console **Model access** page is no longer the flow
in commercial regions:

- In **commercial regions** there is no longer a **Model access** console page
  to toggle. Access to most foundation models is enabled automatically on the
  first invocation (an auto-subscription happens in the background). The
  **Model access** page under Bedrock configurations now applies to **AWS
  GovCloud (US)** only.
- **Anthropic** models (the default here) additionally require a **one-time
  First-Time-Use (FTU) use-case form** per account/organization, submitted
  BEFORE the first invoke. You can submit it either in the console or via the
  CLI/SDK:
  - **Console:** open the **Model catalog**, pick an Anthropic model and open it
    in the playground, or go to the **Anthropic** providers page, then complete
    the use-case form. The model may appear greyed out with no visible "accept
    agreement" control until the FTU form is on file.
  - **CLI/SDK:** `aws bedrock put-use-case-for-model-access` with the
    base64-encoded use-case form JSON.

If the console UI is unavailable or the model stays greyed out, the reliable
end-to-end CLI fallback is:

```bash
# 1. Get the offer token for the model's agreement.
aws bedrock list-foundation-model-agreement-offers \
  --region <your-region> \
  --model-id anthropic.claude-haiku-5-5 \
  --offer-type ALL

# 2. Create the agreement using the offerToken from step 1.
aws bedrock create-foundation-model-agreement \
  --region <your-region> \
  --model-id anthropic.claude-haiku-5-5 \
  --offer-token <token>

# 3. Verify access. Check agreementAvailability.status: AVAILABLE means ready,
#    PENDING means it is still being set up (give it a few minutes), and
#    NOT_AVAILABLE means no agreement is on file yet.
aws bedrock get-foundation-model-availability \
  --region <your-region> \
  --model-id anthropic.claude-haiku-5-5
```

After you submit the agreement it may sit in `PENDING` for a few minutes before
it becomes `AVAILABLE`; an email is sent when it is ready.

**Permissions for the one-time enablement.** The identity that triggers the
first subscription needs AWS Marketplace permissions
(`aws-marketplace:Subscribe`, `aws-marketplace:Unsubscribe`,
`aws-marketplace:ViewSubscriptions`) plus a valid payment method on the account.
These are needed ONLY for the one-time enablement, not for ordinary invokes.
Once the model is enabled for the account, the pipeline's Lambda invokes with
only `bedrock:InvokeModel`, which the template already grants.

Claude Haiku 5.5 is a cross-region-inference model: calling the bare base id
`anthropic.claude-haiku-5-5` on the bedrock-runtime `InvokeModel` endpoint fails
with HTTP 400, so it must be invoked via an **inference-profile id** (the base id
with a region prefix such as `global.`, `us.`, `eu.`, `au.`, or `jp.`). The
model id is the `BedrockModelId` CloudFormation parameter, which defaults to the
global cross-region inference profile `global.anthropic.claude-haiku-5-5`. You
can override it at deploy time (see the parameter override example below) to pin
a different model or a geo profile (for example `us.anthropic.claude-haiku-5-5`)
without editing the template.

The pipeline still runs end to end without model access (the `bedrock-step`
falls back to an empty summary if the model is unavailable), but a real
`bedrock:InvokeModel` call only returns a summary once the model is enabled.

**Fail-open with a degraded marker.** Both the `comprehend-step` and the
`bedrock-step` fail OPEN: if the model call raises (access denied, throttled, or
the Bedrock model is not enabled) the step returns an empty result rather than
failing the whole execution, so the demo still reaches `Merge` and `SUCCEEDED`.
To keep that legible, the branch attaches a `degraded` reason string to its
`analysis[]` entry (`entities` or `summary`). The key is present ONLY on a
failure, so a consumer can tell a genuinely empty result (no meds mentioned, no
summary) from a silent model failure. On a healthy run the key is absent, and
`validate-live.sh` asserts that. Transcription, by contrast, fails CLOSED through
the dedicated `TranscriptionFailed` state.

## Deploy (AWS SAM)

Because the Seal step is now a packaged `AWS::Serverless::Function` and the
template carries `Transform: AWS::Serverless-2016-10-31`, the stack is deployed
with **AWS SAM** (`sam build` + `sam deploy`) instead of a bare
`aws cloudformation deploy`.

### Prerequisites

- **AWS SAM CLI** -- installs the `sam` command used below. See the
  [AWS SAM CLI install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
- **Docker** -- must be installed and running, because the Seal function is
  built inside a Lambda build container (see "Why `--use-container`" below).

### Why `--use-container` is required

The Seal function depends on `jwcrypto`, which depends on `cryptography`, and
`cryptography` ships **compiled native binaries** (a Rust/CFFI extension linked
against OpenSSL). Those binaries must be built for the **Lambda execution
environment** that the `SealFunction` runs in: Amazon Linux 2023, CPython 3.12,
on `x86_64`. If you let SAM package your local-OS site-packages instead, the
wheel compiled for your machine (for example macOS/arm64 or a different glibc)
would **fail to import at Lambda runtime**. Passing `--use-container` makes
`sam build` compile the correct native wheel inside the matching Lambda build
image, so it imports cleanly once deployed.

The other seven pipeline steps are inline Lambdas that import only `boto3`
(provided by the runtime), so they need no build and are unaffected by this.

### Build, then deploy

```bash
# From the repository root, build the Seal function in the Lambda build container.
sam build --use-container --template infrastructure/template.yaml

# First deploy: --guided walks you through stack name, region, capabilities, and
# parameter overrides, then saves them to samconfig.toml for next time.
sam deploy --guided
```

On subsequent deploys the saved config lets you just run `sam deploy`, or you can
pass everything explicitly:

```bash
sam deploy \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM
```

Optional parameter overrides (same parameters as before):

```bash
sam deploy \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName=vcon-livekit-demo Environment=dev BedrockModelId=global.anthropic.claude-haiku-5-5
```

`BedrockModelId` must be a Bedrock inference-profile id (default
`global.anthropic.claude-haiku-5-5`); override it to pin a different model or a
geo profile such as `us.anthropic.claude-haiku-5-5`.

### Sealing core

`infrastructure/functions/seal/sealing.py` is the **single authoritative** pure
crypto/redaction core. It lives inside the packaged Seal Lambda, so `sam build`
ships it directly in the function artifact. Both the packaged `seal_handler` and
the offline pytest suite import this same file, so there is no separate source
copy and no vendoring/regeneration step.

## Read the Outputs into your local `.env`

The stack exports three Outputs: `BucketName`, `StateMachineArn`, and `Region`.
View them with:

```bash
aws cloudformation describe-stacks \
  --stack-name vcon-livekit-demo \
  --query "Stacks[0].Outputs" \
  --output table
```

Copy `.env.example` (at the repository root) to `.env` and fill in the values:

| Stack Output | `.env` variable |
|---|---|
| `BucketName` | `BUCKET_NAME` |
| `StateMachineArn` | `STATE_MACHINE_ARN` |
| `Region` | `AWS_REGION` |

```bash
cp .env.example .env
# then paste the Output values into .env
```

## S3 unique-key-per-run convention

To avoid collisions and accidental overwrites, every run writes objects under a
**unique key prefix** built from the room name and the session start (a
microsecond-precision UTC instant):

```
<room_name>/<session_start_iso>/recording.ogg
<room_name>/<session_start_iso>/vcon-skeleton.json
<room_name>/<session_start_iso>/vcon-enriched.json
<room_name>/<session_start_iso>/vcon-redacted.jws
```

`session_start_iso` is a microsecond-precision UTC instant captured once per
run, so it uniquely identifies the run as a single key segment. Bucket
**versioning** is enabled as cheap insurance in case a key is ever reused.

## Run the pipeline (`StartExecution`)

In the demo the **local agent** starts the pipeline directly on session end: it
PUTs the skeleton vCon to S3 and calls Step Functions `StartExecution` with input
`{"vcon": <doc>}` and a **deterministic execution name** derived from the
idempotency key `room_name + session start` (see
`src/vcon_agent/vcon.py::execution_name`). Because the name is deterministic and
Step Functions rejects a duplicate name, a retried start is a **no-op** rather
than a second pipeline run. There is no EventBridge or webhook on the critical
path.

### The states

```
WaitForRecording   poll S3 (HeadObject) for the recording object
  -> IsRecordingReady (Choice): not ready -> Wait 5s -> WaitForRecording
LoadSkeleton       load the skeleton vCon for this run (pass-through today)
StartTranscription start the Transcribe Medical BATCH job (async), return job name
  -> WaitForTranscription (Wait 5s)
  -> GetTranscription (poll the job once)
  -> IsTranscriptionComplete (Choice):
       COMPLETED -> Analyze
       FAILED    -> TranscriptionFailed (Fail)
       otherwise -> WaitForTranscription   (the state machine owns the wait)
Analyze            Parallel: Comprehend Medical (meds + conditions) | Bedrock summary
Merge              write analysis[] entries with dialog turn-index provenance
Done               Succeed
```

`LoadSkeleton` is a **pass-through** under the current by-value wiring: the agent
inlines the skeleton into the `StartExecution` input, so the doc is already on
the envelope and the step just forwards it. It is kept as a reserved seam for the
planned **event-driven trigger**, where the pipeline would receive only an object
key and `LoadSkeleton` would load the skeleton from S3 by key instead.

The clinical **Transcribe Medical** transcript is added to `analysis[]` marked
`authoritative` and sits **alongside** the live Deepgram transcript already in
`dialog[]`. This dual-transcript shape is deliberate: the real-time agent STT is
tuned for latency, Transcribe Medical for clinical accuracy, and vCon holds both
with provenance rather than forcing you to reconcile two vendor records by hand.

### Start it by hand

Grab the state machine ARN from the Outputs (or your `.env`), then start an
execution with a canned skeleton vCon as input. With no real recording in the
input, the poll step reports ready immediately and `start-transcription-step`
takes the **canned-transcript stub** path, so the execution still reaches `Merge`
and writes the real analysis types.

```bash
aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input '{
    "vcon": {
      "vcon": "0.0.1",
      "uuid": "00000000-0000-0000-0000-000000000000",
      "subject": "demo call",
      "parties": [{"name": "Agent"}, {"name": "Caller"}],
      "dialog": [],
      "analysis": []
    }
  }'
```

Check the result (the final output carries `"vcon": "0.0.1"` and `analysis[]`
with types `transcript`, `entities`, `summary` in that order):

```bash
aws stepfunctions describe-execution --execution-arn "<execution-arn-from-above>"
```

## Teardown (no orphaned resources)

```bash
aws cloudformation delete-stack --stack-name vcon-livekit-demo
aws cloudformation wait stack-delete-complete --stack-name vcon-livekit-demo
```

The bucket and its log group use `DeletionPolicy: Delete`, so `delete-stack`
removes everything the template created.

> **Note:** S3 refuses to delete a non-empty bucket. If you have uploaded
> objects, empty the bucket first (`aws s3 rm s3://$BUCKET_NAME --recursive`,
> and delete object versions if versioning accumulated any) before
> `delete-stack`.

## Local validation

`cfn-lint` runs fully offline (no AWS credentials required) and is the
authoritative local check for this template:

```bash
pip3 install --user cfn-lint              # or install into a virtualenv
cfn-lint infrastructure/template.yaml     # expect zero errors
# convenience wrapper:
./infrastructure/scripts/validate.sh
```

### Validate the deployed stack end to end (live AWS)

Once the stack is deployed, `validate-live.sh` checks the **real** infrastructure
end to end. Unlike `cfn-lint` (offline), this script calls the AWS API, so it
**requires live AWS credentials**. Run it with your own environment:

```bash
AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh
```

It reads the stack name from the optional `STACK_NAME` environment variable
(default `vcon-livekit-demo`) and takes the region from your AWS
config/profile (and the stack's `Region` output):

```bash
STACK_NAME=my-stack AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh
```

Each check prints a labelled `PASS`/`FAIL` line; the script reports a final
summary and exits non-zero if any check failed. It verifies:

- the stack status is `CREATE_COMPLETE` or `UPDATE_COMPLETE`;
- the S3 bucket has versioning `Enabled`;
- the S3 default encryption algorithm is `AES256` (SSE-S3);
- the bucket's public access block has all four flags `true`;
- the eight pipeline Lambda functions exist: the seven inline steps
  (`*-poll-recording-step`, `*-load-skeleton-step`, `*-start-transcription-step`,
  `*-get-transcription-step`, `*-comprehend-step`, `*-bedrock-step`,
  `*-merge-step`) plus the packaged `*-seal-step`;
- the Step Functions state machine status is `ACTIVE`;
- an **end-to-end execution**: it starts the state machine with the canned
  skeleton vCon input, polls until the execution finishes, asserts the final
  status is `SUCCEEDED`, and confirms the output preserves the top-level
  `"vcon": "0.0.1"` version and carries the three analysis entries in order
  (`transcript`, `entities`, `summary`) with the clinical transcript marked
  `authoritative` and with **no `degraded` markers** on a healthy run. Note the
  canned input has **no real recording**, so the pipeline exercises the
  canned-transcript stub path on the live stack too.
- the **Seal step ran**: it reads the execution output and asserts the Seal
  result carries its redacted pointer, then inspects the Seal step's
  `seal_keys`. When `seal_keys` is non-empty (a run with a real `recording_key`,
  see below), it `head-object`s each of the three Seal siblings
  (`vcon-signed.jws`, `vcon-full.jwe`, `vcon-redacted.jws`) and PASS/FAILs per
  object. When `seal_keys` is empty (the expected case for the **canned**
  no-recording run, which derives no run-key prefix and therefore writes no Seal
  siblings), it records that the canned path writes no siblings and notes that
  confirming the physical S3 siblings requires a run with a real
  `recording_key`.

### The three Seal siblings and when they are written

The Seal step derives the run-key prefix from `event['recording_key']` (via
`rsplit('/', 1)[0]`) and writes three objects next to the skeleton vCon:

```
<room_name>/<session_start_iso>/vcon-signed.jws      (application/vcon+jws)
<room_name>/<session_start_iso>/vcon-full.jwe        (application/jose)
<room_name>/<session_start_iso>/vcon-redacted.jws   (application/vcon+jws)
```

The redacted sibling is itself a **signed vCon (JWS)**, and its top-level
`redacted` pointer carries a `content_hash` of the encrypted full copy
(`vcon-full.jwe`); see README.md's
[Redaction scope & limitations](../README.md#redaction-scope--limitations) for
the sourced detail on what the redaction does and does not strip and the
conformance behaviors.

Its output dict carries `seal_keys` (`{signed_key, full_key, redacted_key}`)
when those PUTs happened, and an empty `seal_keys` dict when there was no bucket
and prefix to write to. The redacted pointer url is
`s3://<bucket>/<full_key>`. Because the canned validation input has **no**
`recording_key`, the Seal step derives no prefix and writes **no** siblings on
that run, so `validate-live.sh` only head-objects the three siblings when a run
actually produced them. To exercise and confirm the physical siblings, run the
agent against a real recording (so the skeleton carries a `recording` dialog
object and the pipeline threads a real `recording_key` through to Seal).

### Full local end-to-end sequence (Docker + AWS creds)

With Docker running and real AWS credentials configured, the complete live path
from a clean checkout is:

```bash
# 1. Install the AWS SAM CLI (one time). See the install guide linked above.

# 2. Build the Seal function in the Lambda build container (Docker required).
sam build --use-container --template infrastructure/template.yaml

# 3. Deploy the stack (first time: --guided; afterwards: the explicit form).
sam deploy \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName=vcon-livekit-demo Environment=dev BedrockModelId=global.anthropic.claude-haiku-5-5

# 4. Read the stack Outputs (BucketName / StateMachineArn / Region) into .env.
cp .env.example .env     # at the repository root
aws cloudformation describe-stacks \
  --stack-name vcon-livekit-demo \
  --query "Stacks[0].Outputs" \
  --output table
# then paste BucketName -> BUCKET_NAME, StateMachineArn -> STATE_MACHINE_ARN,
# Region -> AWS_REGION into .env (see the Outputs mapping table above).

# 5. Produce a vCon: either run the agent against a real call (which records to
#    S3 and threads a real recording_key, producing the three Seal siblings) ...
python -m vcon_agent.agent dev
#    ... or start a canned execution by hand (reaches Merge + Seal, but writes
#    NO Seal siblings because the canned input has no recording_key):
aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input '{"vcon": {"vcon": "0.0.1", "uuid": "00000000-0000-0000-0000-000000000000", "subject": "demo call", "parties": [{"name": "Agent"}, {"name": "Caller"}], "dialog": [], "analysis": []}}'

# 6. Confirm the pipeline + Seal end to end.
AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh
```

A run with a **real `recording_key`** (step 5, via the agent) is what produces
the three physical Seal siblings in S3; only then will `validate-live.sh`
head-object and PASS them. A canned-only run still confirms the Seal state ran
and carried its redacted pointer.

### What can be validated offline vs. what needs live AWS

The template now carries a SAM transform (`Transform: AWS::Serverless-2016-10-31`)
for the packaged Seal function, so the offline checks include a SAM-aware lint.

- **Validated offline:** the full pytest suite is green
  (`python3 -m pytest tests/ -q`, which exercises the correctness-critical
  `src/vcon_agent/{enrichment,vcon}.py` and the packaged
  `infrastructure/functions/seal/sealing.py` cores and the packaged
  `seal_handler` with fakes); `cfn-lint infrastructure/template.yaml` reports
  **zero errors**; and `sam validate --lint --template infrastructure/template.yaml`
  confirms the SAM template is valid. Each remaining inline Lambda body also
  compiles cleanly (`python3 -m py_compile`).
- **Requires live AWS credentials (and Docker):** `sam build --use-container`
  (compiles the Seal function's native `cryptography` wheel for the Lambda
  runtime), `sam deploy` (replaces the old `aws cloudformation deploy`), running
  the agent to produce a vCon, and `./infrastructure/scripts/validate-live.sh`.
  The underlying `aws stepfunctions start-execution`,
  `aws cloudformation describe-stacks`, and `aws cloudformation delete-stack`
  calls likewise need a real account. These are documented above and should be
  run against a real account.

## Shortcuts (demo) vs. production

- 🩹 **Broad-ish IAM** (service-level actions, a single shared Lambda role,
  single bucket) for readability. → *Production:* least-privilege per
  action/resource, condition keys, a separate role per Lambda.
- 🩹 **Inline Lambda code for seven of the eight steps**, deployed with the
  template, restating the pure `enrichment.py` / `vcon.py` logic because inline
  code cannot import the repo package at AWS runtime. The **Seal** step is the
  exception: it is a packaged (container-built) `AWS::Serverless::Function` that
  imports the sealing core packaged alongside its handler
  (`functions/seal/sealing.py`), so its crypto is **not** hand-mirrored. It is packaged rather than
  inlined because its `jwcrypto` / `cryptography` dependency ships native
  binaries that cannot be inlined and must be compiled for the Lambda runtime
  (hence `sam build --use-container`). → *Production:* CI/CD, versioned artifacts
  in S3/ECR, a shared layer/package for all steps instead of copy-pasted logic,
  separate infra vs. app pipelines.
- 🩹 **The transcription wait has no stall guard.** The state machine polls the
  Transcribe Medical job on a fixed interval and waits as long as the real job
  takes -- correct for long conversations -- but a job that hangs, vanishes, or
  errors persistently would poll indefinitely (a persistent `GetTranscription`
  error currently looks identical to "in progress"). A genuinely `FAILED` job is
  already handled. → *Production:* stall/liveness detection (bounded
  non-progressing polls) plus retry-on-transient-error and a `Catch` to a visible
  failure state, while a healthy long job keeps waiting.
- 🩹 **Optional canned-transcript stub**: if there is no real recording (or
  starting the job fails), `start-transcription-step` falls back to a canned
  transcript so the pipeline is runnable end to end without a recording. →
  *Production:* always run the real job, or use AWS HealthScribe for
  clinician-grade notes + transcript in one service.
- 🩹 **Minimal Comprehend Medical entity set** (meds + conditions only). →
  *Production:* the full entity set with RxNorm / ICD-10-CM linking and
  confidence thresholds.
- 🩹 **Turn-index provenance mapped by timestamp overlap** (simple alignment of
  medical segments to dialog turns). The segment-level mapper
  (`enrichment.map_turns_by_timestamp_overlap`) is the production-direction
  logic and is unit-tested, but it is intentionally not wired into the demo
  pipeline: the inline merge/comprehend steps use coarser attribution (the
  transcript covers every text turn, entities pin to the first patient turn)
  because the canned/demo Transcribe path does not emit per-segment timings to
  feed it. → *Production:* robust alignment between the diarized medical
  transcript and the live turns (word-level timing, speaker reconciliation),
  fed through that mapper.
- 🩹 **Single-turn entity attribution** (all Comprehend entities pinned to the
  first patient text turn). → *Production:* per-entity character offsets mapped
  to the turn each entity actually came from.
- 🩹 **Analysis fails open with a `degraded` marker** (Comprehend / Bedrock
  failures yield an empty result plus a `degraded` reason on the entry, so the
  demo still finishes while the failure stays visible). → *Production:* a
  `Catch` to a visible alarm state plus retries so an operator is paged, rather
  than fail-open.
- 🩹 **SSE-S3 default encryption**, no customer-managed KMS key. →
  *Production:* SSE-KMS with a CMK, key policy, and separate access scopes for
  full vs. redacted vCons.
- 🩹 **Bedrock model access is a one-time account-level enablement** (for
  Anthropic, a first-time-use agreement) done out-of-band, not provisioned by
  this template. → *Production:* provisioned via account setup / landing zone.
- 🩹 **Single region, no data-residency controls.** → *Production:* region
  pinning, BAAs, VPC endpoints for the AWS service calls.
