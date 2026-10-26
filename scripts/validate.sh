#!/usr/bin/env bash
#
# Validate the CloudFormation template with cfn-lint.
#
# cfn-lint runs fully offline (no AWS credentials needed) and is the
# authoritative local validation for this project. Install it with:
#
#   pip3 install --user cfn-lint      # or into a virtualenv
#
# Usage:
#   ./scripts/validate.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${ROOT_DIR}/template.yaml"

if ! command -v cfn-lint >/dev/null 2>&1; then
  echo "cfn-lint not found on PATH. Install it with: pip3 install --user cfn-lint" >&2
  exit 1
fi

echo "Linting ${TEMPLATE} ..."
cfn-lint "${TEMPLATE}"
echo "cfn-lint passed with no errors."
