# Cloud infrastructure (CloudFormation)

A single, flat CloudFormation template (`infrastructure/template.yaml`) that
provisions the minimal AWS backend the vCon + LiveKit demo consumes. It deploys
with one command, exposes the identifiers the local app needs as stack
**Outputs**, and tears down cleanly.

Everything lives in one template, Lambda code is inlined, and IAM is kept broad
for readability. The acknowledged shortcuts and their production counterparts
are listed at the end.

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
| **Lambda functions** (`AWS::Lambda::Function`) | The seven real pipeline steps invoked by the state machine, inline Python: `poll-recording-step` (poll S3 for the recording), `load-skeleton-step` (load the skeleton vCon), `start-transcription-step` (start the Transcribe Medical batch job), `get-transcription-step` (poll the job once), `comprehend-step` (Comprehend Medical meds + conditions), `bedrock-step` (physician summary), and `merge-step` (merge results into `analysis[]` with turn-index provenance). Each body mirrors the pure, offline-tested logic in `src/vcon_agent/enrichment.py` + `vcon.py` (inline code cannot import the repo package at AWS runtime). |
| **Step Functions state machine** (`AWS::StepFunctions::StateMachine`) | The enrichment orchestrator. Polls S3 for the recording, runs Transcribe Medical as an async batch whose completion the state machine awaits, then runs Comprehend Medical and Bedrock as a parallel branch, then merges into `analysis[]`. Invoked by the local app via a deterministic, idempotent `StartExecution`. |
| **IAM roles** (`AWS::IAM::Role`) | A Step Functions execution role (invokes all seven pipeline Lambdas + writes execution logs) and a Lambda execution role granting access to Transcribe Medical, Comprehend Medical, Bedrock, S3, and CloudWatch Logs. |

### Explicitly NOT created (and why)

- **Transcribe Medical, Comprehend Medical, Bedrock** — these are managed
  services you *call*, not resources you create. The template only grants IAM
  permission to use them.
- **Bedrock model access** — a per-model **console opt-in**, not a
  CloudFormation resource. See the manual prerequisite below.
- **No EventBridge / webhook endpoint** — enrichment is triggered by the local
  app via `StartExecution`, so no inbound event plumbing is needed.
- **No compute for the agent** (no ECS / EC2 / Fargate) — the agent runs
  locally.

## Manual prerequisite: enable Bedrock model access

Bedrock foundation models require a **one-time, per-model opt-in** in the AWS
console. This cannot be expressed as a CloudFormation resource, so you must do
it once per account/region before the `bedrock-step` can call Bedrock:

1. Open the **Amazon Bedrock** console in your target region.
2. Go to **Model access** (left nav).
3. Request/enable access for the model you intend to invoke. The default is
   **Anthropic Claude Haiku 5.5** (`anthropic.claude-haiku-5-5`).
4. Wait until the status shows **Access granted**.

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

## Deploy (one command)

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM
```

Optional parameter overrides:

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName=vcon-livekit-demo Environment=dev BedrockModelId=global.anthropic.claude-haiku-5-5
```

`BedrockModelId` must be a Bedrock inference-profile id (default
`global.anthropic.claude-haiku-5-5`); override it to pin a different model or a
geo profile such as `us.anthropic.claude-haiku-5-5`.

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
<room_name>/<session_start_iso>/vcon-redacted.json
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
- the seven pipeline Lambda functions exist (`*-poll-recording-step`,
  `*-load-skeleton-step`, `*-start-transcription-step`,
  `*-get-transcription-step`, `*-comprehend-step`, `*-bedrock-step`,
  `*-merge-step`);
- the Step Functions state machine status is `ACTIVE`;
- an **end-to-end execution**: it starts the state machine with the canned
  skeleton vCon input, polls until the execution finishes, asserts the final
  status is `SUCCEEDED`, and confirms the output preserves the top-level
  `"vcon": "0.0.1"` version and carries the three analysis entries in order
  (`transcript`, `entities`, `summary`) with the clinical transcript marked
  `authoritative` and with **no `degraded` markers** on a healthy run. Note the
  canned input has **no real recording**, so the pipeline exercises the
  canned-transcript stub path on the live stack too.

### What can be validated offline vs. what needs live AWS

- **Validated offline:** `cfn-lint infrastructure/template.yaml` reports **zero
  errors** (and zero warnings), each inline Lambda body compiles cleanly
  (`python3 -m py_compile`), and the correctness-critical logic the inline
  bodies mirror (`src/vcon_agent/enrichment.py` + `vcon.py`) is exercised by the
  offline pytest suite (`python3 -m pytest tests/ -q`).
- **Requires live AWS credentials:** `aws cloudformation deploy`,
  `aws stepfunctions start-execution`, `aws cloudformation describe-stacks`, and
  `aws cloudformation delete-stack`. These are documented above and should be run
  against a real account. `aws cloudformation validate-template` also calls the
  AWS API and therefore needs credentials.

## Acknowledged shortcuts (demo) vs. production

- 🩹 **Broad-ish IAM** (service-level actions, a single shared Lambda role,
  single bucket) for readability. → *Production:* least-privilege per
  action/resource, condition keys, a separate role per Lambda.
- 🩹 **Inline Lambda code** deployed with the template, restating the pure
  `enrichment.py` / `vcon.py` logic because inline code cannot import the repo
  package at AWS runtime. → *Production:* CI/CD, versioned artifacts in S3/ECR, a
  shared layer/package instead of copy-pasted logic, separate infra vs. app
  pipelines.
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
- 🩹 **Bedrock model access is a manual console step** done once. →
  *Production:* provisioned via account setup / landing zone.
- 🩹 **Single region, no data-residency controls.** → *Production:* region
  pinning, BAAs, VPC endpoints for the AWS service calls.
