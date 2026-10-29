"""Self-checking tests for the committed offline fixture pair.

The demo backend's offline fallback (``BUCKET_NAME`` unset) serves a committed
full + redacted vCon pair from ``frontend/fixtures/`` so the viewer side is
demoable with no live call. Those readable JSON files are DECODED from sealed
JOSE artifacts (``vcon-full.jwe`` / ``vcon-redacted.jws``) produced by running
the authoritative seal core over a representative enriched vCon with a committed
throwaway demo key (see ``frontend/fixtures/generate_fixtures.py``).

These tests prove the committed artifacts are internally consistent and
regenerable: the full JWE decrypts + its inner JWS verifies with the committed
key and yields a vCon with NO top-level ``redacted`` pointer; the redacted JWS
verifies and yields a vCon whose top-level ``redacted`` pointer is main's REAL
shape; and ``compute_content_hash`` over the committed full JWE bytes equals the
pointer's ``content_hash``. They run fully offline and never import
fastapi / boto3 / livekit.

Guarded with ``pytest.importorskip('jwcrypto')`` so the suite SKIPS cleanly (not
fails collection) if the JOSE dep was unavailable at install time, mirroring
tests/test_sealing.py.
"""

import json
import os
import sys

import pytest

# Skip the whole module (with an explanatory reason) if jwcrypto is unavailable.
pytest.importorskip("jwcrypto", reason="jwcrypto not installed; crypto tests skipped")

# The authoritative sealing core lives in the packaged Seal Lambda under
# infrastructure/functions/seal/, which is not on the normal import path. Prepend
# that directory to sys.path so `from sealing import ...` resolves the same file
# the handler imports at AWS runtime (mirroring tests/test_sealing.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SEAL_PKG = os.path.join(_REPO_ROOT, "infrastructure", "functions", "seal")
if _SEAL_PKG not in sys.path:
    sys.path.insert(0, _SEAL_PKG)

from sealing import (  # noqa: E402  (import after importorskip + sys.path shim)
    VCON_JWS_CONTENT_TYPE,
    compute_content_hash,
    decrypt_vcon,
    load_jwk,
    public_jwk,
    verify_vcon,
)

_FIXTURES_DIR = os.path.join(_REPO_ROOT, "frontend", "fixtures")


def _read(name):
    with open(os.path.join(_FIXTURES_DIR, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def demo_key():
    """The committed throwaway demo key the fixtures were sealed with."""
    return load_jwk(_read("demo-signing-key.jwk"))


@pytest.fixture(scope="module")
def full_jwe():
    return _read("vcon-full.jwe")


@pytest.fixture(scope="module")
def redacted_jws():
    return _read("vcon-redacted.jws")


# ---------------------------------------------------------------------------
# The full JWE decrypts + inner JWS verifies -> full vCon, NO redacted pointer.
# ---------------------------------------------------------------------------


def test_full_jwe_decrypts_and_inner_jws_verifies(demo_key, full_jwe):
    """Decrypting the full JWE then verifying the inner JWS yields the full vCon.

    This is exactly the DOCTOR-view server path: the committed demo key unwraps
    the JWE to the signed JWS, which verifies with the key's public half.
    """
    signed_jws = decrypt_vcon(full_jwe, demo_key)
    full = verify_vcon(signed_jws, public_jwk(demo_key))

    assert full["vcon"] == "0.0.1"
    assert full["uuid"]
    # The FULL copy must NOT carry a top-level redacted pointer (only the
    # redacted copy points back to the full one in the real model).
    assert "redacted" not in full


def test_full_readable_json_matches_decoded_full(demo_key, full_jwe):
    """The committed vcon-full.json equals the decoded+verified full vCon.

    Keeps the readable fallback file honest: it is the exact decoded artifact,
    not a hand-authored copy that could drift from the sealed bytes.
    """
    decoded = verify_vcon(decrypt_vcon(full_jwe, demo_key), public_jwk(demo_key))
    committed = json.loads(_read("vcon-full.json"))
    assert committed == decoded


def test_full_has_numeric_dialog_start_for_audio_seek(demo_key, full_jwe):
    """The full vCon's speech dialog turns carry NUMERIC start + duration.

    The viewer audio-seek + medication->provenance click-through work in numeric
    seconds, so the fixture must not fall back to ISO-string starts.
    """
    full = verify_vcon(decrypt_vcon(full_jwe, demo_key), public_jwk(demo_key))
    text_turns = [t for t in full["dialog"] if t.get("type") == "text"]
    assert text_turns
    for turn in text_turns:
        assert isinstance(turn["start"], (int, float))
        assert isinstance(turn["duration"], (int, float))


def test_medication_entity_resolves_to_a_numeric_dialog_start(demo_key, full_jwe):
    """The MEDICATION entity's provenance resolves to a numeric dialog start.

    Mirrors the viewer's resolveEntityStartSeconds chain: entities entry ->
    dialog[0] turn index -> that turn's numeric ``start``.
    """
    full = verify_vcon(decrypt_vcon(full_jwe, demo_key), public_jwk(demo_key))
    entities = [a for a in full["analysis"] if a.get("type") == "entities"]
    assert entities
    entry = entities[0]
    categories = {e.get("category") for e in entry["body"]}
    assert "MEDICATION" in categories
    turn_index = entry["dialog"][0]
    start = full["dialog"][turn_index]["start"]
    assert isinstance(start, (int, float))


# ---------------------------------------------------------------------------
# The redacted JWS verifies -> redacted vCon with the REAL redacted pointer.
# ---------------------------------------------------------------------------


def test_redacted_jws_verifies_with_real_pointer_shape(
    demo_key, redacted_jws, full_jwe
):
    """The redacted JWS verifies and carries main's REAL redacted-pointer shape.

    The pointer links back to the full copy: uuid === full uuid,
    type === 'application/vcon+jws', url ends with 'vcon-full.jwe', and
    content_hash starts with 'sha512-'. This is the INVERSE of the stale
    {type:'redacted'} shape -- only the redacted copy points at the full one.
    """
    full = verify_vcon(decrypt_vcon(full_jwe, demo_key), public_jwk(demo_key))
    redacted = verify_vcon(redacted_jws, public_jwk(demo_key))

    pointer = redacted["redacted"]
    assert pointer["uuid"] == full["uuid"]
    assert pointer["type"] == VCON_JWS_CONTENT_TYPE == "application/vcon+jws"
    assert pointer["url"].endswith("vcon-full.jwe")
    assert pointer["content_hash"].startswith("sha512-")


def test_redacted_readable_json_matches_decoded_redacted(demo_key, redacted_jws):
    """The committed vcon-redacted.json equals the verified redacted vCon."""
    decoded = verify_vcon(redacted_jws, public_jwk(demo_key))
    committed = json.loads(_read("vcon-redacted.json"))
    assert committed == decoded


def test_redaction_masked_the_phone_number(demo_key, redacted_jws):
    """A PII-looking token (phone number) is masked in the redacted copy.

    Demonstrates redaction is visible: the phone number present in the full
    dialog body is replaced with the placeholder in the redacted copy.
    """
    redacted = verify_vcon(redacted_jws, public_jwk(demo_key))
    bodies = " ".join(
        t.get("body", "") for t in redacted["dialog"] if t.get("type") == "text"
    )
    assert "415-555-0137" not in bodies
    assert "[REDACTED]" in bodies


# ---------------------------------------------------------------------------
# Internal consistency: content_hash(full.jwe bytes) === pointer.content_hash.
# ---------------------------------------------------------------------------


def test_content_hash_of_full_jwe_matches_pointer(demo_key, redacted_jws, full_jwe):
    """compute_content_hash(the committed full.jwe) equals the pointer hash.

    Proves the committed full JWE and the committed redacted pointer are a
    consistent captured pair (the JWE carries a random nonce, so this only holds
    because both were produced once with the committed key).
    """
    redacted = verify_vcon(redacted_jws, public_jwk(demo_key))
    pointer_hash = redacted["redacted"]["content_hash"]
    assert compute_content_hash(full_jwe.encode("utf-8")) == pointer_hash
    # Hashing the str form is equivalent (compute_content_hash utf-8 encodes).
    assert compute_content_hash(full_jwe) == pointer_hash
