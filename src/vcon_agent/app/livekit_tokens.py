"""LiveKit patient access-token minting for the Start-call flow.

This module owns the one concern of producing the LiveKit access token the
patient's browser needs to JOIN the intake room at the START of the pipeline
(the record-review end lives in :mod:`vcon_agent.app.records`). It keeps the
same pure / seam discipline as that module so it imports and unit-tests fully
offline (no ``livekit`` installed):

* :func:`build_patient_token_claims` is a pure claim builder (NO ``livekit``
  import) describing the grant;
* :func:`default_mint_token` is the live signing seam that lazily imports
  ``livekit.api`` INSIDE the function and reads the LiveKit API credentials;
* :func:`mint_patient_token` orchestrates the pure builder and the injectable
  ``mint_token`` seam (defaulting to :func:`default_mint_token`), so tests can
  inject a fake and never require the ``livekit`` package.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from ..config import ENV_LIVEKIT_API_KEY, ENV_LIVEKIT_API_SECRET

# Default access-token lifetime for the patient join grant.
DEFAULT_TOKEN_TTL_SECONDS = 60 * 60  # one hour


def build_patient_token_claims(
    room: str,
    identity: str,
    name: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> Dict[str, Any]:
    """Build the LiveKit access-token claims for the patient join grant.

    Returns a plain dict describing the grant the patient needs to join the
    intake room from the browser: a room-join grant scoped to ``room`` plus the
    ``can_publish`` / ``can_subscribe`` the two-way voice leg requires. This is
    pure (NO ``livekit`` import) so it is unit-testable offline; the actual
    token signing happens in the :func:`default_mint_token` seam.

    🩹 Shortcut: the patient identity / room are chosen client-side and the
    grant is broad (publish + subscribe) with no participant limit or per-user
    identity binding. → Production: mint tokens server-side for an authenticated
    patient, bind the identity to a verified record, scope the grant tightly,
    and keep the TTL short.
    """
    return {
        "identity": identity,
        "name": name if name is not None else identity,
        "ttl_seconds": int(ttl_seconds),
        "grant": {
            "room_join": True,
            "room": room,
            "can_publish": True,
            "can_subscribe": True,
            "can_publish_data": True,
        },
    }


def default_mint_token(
    claims: Dict[str, Any],
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Sign a LiveKit access token from ``claims`` (the live signing seam).

    Lazily imports ``livekit.api`` (the server token SDK, provided by the
    ``[app]`` extra's ``livekit-api``) and signs the token with
    ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET``. Kept behind this seam -- and
    reached only from the FastAPI route -- so :func:`build_patient_token_claims`
    and the tests never require the ``livekit`` package, mirroring agent.py's
    ``_default_start_egress`` seam.

    This live path (real LiveKit creds, real signing) is NOT part of the
    automated suite, matching the repo's live-path convention.
    """
    if env is None:
        env = os.environ
    from livekit import api  # noqa: WPS433 (intentional lazy import)

    grant = claims.get("grant", {})
    token = (
        api.AccessToken(
            env.get(ENV_LIVEKIT_API_KEY),
            env.get(ENV_LIVEKIT_API_SECRET),
        )
        .with_identity(claims["identity"])
        .with_name(claims.get("name", claims["identity"]))
        .with_grants(
            api.VideoGrants(
                room_join=bool(grant.get("room_join", True)),
                room=grant.get("room"),
                can_publish=bool(grant.get("can_publish", True)),
                can_subscribe=bool(grant.get("can_subscribe", True)),
                can_publish_data=bool(grant.get("can_publish_data", True)),
            )
        )
    )
    ttl = claims.get("ttl_seconds")
    if ttl:
        from datetime import timedelta

        token = token.with_ttl(timedelta(seconds=int(ttl)))
    return token.to_jwt()


def mint_patient_token(
    room: str,
    identity: str,
    name: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    env: Optional[Dict[str, str]] = None,
    mint_token: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> str:
    """Build patient claims and sign them via the injectable ``mint_token`` seam.

    Orchestrates the pure claim builder and the signing seam: builds the claims
    with :func:`build_patient_token_claims`, then calls ``mint_token`` (defaults
    to :func:`default_mint_token`) to produce the signed JWT. Tests inject a
    fake ``mint_token`` and assert it received the expected claims, so no
    ``livekit`` import is required offline.
    """
    claims = build_patient_token_claims(
        room, identity, name=name, ttl_seconds=ttl_seconds
    )
    if mint_token is None:
        return default_mint_token(claims, env=env)
    return mint_token(claims)
