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
    # Party indices / roles untouched; names are now HARD-REPLACED (a name is PHI
    # by nature, so the redactor masks parties[].name unconditionally per the
    # redaction audit — see test_redaction_masks_every_phi_capable_field).
    for r_party, o_party in zip(redacted["parties"], doc["parties"]):
        assert r_party["role"] == o_party["role"]
        assert r_party["name"] == REDACTION_PLACEHOLDER
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


# ---------------------------------------------------------------------------
# Deny-by-default coverage guard.
# ---------------------------------------------------------------------------


def test_redaction_masks_every_phi_capable_field():
    """Sentinel guard: NO PHI sentinel may survive ANYWHERE in the redacted doc.

    This is the durable coverage guard for the redaction audit. The de-identified
    (redacted / analyst) vCon must not carry PHI in ANY field it stores or
    renders, and redaction is DENY-BY-DEFAULT: every free-text string is masked
    unless its key is in sealing.STRUCTURAL_ALLOWLIST.

    The test places a DISTINCT, unmistakable sentinel token in EVERY PHI-capable
    free-text field enumerated by the audit — subject, both party names, a dialog
    turn body, the analysis flat body, an analysis turns[].text, an entity text,
    the consent script, and a recording dialog s3:// url — then serializes the
    WHOLE redacted document and asserts none of the sentinels survive. It also
    asserts the structural / kept fields are preserved verbatim.

    CONTRIBUTOR CONTRACT: if you add a NEW rendered or stored free-text field to
    the vCon model, you MUST add a sentinel for it HERE. Deny-by-default will
    already mask an unknown free-text key, but this test is what proves the
    coverage stays real as the model grows — do not let a field go untested.

    SCOPE: this test injects its own canned ``phi_spans`` for the span-masked
    fields, so it proves each PHI-capable field is REACHED and masked WHEN a span
    is supplied (plus the whole-value name hard-replace and recording url clear).
    It does NOT prove real clinical PHI would be detected: that depends on a live
    Comprehend Medical DetectPHI run and is validated only there, not offline.
    """
    # Distinct sentinels, one per PHI-capable field.
    S_SUBJECT = "SENTINEL_SUBJECT_PHI"
    S_PATIENT_NAME = "SENTINEL_PATIENT_NAME"
    S_AGENT_NAME = "SENTINEL_AGENT_NAME"
    S_DIALOG_BODY = "SENTINEL_DIALOG_BODY_PHI"
    S_FLAT_BODY = "SENTINEL_FLAT_BODY_PHI"
    S_TURN_TEXT = "SENTINEL_TURN_TEXT_PHI"
    S_ENTITY_TEXT = "SENTINEL_ENTITY_TEXT_PHI"
    S_CONSENT_SCRIPT = "SENTINEL_CONSENT_SCRIPT_PHI"
    S_RECORDING_URL = "SENTINEL_RECORDING_URL_PHI"
    ALL_SENTINELS = [
        S_SUBJECT,
        S_PATIENT_NAME,
        S_AGENT_NAME,
        S_DIALOG_BODY,
        S_FLAT_BODY,
        S_TURN_TEXT,
        S_ENTITY_TEXT,
        S_CONSENT_SCRIPT,
        S_RECORDING_URL,
    ]

    # Build via the real VConBuilder, then edit the few fields the builder has no
    # direct setter for (subject + party names happen to be constructor/default,
    # so set them explicitly on the dict).
    builder = VConBuilder(subject=S_SUBJECT)
    consent = builder.add_consent(
        granted=True, script=f"Consent for {S_CONSENT_SCRIPT} recording."
    )
    builder.add_dialog_turn(0, f"I take {S_DIALOG_BODY} daily.", start="0")
    builder.add_recording_dialog(
        url=f"s3://bucket/room/{S_RECORDING_URL}/recording.ogg",
        start="0",
        filename="recording.ogg",
    )
    builder.add_transcript_analysis(
        f"Patient mentioned {S_FLAT_BODY} during intake.",
        dialog_indices=[0],
        turns=[
            {
                "party": 0,
                "speaker": "patient",
                "start": 0.0,
                "end": 3.5,
                "text": f"I take {S_TURN_TEXT} every morning.",
            }
        ],
    )
    builder.add_entities_analysis(
        [{"category": "MEDICATION", "text": S_ENTITY_TEXT, "score": 0.9}],
        dialog_index=0,
    )
    doc = builder.to_dict()
    # Party names: the builder seeds generic placeholders; set PHI sentinels so
    # the hard-replace rule is exercised on real-looking names.
    doc["parties"][0]["name"] = S_PATIENT_NAME
    doc["parties"][1]["name"] = S_AGENT_NAME

    # Canned phi_spans (the DetectPHI mirror seam) for the span-masked free-text
    # fields. Names are HARD-REPLACED so they need no span; the recording url is
    # CLEARED wholesale so it needs none either.
    def _span_for(haystack, needle):
        idx = haystack.index(needle)
        return [(idx, idx + len(needle))]

    dialog_text = doc["dialog"][0]["body"]
    flat_body = doc["analysis"][0]["body"]
    turn_text = doc["analysis"][0]["turns"][0]["text"]
    entity_text = doc["analysis"][1]["body"][0]["text"]
    script_text = consent["script"]
    phi_spans = {
        S_SUBJECT: _span_for(S_SUBJECT, S_SUBJECT),
        dialog_text: _span_for(dialog_text, S_DIALOG_BODY),
        flat_body: _span_for(flat_body, S_FLAT_BODY),
        turn_text: _span_for(turn_text, S_TURN_TEXT),
        entity_text: _span_for(entity_text, S_ENTITY_TEXT),
        script_text: _span_for(script_text, S_CONSENT_SCRIPT),
    }

    # Use a clearly STRUCTURAL (non-PHI) pointer url so it is allowed to survive;
    # it lives in the top-level `redacted` subtree, which the walk skips entirely.
    pointer_url = "s3://bucket/room/ts/vcon-full.jwe"
    ref = build_redacted_reference(
        doc["uuid"], url=pointer_url, content_hash="sha512-structurallinkage"
    )

    redacted = redact_vcon(doc, ref, phi_spans=phi_spans)
    blob = json.dumps(redacted)

    # (1) NO PHI sentinel survives ANYWHERE in the serialized redacted doc.
    for sentinel in ALL_SENTINELS:
        assert sentinel not in blob, f"PHI sentinel leaked: {sentinel}"

    # (2) Structural / kept fields preserved.
    # Party roles kept; names hard-replaced.
    assert [p["role"] for p in redacted["parties"]] == ["patient", "agent"]
    assert redacted["parties"][0]["name"] == REDACTION_PLACEHOLDER
    assert redacted["parties"][1]["name"] == REDACTION_PLACEHOLDER
    # Document-level structural fields verbatim.
    assert redacted["uuid"] == doc["uuid"]
    assert redacted["vcon"] == doc["vcon"]
    assert redacted["created_at"] == doc["created_at"]
    # Dialog text turn: party/type/start preserved, body masked.
    text_turn = redacted["dialog"][0]
    assert text_turn["type"] == "text"
    assert text_turn["party"] == 0
    assert text_turn["start"] == "0"
    assert REDACTION_PLACEHOLDER in text_turn["body"]
    # Recording dialog: structural fields kept, url cleared.
    rec = redacted["dialog"][1]
    assert rec["type"] == "recording"
    assert rec["start"] == "0"
    assert rec["mimetype"] == "audio/ogg"
    assert rec["filename"] == "recording.ogg"
    assert rec["url"] == REDACTION_PLACEHOLDER
    # Transcript analysis: structural provenance kept, turns structural keys kept.
    transcript = redacted["analysis"][0]
    assert transcript["type"] == "transcript"
    assert transcript["vendor"] == "aws-transcribe-medical"
    assert transcript["authoritative"] is True
    assert transcript["dialog"] == [0]
    turn = transcript["turns"][0]
    assert turn["party"] == 0
    assert turn["speaker"] == "patient"
    assert turn["start"] == 0.0
    assert turn["end"] == 3.5
    assert REDACTION_PLACEHOLDER in turn["text"]
    # Entities analysis: category kept, entity text masked.
    entities = redacted["analysis"][1]
    assert entities["type"] == "entities"
    assert entities["body"][0]["category"] == "MEDICATION"
    assert REDACTION_PLACEHOLDER in entities["body"][0]["text"]
    # Consent attachment: type/party/granted/timestamp kept, script masked.
    att = redacted["attachments"][0]
    assert att["type"] == "consent"
    assert att["party"] == 0
    assert att["granted"] is True
    assert att["timestamp"] == consent["timestamp"]
    assert REDACTION_PLACEHOLDER in att["script"]
    # The top-level redacted pointer survives verbatim (structural linkage, NOT
    # masked): its structural url must NOT be cleared.
    assert redacted["redacted"]["uuid"] == doc["uuid"]
    assert redacted["redacted"]["type"] == REDACTED_POINTER_TYPE
    assert redacted["redacted"]["url"] == pointer_url
    assert redacted["redacted"]["content_hash"] == "sha512-structurallinkage"
