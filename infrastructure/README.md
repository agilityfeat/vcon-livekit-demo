# Cloud infrastructure (CloudFormation)

A single, flat CloudFormation template (`infrastructure/template.yaml`) that
provisions the minimal AWS backend the vCon + LiveKit demo consumes. It deploys
with one command, exposes the identifiers the local app needs as stack
**Outputs**, and tears down cleanly.

Everything lives in one template, Lambda code is inlined, and IAM is kept broad
for readability. The acknowledged shortcuts and their production counterparts
are listed at the end.

> All commands below are written to be run from the **repository root** (the
> parent of this `infrastructure/` folder), which is why paths are prefixed
> with `infrastructure/`.

## What the template creates

| Resource | Purpose |
|---|---|
| **S3 bucket** (`AWS::S3::Bucket`) | Recordings from LiveKit Egress + skeleton / enriched / redacted vCon JSON. Versioning on, SSE-S3 default encryption, all public access blocked. |
| **Lambda functions** (`AWS::Lambda::Function`) | The pipeline steps invoked by the state machine. Inline Python stubs (`transcribe-step`, `comprehend-step`, `enrich-step`). |
| **Step Functions state machine** (`AWS::StepFunctions::StateMachine`) | The enrichment orchestrator. Runs the Lambdas in sequence; invoked by the local app via `StartExecution`. |
| **IAM roles** (`AWS::IAM::Role`) | A Step Functions execution role and a Lambda execution role granting access to Transcribe Medical, Comprehend Medical, Bedrock, S3, and CloudWatch Logs. |

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
it once per account/region before the `enrich` step can call Bedrock:

1. Open the **Amazon Bedrock** console in your target region.
2. Go to **Model access** (left nav).
3. Request/enable access for the model(s) you intend to invoke (for example an
   Anthropic Claude or Amazon Titan text model).
4. Wait until the status shows **Access granted**.

The stubbed pipeline runs end to end without this, but a real
`bedrock:InvokeModel` call will fail until the model is enabled.

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
  --parameter-overrides ProjectName=vcon-livekit-demo Environment=dev
```

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
**unique key prefix** built from the room name, the session start, and a
timestamp:

```
<room_name>/<session_start_iso>/<timestamp>/recording.ogg
<room_name>/<session_start_iso>/<timestamp>/vcon-skeleton.json
<room_name>/<session_start_iso>/<timestamp>/vcon-enriched.json
<room_name>/<session_start_iso>/<timestamp>/vcon-redacted.json
```

Bucket **versioning** is enabled as cheap insurance in case a key is ever
reused.

## Run the pipeline (trivial `StartExecution`)

Grab the state machine ARN from the Outputs (or your `.env`), then start an
execution with a canned skeleton vCon as input. The stubbed steps pass the vCon
through and the execution reaches the `Succeed` state end to end.

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

Check the result:

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
- the three pipeline Lambda functions (`*-transcribe-step`,
  `*-comprehend-step`, `*-enrich-step`) exist;
- the Step Functions state machine status is `ACTIVE`;
- an **end-to-end execution**: it starts the state machine with the canned
  skeleton vCon input, polls until the execution finishes, asserts the final
  status is `SUCCEEDED`, and confirms the output preserves the top-level
  `"vcon": "0.0.1"` version and carries the three analysis entries in order
  (`transcript`, `phi-detection`, `summary`).

### What can be validated offline vs. what needs live AWS

- **Validated offline:** `cfn-lint infrastructure/template.yaml` reports **zero
  errors** (and zero warnings), and the inline Lambda Python stubs compile
  cleanly (`python3 -m py_compile`).
- **Requires live AWS credentials:** `aws cloudformation deploy`,
  `aws stepfunctions start-execution`, `aws cloudformation describe-stacks`, and
  `aws cloudformation delete-stack`. These are documented above and should be run
  against a real account. `aws cloudformation validate-template` also calls the
  AWS API and therefore needs credentials.

## Acknowledged shortcuts (demo) vs. production

- 🩹 **Broad-ish IAM** (service-level actions, a single shared Lambda role,
  single bucket) for readability. → *Production:* least-privilege per
  action/resource, condition keys, a separate role per Lambda.
- 🩹 **Inline Lambda code** deployed with the template. → *Production:* CI/CD,
  versioned artifacts in S3/ECR, separate infra vs. app pipelines.
- 🩹 **SSE-S3 default encryption**, no customer-managed KMS key. →
  *Production:* SSE-KMS with a CMK, key policy, and separate access scopes for
  full vs. redacted vCons.
- 🩹 **Bedrock model access is a manual console step** done once. →
  *Production:* provisioned via account setup / landing zone.
- 🩹 **Single region, no data-residency controls.** → *Production:* region
  pinning, BAAs, VPC endpoints for the AWS service calls.
