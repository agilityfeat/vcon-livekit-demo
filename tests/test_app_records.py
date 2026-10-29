"""Offline unit tests for the demo backend's pure logic (records.py).

Fully offline, mirroring tests/test_persistence.py (injected fakes) and
tests/test_sealing.py (seal-core sys.path shim + ``pytest.importorskip``):

* The S3 client is a FakeS3 returning canned ``list_objects_v2`` pages and
  ``get_object`` bodies that are REAL sealed artifacts -- built here by running
  the authoritative seal core (generate_demo_keypair -> sign -> encrypt ->
  redact) -- so the tests exercise the genuine decrypt + verify path.
* The LiveKit token signing seam is a fake; no ``livekit`` is imported.
* Nothing imports ``fastapi`` / ``boto3`` / ``livekit``.

The tests are meaningful: they fail if the backend logic is reverted (wrong
keys, wrong artifact names, missing redacted pointer, broken token claims), and
a wrong-key / tampered-ciphertext case asserts a raise.
"""

import json
import os
import sys

import pytest

# Skip the whole module cleanly if jwcrypto could not be installed.
pytest.importorskip("jwcrypto", reason="jwcrypto not installed; crypto tests skipped")

# The authoritative seal core lives in the packaged Seal Lambda; prepend its
# directory so ``import sealing`` resolves the same file (mirrors
# tests/test_sealing.py). records.py adds this shim itself too, but importing it
# here keeps the test helpers independent of import order.
_SEAL_PKG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "infrastructure",
    "functions",
    "seal",
)
if _SEAL_PKG not in sys.path:
    sys.path.insert(0, _SEAL_PKG)

from sealing import (  # noqa: E402
    build_redacted_reference,
    compute_content_hash,
    encrypt_vcon,
    generate_demo_keypair,
    public_jwk,
    redact_vcon,
    sign_vcon,
)

from vcon_agent.app import records as rec  # noqa: E402
from vcon_agent.vcon import VConBuilder  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------

BUCKET = "demo-bucket"
ROOM = "room-abc"
SESSION = "2024-01-01T00:00:00+00:00"
RUN_PREFIX = "{}/{}".format(ROOM, SESSION)


@pytest.fixture(scope="module")
def keypair():
    return generate_demo_keypair(size=2048)


def _enriched_vcon():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(0, "I have a headache, email jane@example.com.", start="0")
    builder.add_dialog_turn(1, "How long has that been going on?", start="5")
    builder.add_transcript_analysis("Patient reports a headache.", dialog_indices=[0, 1])
    builder.add_summary_analysis("Patient reports a headache.", dialog_indices=[0, 1])
    return builder.to_dict()


def _seal_artifacts(key):
    """Produce the three sibling artifacts exactly like the seal step.

    Returns (full_doc, signed_jws, full_jwe, redacted_jws, redacted_doc).
    """
    full_doc = _enriched_vcon()
    signed_jws = sign_vcon(full_doc, key)
    full_jwe = encrypt_vcon(signed_jws, public_jwk(key))
    full_url = "s3://{}/{}/{}".format(BUCKET, RUN_PREFIX, rec.FULL_OBJECT)
    content_hash = compute_content_hash(full_jwe)
    reference = build_redacted_reference(
        full_doc["uuid"], url=full_url, content_hash=content_hash
    )
    redacted_doc = redact_vcon(full_doc, reference)
    redacted_jws = sign_vcon(redacted_doc, key)
    return full_doc, signed_jws, full_jwe, redacted_jws, redacted_doc


class FakeS3Client:
    """Returns canned list_objects_v2 pages and get_object bodies (no boto3)."""

    def __init__(self, listing=None, objects=None):
        self._listing = listing or {}
        self._objects = objects or {}
        self.list_calls = []
        self.get_calls = []

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        return self._listing

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        key = kwargs["Key"]
        if key not in self._objects:
            raise KeyError("no such key: {}".format(key))
        return {"Body": self._objects[key]}


def _canned_listing(*run_prefixes):
    contents = []
    for prefix in run_prefixes:
        # Seal writes three siblings; list_records keys off the signed artifact.
        contents.append({"Key": "{}/{}".format(prefix, rec.SIGNED_OBJECT)})
        contents.append({"Key": "{}/{}".format(prefix, rec.FULL_OBJECT)})
        contents.append({"Key": "{}/{}".format(prefix, rec.REDACTED_OBJECT)})
    return {"Contents": contents}


def _fake_s3_with_artifacts(key):
    full_doc, signed_jws, full_jwe, redacted_jws, redacted_doc = _seal_artifacts(key)
    objects = {
        "{}/{}".format(RUN_PREFIX, rec.SIGNED_OBJECT): signed_jws.encode("utf-8"),
        "{}/{}".format(RUN_PREFIX, rec.FULL_OBJECT): full_jwe.encode("utf-8"),
        "{}/{}".format(RUN_PREFIX, rec.REDACTED_OBJECT): redacted_jws.encode("utf-8"),
    }
    s3 = FakeS3Client(listing=_canned_listing(RUN_PREFIX), objects=objects)
    return s3, full_doc, full_jwe, redacted_doc


# ---------------------------------------------------------------------------
# list_records: derive run prefixes / keys from canned S3 listings.
# ---------------------------------------------------------------------------


def test_list_records_derives_prefixes_and_keys():
    s3 = FakeS3Client(
        listing=_canned_listing("room-a/ts-1", "room-b/ts-2"),
    )
    descriptors = rec.list_records(s3, BUCKET)

    assert [d.run_prefix for d in descriptors] == ["room-a/ts-1", "room-b/ts-2"]
    first = descriptors[0]
    assert first.room == "room-a"
    assert first.session_start == "ts-1"
    assert first.signed_key == "room-a/ts-1/vcon-signed.jws"
    assert first.full_key == "room-a/ts-1/vcon-full.jwe"
    assert first.redacted_key == "room-a/ts-1/vcon-redacted.jws"
    # The list was scoped to the injected bucket.
    assert s3.list_calls[0]["Bucket"] == BUCKET


def test_list_records_ignores_unrelated_objects():
    listing = {
        "Contents": [
            {"Key": "room-a/ts-1/vcon-signed.jws"},
            {"Key": "room-a/ts-1/recording.ogg"},
            {"Key": "room-a/ts-1/vcon-skeleton.json"},
            {"Key": "stray-object.txt"},
        ]
    }
    s3 = FakeS3Client(listing=listing)
    descriptors = rec.list_records(s3, BUCKET)
    # Only the signed artifact yields a record; one run, de-duplicated.
    assert [d.run_prefix for d in descriptors] == ["room-a/ts-1"]


# ---------------------------------------------------------------------------
# load_full_record: real decrypt + verify round-trip, and failure cases.
# ---------------------------------------------------------------------------


def test_load_full_record_round_trips_doctor_view(keypair):
    s3, full_doc, _full_jwe, _redacted_doc = _fake_s3_with_artifacts(keypair)
    descriptor = rec._descriptor_for_prefix(RUN_PREFIX)

    recovered = rec.load_full_record(s3, keypair, BUCKET, descriptor.full_key)

    # Decrypting the JWE + verifying the JWS yields the EXACT full vCon, and the
    # full copy carries NO redacted pointer.
    assert recovered == full_doc
    assert "redacted" not in recovered
    # It read the full artifact by its exact key.
    assert s3.get_calls[0]["Key"] == descriptor.full_key


def test_load_full_record_fails_with_wrong_key(keypair):
    s3, _full_doc, _full_jwe, _redacted_doc = _fake_s3_with_artifacts(keypair)
    descriptor = rec._descriptor_for_prefix(RUN_PREFIX)
    wrong_key = generate_demo_keypair(size=2048)

    with pytest.raises(Exception):
        rec.load_full_record(s3, wrong_key, BUCKET, descriptor.full_key)


def test_decode_full_record_fails_on_tampered_ciphertext(keypair):
    _s3, _full_doc, full_jwe, _redacted_doc = _fake_s3_with_artifacts(keypair)
    # Corrupt the JWE ciphertext segment (compact serialization: 5 dot-parts).
    parts = full_jwe.split(".")
    assert len(parts) == 5
    ciphertext = parts[3]
    parts[3] = ("A" if ciphertext[:1] != "A" else "B") + ciphertext[1:]
    tampered = ".".join(parts)

    with pytest.raises(Exception):
        rec.decode_full_record(tampered, keypair)


# ---------------------------------------------------------------------------
# load_redacted_record: verify + carries the real redacted pointer.
# ---------------------------------------------------------------------------


def test_load_redacted_record_carries_real_pointer(keypair):
    s3, full_doc, _full_jwe, redacted_doc = _fake_s3_with_artifacts(keypair)
    descriptor = rec._descriptor_for_prefix(RUN_PREFIX)

    recovered = rec.load_redacted_record(
        s3, keypair, BUCKET, descriptor.redacted_key
    )

    assert recovered == redacted_doc
    pointer = recovered["redacted"]
    assert pointer["uuid"] == full_doc["uuid"]
    assert pointer["type"] == "application/vcon+jws"
    assert pointer["url"] == "s3://{}/{}/{}".format(
        BUCKET, RUN_PREFIX, rec.FULL_OBJECT
    )
    assert pointer["content_hash"].startswith("sha512-")
    # The seeded email PII is gone from the redacted copy.
    assert "jane@example.com" not in json.dumps(recovered)


def test_load_redacted_record_accepts_public_key(keypair):
    s3, _full_doc, _full_jwe, redacted_doc = _fake_s3_with_artifacts(keypair)
    descriptor = rec._descriptor_for_prefix(RUN_PREFIX)
    # Passing the PUBLIC half alone still verifies.
    recovered = rec.load_redacted_record(
        s3, public_jwk(keypair), BUCKET, descriptor.redacted_key
    )
    assert recovered == redacted_doc


def test_load_redacted_record_fails_with_wrong_key(keypair):
    s3, _full_doc, _full_jwe, _redacted_doc = _fake_s3_with_artifacts(keypair)
    descriptor = rec._descriptor_for_prefix(RUN_PREFIX)
    wrong_key = generate_demo_keypair(size=2048)
    with pytest.raises(Exception):
        rec.load_redacted_record(s3, wrong_key, BUCKET, descriptor.redacted_key)


# ---------------------------------------------------------------------------
# RecordsStore: S3 path vs. offline fixtures fallback (same output shape).
# ---------------------------------------------------------------------------


def test_records_store_s3_path(keypair):
    s3, full_doc, _full_jwe, redacted_doc = _fake_s3_with_artifacts(keypair)
    store = rec.RecordsStore(bucket=BUCKET, private_key=keypair, s3_client=s3)

    assert store.is_offline is False
    descriptors = store.list_records()
    assert [d.run_prefix for d in descriptors] == [RUN_PREFIX]
    assert store.full_record(RUN_PREFIX) == full_doc
    assert store.redacted_record(RUN_PREFIX) == redacted_doc


def test_records_store_offline_fixtures_fallback(keypair, tmp_path):
    # Author a fixtures pair (the decoded readable vCon JSON) on disk.
    full_doc, _signed, _jwe, _redacted_jws, redacted_doc = _seal_artifacts(keypair)
    (tmp_path / "vcon-full.json").write_text(json.dumps(full_doc), encoding="utf-8")
    (tmp_path / "vcon-redacted.json").write_text(
        json.dumps(redacted_doc), encoding="utf-8"
    )

    # No bucket -> offline. No S3 client is needed or touched.
    store = rec.RecordsStore(
        bucket=None, private_key=keypair, fixtures_dir=str(tmp_path)
    )
    assert store.is_offline is True

    descriptors = store.list_records()
    assert len(descriptors) == 1
    # Same descriptor SHAPE as the S3 path.
    d = descriptors[0].to_dict()
    assert set(d.keys()) == {
        "run_prefix",
        "room",
        "session_start",
        "signed_key",
        "full_key",
        "redacted_key",
    }
    assert store.full_record(descriptors[0].run_prefix) == full_doc
    red = store.redacted_record(descriptors[0].run_prefix)
    assert red == redacted_doc
    assert red["redacted"]["uuid"] == full_doc["uuid"]


def test_records_store_offline_empty_when_no_fixtures(keypair, tmp_path):
    # Graceful guard: fixtures not authored yet -> empty list, no raise.
    store = rec.RecordsStore(
        bucket=None, private_key=keypair, fixtures_dir=str(tmp_path)
    )
    assert store.list_records() == []
    with pytest.raises(FileNotFoundError):
        store.full_record("fixtures/sample-call")


def test_build_records_store_reads_env(keypair):
    # BUCKET_NAME set -> S3 store; unset -> offline store. Key is injected so
    # no env key material is needed.
    s3 = FakeS3Client()
    online = rec.build_records_store(
        env={"BUCKET_NAME": BUCKET}, s3_client=s3, private_key=keypair
    )
    assert online.is_offline is False
    assert online.bucket == BUCKET

    offline = rec.build_records_store(env={}, private_key=keypair)
    assert offline.is_offline is True


# ---------------------------------------------------------------------------
# Record shaping: numeric dialog start/duration for the audio seek.
# ---------------------------------------------------------------------------


def test_shape_record_coerces_numeric_start():
    doc = {
        "dialog": [
            {"type": "text", "party": 0, "body": "hi", "start": "5"},
            {"type": "text", "party": 1, "body": "yo", "start": 10, "duration": "3"},
        ]
    }
    shaped = rec.shape_record_for_frontend(doc)
    assert shaped["dialog"][0]["start"] == 5.0
    assert shaped["dialog"][1]["start"] == 10
    assert shaped["dialog"][1]["duration"] == 3.0
    # Original is not mutated (pure).
    assert doc["dialog"][0]["start"] == "5"


def test_shape_record_leaves_non_numeric_start_untouched():
    doc = {"dialog": [{"type": "text", "party": 0, "body": "x", "start": "2024-01-01T00:00:00+00:00"}]}
    shaped = rec.shape_record_for_frontend(doc)
    assert shaped["dialog"][0]["start"] == "2024-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# LiveKit patient token: pure claims + the mint seam (no livekit import).
# ---------------------------------------------------------------------------


def test_build_patient_token_claims():
    claims = rec.build_patient_token_claims("room-xyz", "patient-1", name="Jane")
    assert claims["identity"] == "patient-1"
    assert claims["name"] == "Jane"
    assert claims["ttl_seconds"] == rec.DEFAULT_TOKEN_TTL_SECONDS
    grant = claims["grant"]
    assert grant["room_join"] is True
    assert grant["room"] == "room-xyz"
    assert grant["can_publish"] is True
    assert grant["can_subscribe"] is True


def test_build_patient_token_claims_defaults_name_to_identity():
    claims = rec.build_patient_token_claims("r", "patient-2")
    assert claims["name"] == "patient-2"


def test_mint_patient_token_uses_injected_seam():
    captured = {}

    def fake_mint(claims):
        captured["claims"] = claims
        return "fake.jwt.token"

    token = rec.mint_patient_token(
        "room-xyz", "patient-1", name="Jane", mint_token=fake_mint
    )
    assert token == "fake.jwt.token"
    # The seam received exactly the claims the pure builder produced.
    assert captured["claims"] == rec.build_patient_token_claims(
        "room-xyz", "patient-1", name="Jane"
    )
    assert captured["claims"]["grant"]["room"] == "room-xyz"
