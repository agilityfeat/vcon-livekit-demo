# Validation

What you can check offline, and what needs a deployed stack. Commands run from the
repository root.

## Offline (no AWS credentials)

| Check | Command | Notes |
|---|---|---|
| Test suite | `python3 -m pytest tests/ -q` | Covers the pure cores (`vcon.py`, `enrichment.py`, `persistence.py`, `sealing.py`), the packaged `seal_handler` with fakes, the backend `records.py`, fixtures, and the inline Lambda bodies extracted from the template. |
| Template lint | `cfn-lint infrastructure/template.yaml` | The authoritative offline check for the template. Expect zero errors. |
| Lint wrapper | `./infrastructure/scripts/validate.sh` | Runs `cfn-lint` on `infrastructure/template.yaml` and exits non-zero if `cfn-lint` is missing or fails. Install with `pip3 install --user cfn-lint` (or into a virtualenv). |
| SAM validation | `sam validate --lint --template infrastructure/template.yaml` | SAM-aware lint for the `AWS::Serverless::Function` (needs the SAM CLI, included in the Nix shell). |

The inline Lambda bodies are also compiled by the test suite, so a syntax error in the
template's embedded Python fails `pytest`.

## Live (needs AWS credentials)

These need real AWS access, and the first two also need Docker:

- `sam build --use-container` compiles the Seal function's native `cryptography` wheel
  for the Lambda runtime.
- `sam deploy` deploys the stack. See [infrastructure.md](infrastructure.md#deploy).
- Running the agent against a real call to produce a vCon.
- `./infrastructure/scripts/validate-live.sh`.

### `validate-live.sh`

Unlike `cfn-lint`, this script calls the AWS API against the deployed stack:

```bash
AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh

# non-default stack name
STACK_NAME=my-stack AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh
```

`STACK_NAME` defaults to `vcon-livekit-demo`. The region comes from the stack's `Region`
output. Each check prints a labelled `PASS` or `FAIL` line, and the script exits
non-zero if any check failed. It verifies:

- the stack status is `CREATE_COMPLETE` or `UPDATE_COMPLETE`;
- the S3 bucket has versioning `Enabled`;
- the default encryption algorithm is `AES256` (SSE-S3);
- all four public access block flags are `true`;
- the eight pipeline Lambdas exist: the seven inline steps (`*-poll-recording-step`,
  `*-load-skeleton-step`, `*-start-transcription-step`, `*-get-transcription-step`,
  `*-comprehend-step`, `*-bedrock-step`, `*-merge-step`) plus the packaged `*-seal-step`;
- the state machine status is `ACTIVE`;
- an **end-to-end execution** with the canned skeleton input succeeds, preserves the
  top-level `"vcon": "0.0.1"`, and carries the three analysis entries in order
  (`transcript`, `entities`, `summary`) with the clinical transcript marked
  `authoritative` and **no `degraded` markers** on a healthy run. The canned input has
  no real recording, so this exercises the canned-transcript stub path;
- the **Seal step ran**: its result carries the redacted pointer, and when `seal_keys`
  is non-empty the script `head-object`s each of `vcon-signed.jws`, `vcon-full.jwe`, and
  `vcon-redacted.jws`. For the canned run `seal_keys` is empty (no `recording_key`, so
  no prefix and no seal objects), and the script records that instead of inventing a
  pass. To confirm the physical objects, run the agent against a real recording.

### Full sequence (Docker and AWS credentials)

From a clean checkout, with Docker running and AWS credentials configured:

```bash
# 1. Build the Seal function in the Lambda build container.
sam build --use-container --template infrastructure/template.yaml

# 2. Deploy. VconSigningPrivateJwk must match VCON_SIGNING_PRIVATE_JWK in .env.
sam deploy \
  --stack-name vcon-livekit-demo \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides VconSigningPrivateJwk='<jwk-or-pem>'

# 3. Read the stack Outputs into .env (see infrastructure.md).
cp .env.example .env

# 4. Produce a vCon: run the agent against a real call, which records to S3 and
#    threads a real recording_key (producing the three seal objects) ...
python -m vcon_agent.agent dev
#    ... or start a canned execution by hand (see infrastructure.md).

# 5. Confirm the pipeline and Seal end to end.
AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh
```

Only a run with a real `recording_key` produces the three seal objects.
