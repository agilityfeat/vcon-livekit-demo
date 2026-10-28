#!/usr/bin/env bash
#
# Validate the DEPLOYED CloudFormation stack end to end against real AWS.
#
# Unlike validate.sh (cfn-lint, fully offline), this script talks to the live
# AWS account the stack is deployed in, so it REQUIRES AWS credentials. Run it
# with your own environment, for example:
#
#   AWS_PROFILE=<profile> ./infrastructure/scripts/validate-live.sh
#
# Optional environment overrides:
#   STACK_NAME   CloudFormation stack name (default: vcon-livekit-demo)
#
# The region is taken from your AWS config/profile; this script does not force
# --region (it only derives one from the stack's Region output for the handful
# of calls that benefit from it).
#
set -euo pipefail

STACK_NAME="${STACK_NAME:-vcon-livekit-demo}"

# ---------------------------------------------------------------------------
# PASS/FAIL accounting. Each check prints a labelled line and bumps a counter;
# we do NOT abort on the first failure so the output is a full report.
# ---------------------------------------------------------------------------
CHECKS=0
FAILURES=0

pass() {
  CHECKS=$((CHECKS + 1))
  echo "PASS: $1"
}

fail() {
  CHECKS=$((CHECKS + 1))
  FAILURES=$((FAILURES + 1))
  echo "FAIL: $1"
}

# ---------------------------------------------------------------------------
# Preflight: the aws CLI must be on PATH.
# ---------------------------------------------------------------------------
if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI not found on PATH. Install AWS CLI v2 and configure credentials first." >&2
  exit 1
fi

echo "Validating deployed stack '${STACK_NAME}' against live AWS ..."
echo

# ---------------------------------------------------------------------------
# Read the stack Outputs (BucketName, StateMachineArn, Region) via JMESPath.
# A missing stack or an empty Output is a hard error: without these there is
# nothing to validate.
# ---------------------------------------------------------------------------
read_output() {
  # $1 = Output key. Prints the OutputValue, or empty on miss.
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
    --output text 2>/dev/null
}

if ! aws cloudformation describe-stacks --stack-name "${STACK_NAME}" >/dev/null 2>&1; then
  echo "Could not describe stack '${STACK_NAME}'. Is it deployed and are your credentials/region correct?" >&2
  exit 1
fi

BUCKET_NAME="$(read_output BucketName)"
STATE_MACHINE_ARN="$(read_output StateMachineArn)"
STACK_REGION="$(read_output Region)"

if [[ -z "${BUCKET_NAME}" || "${BUCKET_NAME}" == "None" ]]; then
  echo "Stack Output 'BucketName' is empty. Cannot continue." >&2
  exit 1
fi
if [[ -z "${STATE_MACHINE_ARN}" || "${STATE_MACHINE_ARN}" == "None" ]]; then
  echo "Stack Output 'StateMachineArn' is empty. Cannot continue." >&2
  exit 1
fi
if [[ -z "${STACK_REGION}" || "${STACK_REGION}" == "None" ]]; then
  echo "Stack Output 'Region' is empty. Cannot continue." >&2
  exit 1
fi

# Derive a --region argument from the stack's Region output so every call below
# targets the account/region the stack actually lives in.
REGION_ARG=(--region "${STACK_REGION}")

echo "Stack Outputs:"
echo "  BucketName       = ${BUCKET_NAME}"
echo "  StateMachineArn  = ${STATE_MACHINE_ARN}"
echo "  Region           = ${STACK_REGION}"
echo

# ---------------------------------------------------------------------------
# Check: stack status is CREATE_COMPLETE or UPDATE_COMPLETE.
# ---------------------------------------------------------------------------
STACK_STATUS="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  "${REGION_ARG[@]}" \
  --query 'Stacks[0].StackStatus' \
  --output text 2>/dev/null || echo UNKNOWN)"
if [[ "${STACK_STATUS}" == "CREATE_COMPLETE" || "${STACK_STATUS}" == "UPDATE_COMPLETE" ]]; then
  pass "stack status is ${STACK_STATUS}"
else
  fail "stack status is ${STACK_STATUS} (expected CREATE_COMPLETE or UPDATE_COMPLETE)"
fi

# ---------------------------------------------------------------------------
# Check: S3 bucket versioning is Enabled.
# ---------------------------------------------------------------------------
VERSIONING="$(aws s3api get-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  "${REGION_ARG[@]}" \
  --query 'Status' \
  --output text 2>/dev/null || echo UNKNOWN)"
if [[ "${VERSIONING}" == "Enabled" ]]; then
  pass "S3 bucket versioning is Enabled"
else
  fail "S3 bucket versioning is '${VERSIONING}' (expected Enabled)"
fi

# ---------------------------------------------------------------------------
# Check: S3 default encryption algorithm is AES256 (SSE-S3).
# ---------------------------------------------------------------------------
SSE_ALGO="$(aws s3api get-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  "${REGION_ARG[@]}" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
  --output text 2>/dev/null || echo UNKNOWN)"
if [[ "${SSE_ALGO}" == "AES256" ]]; then
  pass "S3 default encryption is AES256 (SSE-S3)"
else
  fail "S3 default encryption is '${SSE_ALGO}' (expected AES256)"
fi

# ---------------------------------------------------------------------------
# Check: public access block has all four flags true.
# ---------------------------------------------------------------------------
PAB="$(aws s3api get-public-access-block \
  --bucket "${BUCKET_NAME}" \
  "${REGION_ARG[@]}" \
  --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
  --output text 2>/dev/null || echo UNKNOWN)"
if [[ "${PAB}" == $'True\tTrue\tTrue\tTrue' ]]; then
  pass "S3 public access block: all four flags true"
else
  fail "S3 public access block flags are '${PAB}' (expected all true)"
fi

# ---------------------------------------------------------------------------
# Check: the pipeline Lambda functions exist. Names are prefixed with the stack
# name (default ProjectName), e.g. vcon-livekit-demo-dev-comprehend-step. We
# list the functions whose names start with the stack name and assert one of
# each suffix is present. These are the eight pipeline steps: the seven inline
# boto3-only steps (poll S3 -> load skeleton -> start/get Transcribe Medical ->
# Comprehend + Bedrock parallel -> merge) plus the packaged Seal step, so the
# suffix list matches the states defined in template.yaml. The seal-step is the
# container-built SAM function this task made deployable, so check it directly:
# a SealFunction that failed to deploy then produces a crisp FAIL line here
# instead of only surfacing indirectly through the end-to-end execution.
# ---------------------------------------------------------------------------
FUNCTION_NAMES="$(aws lambda list-functions \
  "${REGION_ARG[@]}" \
  --query "Functions[?starts_with(FunctionName, '${STACK_NAME}')].FunctionName" \
  --output text 2>/dev/null || echo "")"

for suffix in \
  poll-recording-step \
  load-skeleton-step \
  start-transcription-step \
  get-transcription-step \
  comprehend-step \
  bedrock-step \
  merge-step \
  seal-step; do
  if echo "${FUNCTION_NAMES}" | tr '\t' '\n' | grep -q -- "-${suffix}$"; then
    pass "Lambda function present: *-${suffix}"
  else
    fail "Lambda function missing: expected a '${STACK_NAME}-*-${suffix}' function"
  fi
done

# ---------------------------------------------------------------------------
# Check: state machine status is ACTIVE.
# ---------------------------------------------------------------------------
SM_STATUS="$(aws stepfunctions describe-state-machine \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  "${REGION_ARG[@]}" \
  --query 'status' \
  --output text 2>/dev/null || echo UNKNOWN)"
if [[ "${SM_STATUS}" == "ACTIVE" ]]; then
  pass "state machine status is ACTIVE"
else
  fail "state machine status is '${SM_STATUS}' (expected ACTIVE)"
fi

# ---------------------------------------------------------------------------
# END-TO-END: start an execution with the canned skeleton vCon input (the exact
# input documented in infrastructure/README.md), poll until it finishes, then
# assert SUCCEEDED and that the output preserves the vCon version and carries
# the three analysis entries in order.
#
# NOTE: this canned input has NO real recording dialog object, so there is no
# recording file in S3. The pipeline tolerates that on purpose: the poll step
# reports ready (nothing to wait for) and the transcribe step takes the
# canned-transcript stub path, so the execution still reaches Merge and
# produces the real analysis types. Allow generous time because the state
# machine runs the Transcribe wait/poll loop and the Comprehend/Bedrock
# parallel branch.
# ---------------------------------------------------------------------------
CANNED_INPUT='{
  "vcon": {
    "vcon": "0.0.1",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "subject": "demo call",
    "parties": [{"name": "Agent"}, {"name": "Caller"}],
    "dialog": [],
    "analysis": []
  }
}'

echo
echo "Starting an end-to-end execution ..."
EXECUTION_ARN="$(aws stepfunctions start-execution \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  "${REGION_ARG[@]}" \
  --input "${CANNED_INPUT}" \
  --query 'executionArn' \
  --output text 2>/dev/null || echo "")"

if [[ -z "${EXECUTION_ARN}" || "${EXECUTION_ARN}" == "None" ]]; then
  fail "could not start a state machine execution"
else
  echo "  executionArn = ${EXECUTION_ARN}"

  # Poll describe-execution until status leaves RUNNING, with a sane timeout
  # (~120s: 60 attempts, 2s apart). The state machine itself waits in 5s
  # increments (recording poll + Transcribe poll), so give it room.
  EXEC_STATUS="RUNNING"
  EXEC_OUTPUT=""
  for _ in $(seq 1 60); do
    EXEC_STATUS="$(aws stepfunctions describe-execution \
      --execution-arn "${EXECUTION_ARN}" \
      "${REGION_ARG[@]}" \
      --query 'status' \
      --output text 2>/dev/null || echo UNKNOWN)"
    if [[ "${EXEC_STATUS}" != "RUNNING" ]]; then
      break
    fi
    sleep 2
  done

  if [[ "${EXEC_STATUS}" == "SUCCEEDED" ]]; then
    pass "execution finished with status SUCCEEDED"
  else
    fail "execution finished with status '${EXEC_STATUS}' (expected SUCCEEDED)"
  fi

  # Fetch the execution output JSON for the content assertions.
  EXEC_OUTPUT="$(aws stepfunctions describe-execution \
    --execution-arn "${EXECUTION_ARN}" \
    "${REGION_ARG[@]}" \
    --query 'output' \
    --output text 2>/dev/null || echo "")"

  if [[ -z "${EXEC_OUTPUT}" || "${EXEC_OUTPUT}" == "None" ]]; then
    fail "execution produced no output JSON to inspect"
  else
    # Assert the output preserves top-level "vcon": "0.0.1" and contains the
    # three analysis entries in order: transcript, entities, summary. The Merge
    # step writes them in that deterministic order and the terminal Seal step
    # carries the same "vcon" through on its output. Use python3 for robust JSON
    # parsing (avoids a hard jq dependency).
    if printf '%s' "${EXEC_OUTPUT}" | python3 -c '
import json
import sys

data = json.load(sys.stdin)

# Seal is the terminal state, returning
# {"vcon": <doc>, "step": "seal", "status": "ok", "redacted": ..., ...}, so one
# unwrap of "vcon" yields the enriched vCon document (Merge threaded it through).
vcon = data.get("vcon", data)

version = vcon.get("vcon")
if version != "0.0.1":
    print("version mismatch: %r" % (version,), file=sys.stderr)
    sys.exit(1)

types = [a.get("type") for a in vcon.get("analysis", [])]
expected = ["transcript", "entities", "summary"]
if types != expected:
    print("analysis order mismatch: %r != %r" % (types, expected), file=sys.stderr)
    sys.exit(1)

# The clinical Transcribe Medical transcript must be marked authoritative
# (the dual-transcript design: it sits alongside the live Deepgram transcript).
transcript = vcon["analysis"][0]
if transcript.get("authoritative") is not True:
    print("transcript analysis is not marked authoritative", file=sys.stderr)
    sys.exit(1)

# On a HEALTHY run, the entities and summary entries must NOT carry a
# "degraded" marker. That marker is only present when the Comprehend or Bedrock
# step failed open (model access denied / throttled / model not enabled), so
# its presence here would mean the canned end-to-end run silently lost analysis.
for a in vcon.get("analysis", []):
    if a.get("degraded"):
        print("analysis entry %r is degraded: %r" % (a.get("type"), a.get("degraded")), file=sys.stderr)
        sys.exit(1)
'; then
      pass 'execution output preserves "vcon": "0.0.1" and has analysis [transcript, entities, summary] in order (transcript authoritative, no degraded markers)'
    else
      fail 'execution output did not match: expected "vcon": "0.0.1" and analysis [transcript, entities, summary]'
    fi

    # -----------------------------------------------------------------------
    # SEAL STEP: the terminal Seal state signs (JWS), encrypts (JWE), and
    # redacts the enriched vCon, then PUTs three siblings under the run-key
    # prefix: vcon-signed.jws / vcon-full.jwe / vcon-redacted.json. Its output
    # is {vcon, step: 'seal', status: 'ok', redacted, seal_keys, seal_degraded}.
    #
    # First assert the Seal state RAN: the terminal output must be step 'seal'
    # and must carry the redacted pointer (redacted.redacted). Then inspect
    # seal_keys:
    #   * seal_keys NON-EMPTY -> a run with a real recording_key wrote the three
    #     siblings; head-object each key and PASS/FAIL per object.
    #   * seal_keys EMPTY     -> the canned no-recording input derives NO run-key
    #     prefix, so the Seal step writes NO siblings. That is expected here; we
    #     record it (not a failure) and note that confirming the physical S3
    #     siblings requires a run with a real recording_key.
    #
    # We do NOT fabricate a PASS for objects a canned run never writes. Keys are
    # printed one-per-line (signed_key, full_key, redacted_key) for head-object;
    # the first line is a STATUS token: SEAL_OK (ran + redacted pointer present),
    # SEAL_NO_POINTER (ran but no redacted pointer), or SEAL_NOT_RUN.
    # -----------------------------------------------------------------------
    SEAL_PARSED="$(printf '%s' "${EXEC_OUTPUT}" | python3 -c '
import json
import sys

data = json.load(sys.stdin)

if data.get("step") != "seal":
    print("SEAL_NOT_RUN")
    sys.exit(0)

redacted = data.get("redacted") or {}
pointer = isinstance(redacted, dict) and isinstance(redacted.get("redacted"), dict)
print("SEAL_OK" if pointer else "SEAL_NO_POINTER")

seal_keys = data.get("seal_keys") or {}
for name in ("signed_key", "full_key", "redacted_key"):
    key = seal_keys.get(name)
    if key:
        print(key)
' 2>/dev/null || echo "SEAL_PARSE_ERROR")"

    SEAL_STATUS="$(printf '%s\n' "${SEAL_PARSED}" | head -n 1)"
    # Lines 2+ are the seal object keys (empty for the canned no-recording run).
    SEAL_KEYS="$(printf '%s\n' "${SEAL_PARSED}" | tail -n +2 | sed '/^$/d')"

    case "${SEAL_STATUS}" in
    SEAL_OK)
      pass "Seal step ran and carried its redacted pointer (output step=seal, redacted present)"
      ;;
    SEAL_NO_POINTER)
      fail "Seal step ran but its output carried no redacted pointer (expected redacted.redacted)"
      ;;
    SEAL_NOT_RUN)
      fail "terminal execution output is not the Seal step (expected step=seal); Seal may not have run"
      ;;
    *)
      fail "could not parse the Seal step output JSON"
      ;;
    esac

    if [[ -n "${SEAL_KEYS}" ]]; then
      # A run with a real recording_key wrote the three siblings: head-object
      # each key under the bucket and PASS/FAIL per object.
      while IFS= read -r SEAL_KEY; do
        [[ -z "${SEAL_KEY}" ]] && continue
        if aws s3api head-object \
          --bucket "${BUCKET_NAME}" \
          --key "${SEAL_KEY}" \
          "${REGION_ARG[@]}" >/dev/null 2>&1; then
          pass "Seal sibling present in S3: ${SEAL_KEY}"
        else
          fail "Seal sibling missing in S3: ${SEAL_KEY}"
        fi
      done <<< "${SEAL_KEYS}"
    else
      # Expected for the canned run: no recording_key -> no run-key prefix ->
      # seal_keys empty -> no physical siblings written. Not a failure; record
      # it and note what a real-recording run is needed to confirm.
      pass "Seal wrote no S3 siblings on the canned run (no recording_key -> empty seal_keys, as expected)"
      echo "NOTE: confirming the physical Seal siblings (vcon-signed.jws / vcon-full.jwe / vcon-redacted.json) requires a run with a REAL recording_key (run the agent against a recording); the canned no-recording input writes none."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Final summary.
# ---------------------------------------------------------------------------
echo
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "All ${CHECKS} checks passed."
  exit 0
else
  echo "${FAILURES} of ${CHECKS} checks FAILED."
  exit 1
fi
