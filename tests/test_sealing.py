"""Unit tests for the pure sign / encrypt / redact core.

Fully offline: the demo keypair is generated in-memory, redaction PHI spans are
canned, and there is no boto3 / network / live Comprehend Medical. These tests
mirror the offline style of tests/test_enrichment.py and tests/test_vcon.py and
are meaningful: they fail if the corresponding logic is reverted.

The crypto-dependent tests are guarded with ``pytest.importorskip('jwcrypto')``
so that, if PyPI was unreachable at install time, the suite SKIPS cleanly with an
explanatory reason rather than failing collection.
"""

import json
import os
import sys

import pytest

# Skip the whole module (with an explanatory reason) if jwcrypto could not be
# installed, instead of failing collection.
pytest.importorskip("jwcrypto", reason="jwcrypto not installed; crypto tests skipped")

# The authoritative sealing core lives in the packaged Seal Lambda under
# infrastructure/functions/seal/, which is not on the normal import path. Prepend
# that directory to sys.path so `from sealing import ...` resolves the same file
# the handler imports at AWS runtime (mirroring tests/test_seal_pipeline_wiring.py).
_SEAL_PKG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "infrastructure",
    "functions",
    "seal",
)
if _SEAL_PKG not in sys.path:
    sys.path.insert(0, _SEAL_PKG)

from sealing import (  # noqa: E402  (import after importorskip + sys.path shim)
    CONTENT_HASH_ALG,
    REDACTED_POINTER_TYPE,
    REDACTION_PLACEHOLDER,
    VCON_JWS_CONTENT_TYPE,
    build_redacted_reference,
    compute_content_hash,
    decrypt_vcon,
    encrypt_vcon,
    generate_demo_keypair,
    load_jwk,
    load_jwk_from_env,
    public_jwk,
    redact_vcon,
    sign_vcon,
    verify_vcon,
)
from vcon_agent.vcon import VConBuilder


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo_keypair():
    """A small RSA demo keypair generated once in-memory for the module."""
    return generate_demo_keypair(size=2048)


def _enriched_vcon(email="jane.doe@example.com", phone="555-123-4567", ssn="123-45-6789"):
    """Build a canned enriched vCon carrying seeded PII in dialog + analysis."""
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(
        0, f"I have a headache. Reach me at {email} or {phone}.", start="0"
    )
    builder.add_dialog_turn(1, "How long has that been going on?", start="5")
    builder.add_transcript_analysis(
        f"Patient SSN {ssn} reports a headache.", dialog_indices=[0, 1]
    )
    builder.add_entities_analysis(
        [
            {"category": "MEDICAL_CONDITION", "text": "headache", "score": 0.98},
            {"category": "MEDICATION", "text": f"call {phone}", "score": 0.5},
        ],
        dialog_index=0,
    )
    builder.add_summary_analysis(
        f"Patient emailed {email} about a headache.", dialog_indices=[0, 1]
    )
    return builder.to_dict()


# ---------------------------------------------------------------------------
# Demo keypair handling.
# ---------------------------------------------------------------------------


def test_public_jwk_strips_private_parameters(demo_keypair):
    pub = public_jwk(demo_keypair)
    exported = json.loads(pub.export())
    # RSA private parameter 'd' must not be present on the public JWK.
    assert "d" not in exported
    assert exported["kty"] == "RSA"


def test_load_jwk_round_trips_json_and_pem(demo_keypair):
    # JWK JSON round-trip.
    from_json = load_jwk(demo_keypair.export())
    assert from_json.thumbprint() == demo_keypair.thumbprint()
    # PEM (public) round-trip.
    pem = demo_keypair.export_to_pem(private_key=False, password=None)
    from_pem = load_jwk(pem.decode("utf-8"))
    assert from_pem.thumbprint() == public_jwk(demo_keypair).thumbprint()


def test_load_jwk_from_env_optional(demo_keypair):
    # Unset / blank var returns None (AWS-optional style fallback).
    assert load_jwk_from_env("VCON_SIGNING_PRIVATE_JWK", env={}) is None
    assert load_jwk_from_env("VCON_SIGNING_PRIVATE_JWK", env={"VCON_SIGNING_PRIVATE_JWK": "  "}) is None
    # Set var loads the key.
    env = {"VCON_SIGNING_PRIVATE_JWK": demo_keypair.export()}
    loaded = load_jwk_from_env("VCON_SIGNING_PRIVATE_JWK", env=env)
    assert loaded is not None
    assert loaded.thumbprint() == demo_keypair.thumbprint()


# ---------------------------------------------------------------------------
# JWS sign / verify.
# ---------------------------------------------------------------------------


def test_sign_verify_round_trip_returns_original(demo_keypair):
    doc = _enriched_vcon()
    jws_ser = sign_vcon(doc, demo_keypair)
    recovered = verify_vcon(jws_ser, public_jwk(demo_keypair))
    assert recovered == doc


def test_tampered_payload_fails_verification(demo_keypair):
    doc = _enriched_vcon()
    jws_ser = sign_vcon(doc, demo_keypair)
    # Tamper: flip the general JWS JSON payload to a different document.
    token = json.loads(jws_ser)
    import base64

    tampered_doc = dict(doc)
    tampered_doc["subject"] = "tampered subject"
    token["payload"] = (
        base64.urlsafe_b64encode(json.dumps(tampered_doc).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    with pytest.raises(Exception):
        verify_vcon(json.dumps(token), public_jwk(demo_keypair))


def test_verify_with_wrong_key_fails(demo_keypair):
    doc = _enriched_vcon()
    jws_ser = sign_vcon(doc, demo_keypair)
    other = generate_demo_keypair(size=2048)
    with pytest.raises(Exception):
        verify_vcon(jws_ser, public_jwk(other))


# ---------------------------------------------------------------------------
# JWE encrypt / decrypt.
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip_doc(demo_keypair):
    doc = _enriched_vcon()
    jwe_ser = encrypt_vcon(doc, public_jwk(demo_keypair))
    plaintext = decrypt_vcon(jwe_ser, demo_keypair)
    assert json.loads(plaintext) == doc


def test_encrypt_decrypt_round_trip_jws(demo_keypair):
    doc = _enriched_vcon()
    jws_ser = sign_vcon(doc, demo_keypair)
    jwe_ser = encrypt_vcon(jws_ser, public_jwk(demo_keypair))
    plaintext = decrypt_vcon(jwe_ser, demo_keypair)
    assert plaintext == jws_ser
    # The decrypted JWS still verifies back to the original doc.
    assert verify_vcon(plaintext, public_jwk(demo_keypair)) == doc


def test_decrypt_with_wrong_key_fails(demo_keypair):
    doc = _enriched_vcon()
    jwe_ser = encrypt_vcon(doc, public_jwk(demo_keypair))
    other = generate_demo_keypair(size=2048)
    with pytest.raises(Exception):
        decrypt_vcon(jwe_ser, other)


# ---------------------------------------------------------------------------
# Redaction.
# ---------------------------------------------------------------------------


def test_redaction_removes_seeded_pii_and_preserves_structure():
    email = "jane.doe@example.com"
    phone = "555-123-4567"
    ssn = "123-45-6789"
    doc = _enriched_vcon(email=email, phone=phone, ssn=ssn)
    ref = build_redacted_reference(doc["uuid"])
    redacted = redact_vcon(doc, ref)

    # Structure preserved: same top-level keys and same collection lengths.
    assert set(redacted.keys()) == set(doc.keys()) | {"redacted"}
    assert len(redacted["dialog"]) == len(doc["dialog"])
    assert len(redacted["analysis"]) == len(doc["analysis"])
    assert len(redacted["parties"]) == len(doc["parties"])
    # Party indices / roles untouched.
    assert redacted["parties"] == doc["parties"]
    # Dialog turns keep their party index and type.
    for r_turn, o_turn in zip(redacted["dialog"], doc["dialog"]):
        assert r_turn.get("party") == o_turn.get("party")
        assert r_turn.get("type") == o_turn.get("type")

    # Seeded PII is gone everywhere, replaced by the placeholder.
    blob = json.dumps(redacted)
    assert email not in blob
    assert phone not in blob
    assert ssn not in blob
    assert REDACTION_PLACEHOLDER in redacted["dialog"][0]["body"]

    # Non-PII content preserved.
    assert redacted["dialog"][1]["body"] == "How long has that been going on?"
    assert "headache" in redacted["dialog"][0]["body"]
    # Entities list length + non-PII entity text preserved.
    entities = redacted["analysis"][1]["body"]
    assert len(entities) == 2
    assert entities[0]["text"] == "headache"
    assert entities[0]["category"] == "MEDICAL_CONDITION"
    # The phone inside an entity text was redacted but the entity kept.
    assert REDACTION_PLACEHOLDER in entities[1]["text"]
    assert entities[1]["category"] == "MEDICATION"


def test_redacted_pointer_on_redacted_copy_only():
    doc = _enriched_vcon()
    ref = build_redacted_reference(doc["uuid"])
    redacted = redact_vcon(doc, ref)

    # The redacted copy carries the pointer back to the FULL uuid.
    assert redacted["redacted"]["uuid"] == doc["uuid"]
    assert redacted["redacted"]["type"] == REDACTED_POINTER_TYPE
    assert REDACTED_POINTER_TYPE == VCON_JWS_CONTENT_TYPE
    # The full (original) copy does NOT carry a redacted pointer.
    assert "redacted" not in doc


def test_build_redacted_reference_optional_url():
    ref = build_redacted_reference("abc-123", url="s3://bucket/full.jws")
    assert ref == {
        "uuid": "abc-123",
        "type": REDACTED_POINTER_TYPE,
        "url": "s3://bucket/full.jws",
    }
    # Bare reference is exactly {uuid, type}: url/content_hash omitted when not
    # supplied, so existing callers that pass neither are unaffected.
    assert build_redacted_reference("abc-123") == {
        "uuid": "abc-123",
        "type": REDACTED_POINTER_TYPE,
    }


def test_build_redacted_reference_carries_content_hash():
    ref = build_redacted_reference(
        "abc-123",
        url="s3://bucket/full.jwe",
        content_hash="sha512-deadbeef",
    )
    assert ref == {
        "uuid": "abc-123",
        "type": REDACTED_POINTER_TYPE,
        "url": "s3://bucket/full.jwe",
        "content_hash": "sha512-deadbeef",
    }
    # content_hash without url still works and url stays absent.
    only_hash = build_redacted_reference("abc-123", content_hash="sha512-deadbeef")
    assert only_hash == {
        "uuid": "abc-123",
        "type": REDACTED_POINTER_TYPE,
        "content_hash": "sha512-deadbeef",
    }
    # content_hash omitted when not supplied.
    assert "content_hash" not in build_redacted_reference("abc-123")


# ---------------------------------------------------------------------------
# content_hash of the externally referenced full copy.
# ---------------------------------------------------------------------------


def test_compute_content_hash_matches_independent_sha512():
    import base64
    import hashlib

    data = b"the exact vcon-full.jwe body bytes"
    expected = (
        "sha512-"
        + base64.urlsafe_b64encode(hashlib.sha512(data).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert compute_content_hash(data) == expected
    # The algorithm label constant is used verbatim as the prefix.
    assert compute_content_hash(data).startswith(CONTENT_HASH_ALG + "-")
    # No base64 '=' padding in the output.
    assert "=" not in compute_content_hash(data)


def test_compute_content_hash_str_and_bytes_identical():
    text = "sóme ünicode jwe body"
    assert compute_content_hash(text) == compute_content_hash(text.encode("utf-8"))


def test_redact_vcon_copies_url_and_content_hash_onto_pointer():
    doc = _enriched_vcon()
    ref = build_redacted_reference(
        doc["uuid"],
        url="s3://bucket/room/ts/vcon-full.jwe",
        content_hash="sha512-abc123",
    )
    redacted = redact_vcon(doc, ref)
    pointer = redacted["redacted"]
    assert pointer["uuid"] == doc["uuid"]
    assert pointer["type"] == REDACTED_POINTER_TYPE
    assert pointer["url"] == "s3://bucket/room/ts/vcon-full.jwe"
    assert pointer["content_hash"] == "sha512-abc123"


def test_redaction_applies_injected_canned_phi_spans():
    # No regex-matchable PII: a bare name only the DetectPHI mirror would flag.
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(0, "My name is Jane Smith and I feel unwell.", start="0")
    builder.add_summary_analysis("Jane Smith reports feeling unwell.", dialog_indices=[0])
    doc = builder.to_dict()

    # Canned spans keyed by exact text (the Comprehend-DetectPHI mirror seam).
    dialog_text = doc["dialog"][0]["body"]
    summary_text = doc["analysis"][0]["body"]
    spans = {
        dialog_text: [(dialog_text.index("Jane Smith"), dialog_text.index("Jane Smith") + len("Jane Smith"))],
        summary_text: [(0, len("Jane Smith"))],
    }

    ref = build_redacted_reference(doc["uuid"])
    redacted = redact_vcon(doc, ref, phi_spans=spans)

    assert "Jane Smith" not in json.dumps(redacted)
    assert redacted["dialog"][0]["body"] == f"My name is {REDACTION_PLACEHOLDER} and I feel unwell."
    assert redacted["analysis"][0]["body"] == f"{REDACTION_PLACEHOLDER} reports feeling unwell."


def test_redaction_accepts_detect_phi_callable():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(0, "Patient Robert Roe called today.", start="0")
    doc = builder.to_dict()

    def fake_detect_phi(text):
        idx = text.find("Robert Roe")
        return [(idx, idx + len("Robert Roe"))] if idx >= 0 else []

    ref = build_redacted_reference(doc["uuid"])
    redacted = redact_vcon(doc, ref, detect_phi=fake_detect_phi)
    assert "Robert Roe" not in redacted["dialog"][0]["body"]
    assert REDACTION_PLACEHOLDER in redacted["dialog"][0]["body"]


def test_redaction_masks_authoritative_transcript_turns_text():
    # The structured-transcript feature stores the authoritative Transcribe
    # Medical transcript as a `turns` list ({party, speaker, start, end, text})
    # on the transcript analysis entry, and the record view renders the clinical
    # transcript from turns[].text. redact_vcon MUST mask turns[].text with the
    # SAME PHI coverage as the bodies, or the de-identified analyst view leaks
    # PHI through the authoritative transcript.
    turn_text = "My name is Jane Smith and I have a headache."
    doc = {
        "uuid": "turns-uuid-1",
        "parties": [{"role": "patient"}, {"role": "provider"}],
        "dialog": [],
        "analysis": [
            {
                "type": "transcript",
                "dialog": [0],
                # Flat body (already covered) plus the authoritative turns.
                "body": "transcript body",
                "turns": [
                    {
                        "party": 0,
                        "speaker": "patient",
                        "start": 0.0,
                        "end": 3.5,
                        "text": turn_text,
                    },
                    {
                        "party": 1,
                        "speaker": "provider",
                        "start": 3.5,
                        "end": 5.0,
                        "text": "How long has that been going on?",
                    },
                ],
            }
        ],
    }

    # Canned span targeting the "Jane Smith" substring (the DetectPHI mirror seam):
    # no regex-matchable PII, so only the turns-aware masking can remove it.
    spans = {
        turn_text: [
            (turn_text.index("Jane Smith"), turn_text.index("Jane Smith") + len("Jane Smith"))
        ]
    }

    ref = build_redacted_reference(doc["uuid"])
    redacted = redact_vcon(doc, ref, phi_spans=spans)

    # The PHI substring is gone from the authoritative transcript turns.
    assert "Jane Smith" not in json.dumps(redacted)
    masked_turn = redacted["analysis"][0]["turns"][0]
    assert REDACTION_PLACEHOLDER in masked_turn["text"]
    assert masked_turn["text"] == f"My name is {REDACTION_PLACEHOLDER} and I have a headache."

    # Structure preserved: the turn keeps all its non-text keys unchanged.
    assert masked_turn["party"] == 0
    assert masked_turn["speaker"] == "patient"
    assert masked_turn["start"] == 0.0
    assert masked_turn["end"] == 3.5
    # The list length and other turns are preserved (non-PII text intact).
    assert len(redacted["analysis"][0]["turns"]) == 2
    assert redacted["analysis"][0]["turns"][1]["text"] == "How long has that been going on?"
