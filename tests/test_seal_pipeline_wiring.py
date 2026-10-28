"""Offline wiring tests for the inline Merge body + the packaged Seal handler.

The correctness-critical sign/encrypt/redact logic lives in the single
authoritative ``infrastructure/functions/seal/sealing.py`` module, imported
directly by both the Lambda handler and the unit tests. This module instead guards the
PIPELINE WIRING that the pure core cannot see: the Step Functions envelope must
thread ``recording_key`` from the analyze branches through Merge to Seal, because
Seal derives its S3 run-key prefix from ``event["recording_key"]``. A regression
where an intermediate step drops ``recording_key`` silently disables all three
S3 PUTs (vcon-signed.jws / vcon-full.jwe / vcon-redacted.jws) in a real run.

Merge is still an inline ``Code.ZipFile`` body in
``infrastructure/template.yaml`` (it imports only boto3 and cannot import the
repo package at AWS runtime), so the Merge half EXTRACTS the inline body and
execs it with injected fakes. Seal is no longer inline: it is a packaged
AWS::Serverless::Function under ``infrastructure/functions/seal/``, so the Seal
half IMPORTS the packaged ``seal_handler`` by file path and exercises the same
scenarios with the same injected fakes. The packaged handler creates its boto3
clients lazily inside ``handler()``, so monkeypatching ``boto3.client`` before
each call is enough to keep everything offline and deterministic: no live AWS, no
network, canned inputs. The tests are guarded with ``pytest.importorskip`` so
they skip cleanly if boto3 / jwcrypto were not installed, matching
tests/test_sealing.py.
"""

import importlib.util
import os
import re

import pytest

pytest.importorskip("boto3", reason="boto3 not installed; seal wiring tests skipped")
pytest.importorskip("jwcrypto", reason="jwcrypto not installed; seal wiring tests skipped")

import boto3  # noqa: E402

import sys  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_REPO_ROOT, "infrastructure", "template.yaml")
_SEAL_PKG = os.path.join(_REPO_ROOT, "infrastructure", "functions", "seal")

# The packaged seal dir is prepended to sys.path so `import sealing` resolves the
# same authoritative module the handler imports (matching tests/test_sealing.py).
if _SEAL_PKG not in sys.path:
    sys.path.insert(0, _SEAL_PKG)


def _extract_zipfile_body(function_name):
    """Pull one inline ``Code.ZipFile`` body for a named CFN resource.

    Parses the template as text (not YAML) so the block-scalar body is captured
    verbatim with its original indentation, then dedents it for exec.
    """
    with open(_TEMPLATE) as fh:
        lines = fh.readlines()

    # Find the resource block, then its `ZipFile: |` line, then the indented
    # block scalar that follows.
    in_resource = False
    resource_indent = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not in_resource:
            if stripped == "{}:".format(function_name) or stripped == "{}:".format(
                function_name
            ).strip():
                pass
            if re.match(r"^\s*{}:\s*$".format(re.escape(function_name)), line):
                in_resource = True
                resource_indent = indent
            i += 1
            continue
        # Left the resource block (dedented to a sibling resource).
        if stripped and indent <= resource_indent:
            break
        if stripped == "ZipFile: |":
            body_indent = indent + 2
            body_lines = []
            i += 1
            while i < n:
                bl = lines[i]
                if bl.strip() == "":
                    body_lines.append("")
                    i += 1
                    continue
                bl_indent = len(bl) - len(bl.lstrip())
                if bl_indent < body_indent:
                    break
                body_lines.append(bl[body_indent:].rstrip("\n"))
                i += 1
            return "\n".join(body_lines)
        i += 1
    raise AssertionError("ZipFile body not found for {}".format(function_name))


def _load_inline_handler(function_name, client_factory):
    """Exec an extracted inline body with boto3.client monkeypatched.

    ``client_factory`` maps a service name to a fake client. Returns the body's
    ``handler`` callable. Used for the inline Merge body.
    """
    body = _extract_zipfile_body(function_name)
    namespace = {}
    original = boto3.client
    boto3.client = client_factory
    try:
        exec(compile(body, "<{}>".format(function_name), "exec"), namespace)
    finally:
        boto3.client = original
    return namespace["handler"]


def _import_seal_module():
    """Import the packaged Seal handler module by file path.

    The packaged function lives under ``infrastructure/functions/seal/`` and is
    not on the normal import path. Its own directory is prepended to sys.path so
    its ``from sealing import ...`` (the packaged sealing core) resolves, matching
    how `sam build` lays the package out at runtime.
    """
    import sys

    if _SEAL_PKG not in sys.path:
        sys.path.insert(0, _SEAL_PKG)
    spec = importlib.util.spec_from_file_location(
        "seal_handler", os.path.join(_SEAL_PKG, "seal_handler.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _packaged_seal_handler(client_factory):
    """Return a callable that runs the packaged Seal handler with fake clients.

    The packaged handler creates its boto3 clients lazily inside ``handler()``,
    so ``boto3.client`` is monkeypatched around each invocation.
    """
    module = _import_seal_module()

    def _run(event, context):
        original = boto3.client
        boto3.client = client_factory
        try:
            return module.handler(event, context)
        finally:
            boto3.client = original

    return _run


class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}


class _FakeCM:
    """Comprehend Medical fake: DetectPHI returns no entities (regex-only)."""

    def detect_phi(self, Text):  # noqa: N803 (boto3 kwarg name)
        return {"Entities": []}


def _merge_handler():
    return _load_inline_handler("MergeFunction", lambda service: None)


def _seal_handler(fake_s3, fake_cm):
    def factory(service):
        if service == "s3":
            return fake_s3
        if service == "comprehendmedical":
            return fake_cm
        raise AssertionError("unexpected boto3 client: {}".format(service))

    # Seal reads VCON_SIGNING_PRIVATE_JWK from the env; leave it empty so a demo
    # key is generated in-memory.
    os.environ.pop("VCON_SIGNING_PRIVATE_JWK", None)
    os.environ["BUCKET_NAME"] = "test-bucket"
    return _packaged_seal_handler(factory)


def _vcon():
    return {
        "vcon": "0.0.1",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "parties": [{"role": "patient"}, {"role": "agent"}],
        "dialog": [
            {"type": "text", "party": 0, "body": "I have a headache."},
        ],
        "analysis": [],
    }


def _branches(recording_key):
    """The Parallel state output: a list of the Comprehend + Bedrock envelopes,
    each carrying recording_key (as the fixed inline bodies now emit)."""
    vcon = _vcon()
    return [
        {
            "vcon": vcon,
            "step": "comprehend",
            "recording_key": recording_key,
            "entities": [],
            "entities_dialog_index": 0,
            "transcript_text": "I have a headache.",
            "entities_degraded": None,
        },
        {
            "vcon": vcon,
            "step": "bedrock",
            "recording_key": recording_key,
            "summary_text": "Patient reports a headache.",
            "summary_dialog_indices": [0],
            "summary_degraded": None,
        },
    ]


def test_merge_reemits_recording_key():
    """Merge must forward recording_key so Seal can locate the S3 prefix."""
    handler = _merge_handler()
    out = handler(_branches("room/sess/ts/recording.ogg"), None)
    assert out["step"] == "merge"
    assert out["recording_key"] == "room/sess/ts/recording.ogg"


def test_merge_to_seal_threads_recording_key_and_writes_artifacts():
    """End-to-end over Merge -> Seal: the three seal artifacts are PUT under the
    run-key prefix derived from the threaded recording_key. This is the exact
    regression where a dropped recording_key silently disabled every PUT."""
    merge = _merge_handler()
    merged = merge(_branches("room/sess/ts/recording.ogg"), None)

    fake_s3 = _FakeS3()
    seal = _seal_handler(fake_s3, _FakeCM())
    sealed = seal(merged, None)

    assert sealed["step"] == "seal"
    put_keys = {p["Key"] for p in fake_s3.puts}
    assert put_keys == {
        "room/sess/ts/vcon-signed.jws",
        "room/sess/ts/vcon-full.jwe",
        "room/sess/ts/vcon-redacted.jws",
    }
    assert sealed["seal_keys"]["signed_key"] == "room/sess/ts/vcon-signed.jws"
    assert sealed["seal_keys"]["redacted_key"].endswith("vcon-redacted.jws")


def test_seal_redacted_url_set_independent_of_put():
    """The redacted pointer url must locate the full JWE as soon as the prefix is
    known, even if the PUTs fail (issue 3)."""

    class _FailingS3:
        def put_object(self, **kwargs):
            raise RuntimeError("boom")

    merge = _merge_handler()
    merged = merge(_branches("room/sess/ts/recording.ogg"), None)
    seal = _seal_handler(_FailingS3(), _FakeCM())
    sealed = seal(merged, None)

    # PUTs failed, but the redacted pointer still carries the full-copy url.
    assert sealed["redacted"]["redacted"]["url"] == (
        "s3://test-bucket/room/sess/ts/vcon-full.jwe"
    )
    # content_hash is populated on the reference before the PUT try/except too,
    # so it must survive a failed PUT. Locks in that ordering so a future reorder
    # that moved hashing into the try block would be caught.
    content_hash = sealed["redacted"]["redacted"]["content_hash"]
    assert content_hash and content_hash.startswith("sha512-")
    assert "s3 put_object failed" in (sealed["seal_degraded"] or "")


def test_seal_detect_phi_fail_open_per_field():
    """A per-field DetectPHI failure must fail open (regex-only) and set
    seal_degraded rather than crashing the step (issue 2)."""

    class _ThrowingCM:
        def detect_phi(self, Text):  # noqa: N803
            raise RuntimeError("throttled")

    merge = _merge_handler()
    merged = merge(_branches("room/sess/ts/recording.ogg"), None)
    fake_s3 = _FakeS3()
    seal = _seal_handler(fake_s3, _ThrowingCM())
    # Must not raise even though every DetectPHI call throws.
    sealed = seal(merged, None)

    assert sealed["step"] == "seal"
    assert "detect_phi failed" in (sealed["seal_degraded"] or "")
    # Regex-only redaction still produced the sibling and PUT it.
    assert any(p["Key"].endswith("vcon-redacted.jws") for p in fake_s3.puts)


def _seal_handler_with_key(fake_s3, fake_cm, private_jwk_json):
    """Like _seal_handler but pins VCON_SIGNING_PRIVATE_JWK to a known key.

    Exporting a demo key into the env var lets the test verify the signed
    redacted sibling with that key's public half instead of a key the handler
    generated in-memory and never returned.
    """

    def factory(service):
        if service == "s3":
            return fake_s3
        if service == "comprehendmedical":
            return fake_cm
        raise AssertionError("unexpected boto3 client: {}".format(service))

    os.environ["VCON_SIGNING_PRIVATE_JWK"] = private_jwk_json
    os.environ["BUCKET_NAME"] = "test-bucket"
    return _packaged_seal_handler(factory)


def _redacted_put_body(fake_s3):
    """Return (kwargs) of the vcon-redacted.jws PUT captured by the FakeS3."""
    for put in fake_s3.puts:
        if put["Key"].endswith("vcon-redacted.jws"):
            return put
    raise AssertionError("no vcon-redacted.jws PUT captured")


def _full_put_body(fake_s3):
    """Return the Body bytes of the vcon-full.jwe PUT captured by the FakeS3."""
    for put in fake_s3.puts:
        if put["Key"].endswith("vcon-full.jwe"):
            return put["Body"]
    raise AssertionError("no vcon-full.jwe PUT captured")


def test_seal_redacted_sibling_is_signed_jws_with_populated_pointer():
    """The redacted sibling PUT body must be a JWS that verifies with the demo
    public key to the redacted doc whose top-level `redacted` pointer carries
    uuid, type, url, AND content_hash (spec 4.1.6: the redactor SHOULD sign the
    redacted vCon, and content_hash MUST be present for the url reference)."""
    import sealing

    keypair = sealing.generate_demo_keypair(size=2048)
    private_jwk_json = keypair.export()

    merge = _merge_handler()
    merged = merge(_branches("room/sess/ts/recording.ogg"), None)
    fake_s3 = _FakeS3()
    try:
        seal = _seal_handler_with_key(fake_s3, _FakeCM(), private_jwk_json)
        sealed = seal(merged, None)
    finally:
        os.environ.pop("VCON_SIGNING_PRIVATE_JWK", None)

    assert sealed["step"] == "seal"
    put = _redacted_put_body(fake_s3)
    # ContentType is the JWS media type, not application/json.
    assert put["ContentType"] == "application/vcon+jws"

    jws_body = put["Body"].decode("utf-8")
    # The body must VERIFY with the demo public key (fails if it is not a real
    # signature over this doc or was reverted to bare JSON).
    recovered = sealing.verify_vcon(jws_body, sealing.public_jwk(keypair))
    pointer = recovered["redacted"]
    assert pointer["uuid"] == merged["vcon"]["uuid"]
    assert pointer["type"] == "application/vcon+jws"
    assert pointer["url"] == "s3://test-bucket/room/sess/ts/vcon-full.jwe"
    assert "content_hash" in pointer and pointer["content_hash"]


def test_seal_pointer_content_hash_matches_full_jwe_bytes():
    """The content_hash in the signed redacted pointer must equal
    compute_content_hash of the EXACT vcon-full.jwe bytes that were PUT."""
    import sealing

    keypair = sealing.generate_demo_keypair(size=2048)
    private_jwk_json = keypair.export()

    merge = _merge_handler()
    merged = merge(_branches("room/sess/ts/recording.ogg"), None)
    fake_s3 = _FakeS3()
    try:
        seal = _seal_handler_with_key(fake_s3, _FakeCM(), private_jwk_json)
        sealed = seal(merged, None)
    finally:
        os.environ.pop("VCON_SIGNING_PRIVATE_JWK", None)

    full_body = _full_put_body(fake_s3)
    expected = sealing.compute_content_hash(full_body)

    put = _redacted_put_body(fake_s3)
    recovered = sealing.verify_vcon(
        put["Body"].decode("utf-8"), sealing.public_jwk(keypair)
    )
    assert recovered["redacted"]["content_hash"] == expected
    # The returned redacted document dict carries the same hash.
    assert sealed["redacted"]["redacted"]["content_hash"] == expected
