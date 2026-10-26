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

For full deploy, configuration, and teardown instructions — including the
one-command deploy, mapping stack Outputs into a local `.env` (copied from
`.env.example`), the manual Bedrock model-access prerequisite, and local
validation — see **[infrastructure/README.md](infrastructure/README.md)**.
