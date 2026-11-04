# Infrastructure

A single, flat CloudFormation template (`infrastructure/template.yaml`) provisions the
AWS backend the demo consumes. It deploys with AWS SAM, exposes the identifiers the
local app needs as stack **Outputs**, and tears down cleanly.

Most Lambda code is inlined and IAM is kept broad for readability. The exception is
the **Seal** step, a packaged, container-built `AWS::Serverless::Function`, so it can
ship its native `jwcrypto` / `cryptography` dependency and import the authoritative
sealing core directly. The template carries the SAM transform
(`Transform: AWS::Serverless-2016-10-31`) for that one function, which is why the
stack is deployed with `sam build` and `sam deploy`.

> Commands on this page are run from the **repository root**.

## What the template creates

| Resource | Purpose |
|---|---|
| **S3 bucket** (`AWS::S3::Bucket`) | Recordings from LiveKit Egress plus the vCon objects. Versioning on, SSE-S3 default encryption, all public access blocked. Named `<ProjectName>-<Environment>-<AccountId>-<Region>`. |
| **Seven inline Lambdas** (`AWS::Lambda::Function`) | The boto3-only pipeline steps, inline Python: `poll-recording-step`, `load-skeleton-step`, `start-transcription-step`, `get-transcription-step`, `comprehend-step`, `bedrock-step`, `merge-step`. Each body mirrors the pure logic in `src/vcon_agent/enrichment.py` and `vcon.py`, since inline code cannot import the repo package. They import only `boto3`, which the runtime provides, so they need no build. |
| **Packaged Seal Lambda** (`AWS::Serverless::Function`) | `seal-step`, wired after `merge-step`. `CodeUri: functions/seal/`, `Handler: seal_handler.handler`, `Runtime: python3.12`, `Metadata.BuildMethod: python3.12`. Imports `functions/seal/sealing.py` directly. See [sign-encrypt-redact.md](sign-encrypt-redact.md). |
| **Step Functions state machine** | The enrichment orchestrator; states are listed in [enrichment-pipeline.md](enrichment-pipeline.md#states). Logs go to a CloudWatch log group (14 day retention). |
| **IAM roles** | A Step Functions execution role (invokes the pipeline Lambdas, writes execution logs) and a shared Lambda execution role with access to Transcribe Medical, Comprehend Medical, Bedrock, S3, and CloudWatch Logs. |

Parameters: `ProjectName` (default `vcon-livekit-demo`), `Environment` (`dev`),
`BedrockModelId` (`global.anthropic.claude-haiku-5-5`), and `VconSigningPrivateJwk`
(empty by default).

### Explicitly not created

- **Transcribe Medical, Comprehend Medical, Bedrock.** These are managed services you
  call, not resources you create. The template only grants IAM permission to use them.
- **Bedrock model access.** A one-time, account-level enablement, not a CloudFormation
  resource. See [Bedrock model access](#bedrock-model-access).
- **EventBridge or a webhook endpoint.** Enrichment is started by the agent through
  `StartExecution`, so no inbound event plumbing exists. See
  [production-considerations.md](production-considerations.md#triggering-and-persistence)
  for the planned event-driven alternative.
- **Compute for the agent** (no ECS, EC2, or Fargate). The agent runs locally.

## Bedrock model access

Model access is a one-time, account-level step, so do it before the `bedrock-step` can
call Bedrock. It works differently by region:

- In **commercial regions** access to most foundation models is enabled automatically
  on first invocation, so there is no **Model access** page to toggle. That page under
  Bedrock configurations applies to **AWS GovCloud (US)** only.
- **Anthropic** models (the default here) additionally require a one-time **First-Time
  Use (FTU) use-case form** per account or organization, submitted **before** the first
  invoke. Submit it in the console or with the CLI/SDK:
  - **Console:** open the **Model catalog**, pick an Anthropic model and open it in the
    playground (or go to the **Anthropic** providers page), then complete the use-case
    form. The model may appear greyed out with no visible "accept agreement" control
    until the form is on file.
  - **CLI/SDK:** `aws bedrock put-use-case-for-model-access` with the base64-encoded
    use-case form JSON.

If the console is unavailable or the model stays greyed out, the CLI fallback is:

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

# 3. Verify access. agreementAvailability.status: AVAILABLE means ready,
#    PENDING means still being set up (give it a few minutes), and
#    NOT_AVAILABLE means no agreement is on file yet.
aws bedrock get-foundation-model-availability \
  --region <your-region> \
  --model-id anthropic.claude-haiku-5-5
```

The agreement may sit in `PENDING` for a few minutes before it becomes `AVAILABLE`; an
email is sent when it is ready.

**Permissions for the one-time enablement.** The identity that triggers the first
subscription needs AWS Marketplace permissions (`aws-marketplace:Subscribe`,
`aws-marketplace:Unsubscribe`, `aws-marketplace:ViewSubscriptions`) and a valid payment
method on the account. These are needed only for the enablement. Afterwards the
pipeline invokes with `bedrock:InvokeModel`, which the template grants.

**Model id.** Claude Haiku 5.5 is a cross-region-inference model. Calling the bare id
`anthropic.claude-haiku-5-5` on the bedrock-runtime `InvokeModel` endpoint fails with
HTTP 400, so it must be invoked through an **inference-profile id**: the base id with a
region prefix such as `global.`, `us.`, `eu.`, `au.`, or `jp.`. The `BedrockModelId`
parameter defaults to the global profile `global.anthropic.claude-haiku-5-5`. Override
it at deploy time to pin a geo profile (for example `us.anthropic.claude-haiku-5-5`)
without editing the template.

The pipeline still runs end to end without model access: the `bedrock-step` fails open
with an empty summary and a `degraded` marker (see
[enrichment-pipeline.md](enrichment-pipeline.md#failure-behavior)). A real summary only
appears once the model is enabled.

## Deploy

### Prerequisites

- **AWS SAM CLI**, which provides `sam`. The Nix dev shell includes it; otherwise see
  the [install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
- **Docker**, installed and running, because the Seal function is built in a Lambda
  build container. The Nix shell does not provide it.
- AWS credentials for the target account, and the Bedrock prerequisite above.

### Why `--use-container`

The Seal function depends on `jwcrypto`, which depends on `cryptography`, which ships
compiled native binaries (a Rust/CFFI extension linked against OpenSSL). They must be
built for the Lambda environment the function runs in: Amazon Linux 2023, CPython 3.12,
`x86_64`. If SAM packaged your local-OS site-packages instead, a wheel compiled for your
machine (for example macOS/arm64) would fail to import at runtime. `--use-container`
makes `sam build` compile the right wheel inside the matching build image. The seven
inline Lambdas are unaffected.

### Build and deploy

```bash
# Build the Seal function in the Lambda build container.
sam build --use-container --template infrastructure/template.yaml

# First deploy: --guided asks for stack name, region, capabilities, and parameter
# overrides, and offers to save them to samconfig.toml for next time.
sam deploy --guided
```

Later deploys can reuse the saved config with `sam deploy`, or pass everything
explicitly:

```bash
sam deploy \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName=vcon-livekit-demo Environment=dev \
      BedrockModelId=global.anthropic.claude-haiku-5-5 \
      VconSigningPrivateJwk='<jwk-or-pem>'
```

### Shared signing key

`VconSigningPrivateJwk` is the demo signing key the Seal step uses to sign and encrypt
the vCon artifacts. It **must** equal the local `.env` `VCON_SIGNING_PRIVATE_JWK`,
because the backend verifies and decrypts those artifacts with it (only the private key
must match; the backend derives the public half). Leaving it empty makes the Seal step
generate an ephemeral per-invocation key, and those records can never be read back.

Do this once, **before the first call whose record you want to view**:

1. Generate a key:

   ```bash
   python -c "from jwcrypto import jwk; print(jwk.JWK.generate(kty='RSA', size=2048).export(private_key=True))"
   ```

2. Pass it as `VconSigningPrivateJwk` in `sam deploy` (above).
3. Put the same value in `.env` as `VCON_SIGNING_PRIVATE_JWK`.

If you skip this or change the key later, records sealed with the old key stay
unreadable; fix the key on both sides and place a new call.

## Read the Outputs into `.env`

The stack exports `BucketName`, `StateMachineArn`, and `Region`:

```bash
aws cloudformation describe-stacks \
  --stack-name vcon-livekit-demo \
  --query "Stacks[0].Outputs" \
  --output table
```

| Stack Output | `.env` variable |
|---|---|
| `BucketName` | `BUCKET_NAME` |
| `StateMachineArn` | `STATE_MACHINE_ARN` |
| `Region` | `AWS_REGION` |

```bash
cp .env.example .env
# then paste the Output values into .env
```

The remaining variables are described in [getting-started.md](getting-started.md#configuration-env).

## S3 key convention

Every run writes under a unique prefix built from the room name and the session start
(a microsecond-precision UTC instant captured once per run):

```
<room_name>/<session_start_iso>/recording.ogg          LiveKit Egress, mixed audio
<room_name>/<session_start_iso>/EG_<egress_id>.json    LiveKit Egress manifest (provenance)
<room_name>/<session_start_iso>/vcon-skeleton.json     agent, on session end
<room_name>/<session_start_iso>/vcon-signed.jws        Seal step
<room_name>/<session_start_iso>/vcon-full.jwe          Seal step
<room_name>/<session_start_iso>/vcon-redacted.jws      Seal step
```

`session_start_iso` identifies the run as a single key segment, and bucket versioning is
enabled as cheap insurance in case a key is ever reused. The enriched vCon is not stored
as its own object: it is the payload inside `vcon-signed.jws` and `vcon-full.jwe`, and
its redacted form is `vcon-redacted.jws`. The backend lists records by scanning for
`vcon-signed.jws`. Transcribe Medical writes its result JSON to the same bucket outside
this prefix.

## Running the pipeline

In normal use the **agent process** starts it, at session end, right after uploading the
skeleton (see [enrichment-pipeline.md](enrichment-pipeline.md#trigger-and-idempotency)).

To start an execution by hand, grab the ARN from the Outputs (or `.env`) and pass a
canned skeleton. With no recording in the input, the poll step reports ready, and
`start-transcription-step` takes the canned-transcript stub path, so the run still
reaches `Merge`:

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

aws stepfunctions describe-execution --execution-arn "<execution-arn-from-above>"
```

The final output carries `"vcon": "0.0.1"` and `analysis[]` with types `transcript`,
`entities`, `summary` in that order. A canned run reaches `Seal` but writes **no** seal
objects, because the input has no `recording_key`. To get all three seal objects, run
the agent against a real call.

## Teardown

```bash
aws cloudformation delete-stack --stack-name vcon-livekit-demo
aws cloudformation wait stack-delete-complete --stack-name vcon-livekit-demo
```

The bucket and the state machine log group use `DeletionPolicy: Delete`, so
`delete-stack` removes everything the template created.

> **Note:** S3 refuses to delete a non-empty bucket. Empty it first
> (`aws s3 rm s3://$BUCKET_NAME --recursive`, and delete object versions if versioning
> accumulated any) before `delete-stack`.

For template linting and the live end-to-end check, see [validation.md](validation.md).
Shortcuts for this infrastructure are in
[production-considerations.md](production-considerations.md#infrastructure).
