"""Offline unit tests for the LiveKit patient token module (livekit_tokens.py).

Fully offline: the pure claim builder requires no ``livekit`` import, and the
signing seam is a fake, so nothing imports ``livekit`` / ``fastapi`` / ``boto3``.
The tests are meaningful: they fail if the claim shape, the ttl default, or the
injectable mint seam is broken.
"""

from vcon_agent.app import livekit_tokens as tok


# ---------------------------------------------------------------------------
# LiveKit patient token: pure claims + the mint seam (no livekit import).
# ---------------------------------------------------------------------------


def test_build_patient_token_claims():
    claims = tok.build_patient_token_claims("room-xyz", "patient-1", name="Jane")
    assert claims["identity"] == "patient-1"
    assert claims["name"] == "Jane"
    assert claims["ttl_seconds"] == tok.DEFAULT_TOKEN_TTL_SECONDS
    grant = claims["grant"]
    assert grant["room_join"] is True
    assert grant["room"] == "room-xyz"
    assert grant["can_publish"] is True
    assert grant["can_subscribe"] is True


def test_build_patient_token_claims_defaults_name_to_identity():
    claims = tok.build_patient_token_claims("r", "patient-2")
    assert claims["name"] == "patient-2"


def test_mint_patient_token_uses_injected_seam():
    captured = {}

    def fake_mint(claims):
        captured["claims"] = claims
        return "fake.jwt.token"

    token = tok.mint_patient_token(
        "room-xyz", "patient-1", name="Jane", mint_token=fake_mint
    )
    assert token == "fake.jwt.token"
    # The seam received exactly the claims the pure builder produced.
    assert captured["claims"] == tok.build_patient_token_claims(
        "room-xyz", "patient-1", name="Jane"
    )
    assert captured["claims"]["grant"]["room"] == "room-xyz"
