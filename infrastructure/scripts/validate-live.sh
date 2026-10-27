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
# each suffix is present. These are the seven real pipeline steps (poll S3 ->
# load skeleton -> start/get Transcribe Medical -> Comprehend + Bedrock parallel
# -> merge), so the suffix list matches the states defined in template.yaml.
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
  merge-step; do
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
    # step writes them in that deterministic order. Use python3 for robust JSON
    # parsing (avoids a hard jq dependency).
    if printf '%s' "${EXEC_OUTPUT}" | python3 -c '
import json
import sys

data = json.load(sys.stdin)

# The Merge step returns {"vcon": <doc>, "step": "merge", "status": "ok"}, so
# one unwrap yields the enriched vCon document.
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
