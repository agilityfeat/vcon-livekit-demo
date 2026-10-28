#!/usr/bin/env bash
# Regenerate the vendored copy of the pure seal core for the packaged Seal Lambda.
#
# Single source of truth: src/vcon_agent/sealing.py. The packaged Seal Lambda
# cannot import the repo package at AWS runtime, so a BYTE-IDENTICAL copy of that
# module is vendored next to seal_handler.py and shipped in the SAM build
# artifact. seal_handler.py imports the pure sign/encrypt/redact functions from
# this vendored copy instead of re-mirroring the crypto by hand.
#
# Run this whenever src/vcon_agent/sealing.py changes:
#
#     bash infrastructure/functions/seal/vendor.sh
#
# A test (tests/test_seal_pipeline_wiring.py::test_vendored_sealing_matches_source)
# asserts the vendored copy is byte-identical to the source so drift is caught in
# CI if this script is forgotten.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/../../../src/vcon_agent/sealing.py"
dst="$here/sealing.py"

cp "$src" "$dst"
echo "vendored $src -> $dst"
