# Infrastructure

The AWS backend is defined in `template.yaml` (CloudFormation with the SAM transform),
with the packaged Seal Lambda in `functions/seal/` and validation scripts in `scripts/`.

- Deploy, Bedrock prerequisite, S3 layout, teardown: [../docs/infrastructure.md](../docs/infrastructure.md)
- Validation (`cfn-lint`, `validate.sh`, `validate-live.sh`): [../docs/validation.md](../docs/validation.md)
