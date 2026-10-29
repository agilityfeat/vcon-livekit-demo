"""Pure, framework-free business logic for the demo backend.

This module is the correctness-critical core of the demo backend and is kept
deliberately free of ``fastapi``, top-level ``boto3``, and ``livekit`` imports
so it imports and unit-tests fully offline (no network, no boto3 / livekit
installed), exactly like :mod:`vcon_agent.persistence`. Everything that talks
to AWS, LiveKit, or does server-side JOSE is reached through an INJECTED seam
or a lazy import inside a helper:

* the S3 client is injected (tests pass a fake that returns canned pages);
* ``boto3`` is imported lazily only inside :func:`make_s3_client`;
* LiveKit token SIGNING is a seam (default lazily imports ``livekit.api``);
* the JOSE verify / decrypt reuse the authoritative sealing core in
  ``infrastructure/functions/seal/sealing.py`` (imported via the same
  ``sys.path`` shim used by ``tests/test_sealing.py``) -- NO JOSE is
  reimplemented here.

The backend does ALL crypto server-side: it reads the sealed artifacts
(``vcon-full.jwe`` / ``vcon-redacted.jws``) and returns READABLE vCon JSON to
the frontend, so the browser never handles the demo signing key.

🩹 Shortcut: the browser never sees keys because the server decrypts/verifies
for it, but there is NO authentication and the "role" (doctor vs. analyst) is a
plain UI toggle, so anyone who can reach the backend can read either view.
→ Production: real authN/authZ (OIDC + per-role scopes), with the full
(decrypted) view gated to authorized clinicians and audit-logged.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Import the authoritative seal core (sign/verify/encrypt/decrypt/...).
# ---------------------------------------------------------------------------
# The seal core lives in the packaged Seal Lambda under
# infrastructure/functions/seal/, which is not on the normal import path.
# Prepend that directory to sys.path so ``import sealing`` resolves the SAME
# file the Lambda imports at AWS runtime and the pytest suite imports, mirroring
# tests/test_sealing.py. We reuse its JOSE functions and never reimplement JOSE.
#
# src/vcon_agent/app/records.py -> repo root is four parents up.
_SEAL_PKG = str(
    Path(__file__).resolve().parents[3]
    / "infrastructure"
    / "functions"
    / "seal"
)
if _SEAL_PKG not in sys.path:
    sys.path.insert(0, _SEAL_PKG)

import sealing  # noqa: E402  (import after the sys.path shim)

# Environment variable names (mirror persistence.py / agent.py).
ENV_BUCKET_NAME = "BUCKET_NAME"
ENV_AWS_REGION = "AWS_REGION"
ENV_PRIVATE_JWK = "VCON_SIGNING_PRIVATE_JWK"
ENV_LIVEKIT_URL = "LIVEKIT_URL"
ENV_LIVEKIT_API_KEY = "LIVEKIT_API_KEY"
ENV_LIVEKIT_API_SECRET = "LIVEKIT_API_SECRET"

# The seal step writes three sibling objects under each run-key prefix
# (<room>/<session_start_iso>/), matching seal_handler.py exactly.
SIGNED_OBJECT = "vcon-signed.jws"
FULL_OBJECT = "vcon-full.jwe"
REDACTED_OBJECT = "vcon-redacted.jws"

# Default local directory used by the offline fixtures fallback when
# BUCKET_NAME is unset. The full + redacted sample pair (produced by FEAT-003
# by running the real sealing core) lands here; the fallback guards gracefully
# when the directory does not exist yet.
#
# src/vcon_agent/app/records.py -> repo root is four parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURES_DIR = str(_REPO_ROOT / "frontend" / "fixtures")

# Default access-token lifetime for the patient join grant.
DEFAULT_TOKEN_TTL_SECONDS = 60 * 60  # one hour


# ---------------------------------------------------------------------------
# Record descriptor.
# ---------------------------------------------------------------------------


@dataclass
class RecordDescriptor:
    """Locates the seal artifacts for one call under its run-key prefix.

    ``run_prefix`` is ``<room>/<session_start_iso>`` and the three keys are the
    sibling objects the seal step PUTs under it. ``room`` / ``session_start`` are
    split out from the prefix for display in the records list.
    """

    run_prefix: str
    room: str
    session_start: str
    signed_key: str
    full_key: str
    redacted_key: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_prefix": self.run_prefix,
            "room": self.room,
            "session_start": self.session_start,
            "signed_key": self.signed_key,
            "full_key": self.full_key,
            "redacted_key": self.redacted_key,
        }


def _descriptor_for_prefix(run_prefix: str) -> RecordDescriptor:
    """Build a :class:`RecordDescriptor` from a ``<room>/<session_start>`` prefix.

    The run prefix is split once from the right so a room name that itself
    contains '/' is tolerated (room = everything before the last segment,
    session_start = the last segment), mirroring how the seal step derives the
    prefix from the recording key with a single ``rsplit('/', 1)``.
    """
    if "/" in run_prefix:
        room, session_start = run_prefix.rsplit("/", 1)
    else:
        room, session_start = run_prefix, ""
    return RecordDescriptor(
        run_prefix=run_prefix,
        room=room,
        session_start=session_start,
        signed_key="{}/{}".format(run_prefix, SIGNED_OBJECT),
        full_key="{}/{}".format(run_prefix, FULL_OBJECT),
        redacted_key="{}/{}".format(run_prefix, REDACTED_OBJECT),
    )


def _prefix_from_object_key(key: str) -> Optional[str]:
    """Derive the run-key prefix from a seal object key.

    Mirrors seal_handler._prefix_from_recording_key: strip the trailing object
    name (``vcon-signed.jws`` etc.) to get ``<room>/<session_start_iso>``.
    Returns ``None`` for a key with no '/'.
    """
    if isinstance(key, str) and "/" in key:
        return key.rsplit("/", 1)[0]
    return None


# ---------------------------------------------------------------------------
# Key loading.
# ---------------------------------------------------------------------------


def load_demo_private_key(env: Optional[Dict[str, str]] = None):
    """Load the demo signing key from the env var, or generate one in-memory.

    Reuses the seal core's env loader (``VCON_SIGNING_PRIVATE_JWK`` as JWK or
    PEM) with a generated-keypair fallback, matching the AWS-optional / env-
    optional style used across the repo. Returns the PRIVATE JWK; the public
    half is obtained with ``sealing.public_jwk``.

    🩹 Shortcut: the signing/decryption key is a self-signed demo keypair from
    the local ``.env`` (or generated on the fly), not a managed key, and the
    SAME key is used for JWS and JWE. → Production: AWS KMS / ACM PCA, key
    rotation, HSM-backed signing, and a separate JWE key with least privilege.
    """
    key = sealing.load_jwk_from_env(ENV_PRIVATE_JWK, env=env)
    if key is not None:
        return key
    return sealing.generate_demo_keypair(size=2048)


# ---------------------------------------------------------------------------
# S3 client factory (lazy boto3 import).
# ---------------------------------------------------------------------------


def make_s3_client(region: Optional[str] = None):
    """Create a real boto3 S3 client. Imported lazily so the fixtures fallback
    and tests with injected fakes never require boto3 at import time (mirrors
    persistence._make_boto3_client)."""
    import boto3  # noqa: WPS433 (intentional lazy import)

    if region:
        return boto3.client("s3", region_name=region)
    return boto3.client("s3")


# ---------------------------------------------------------------------------
# Listing records (S3 prefix scan).
# ---------------------------------------------------------------------------


def list_records(
    s3_client: Any,
    bucket: str,
    prefix: str = "",
) -> List[RecordDescriptor]:
    """List call records by scanning S3 for seal artifacts.

    Scans ``bucket`` (optionally under ``prefix``) for objects named
    ``vcon-signed.jws`` and derives each record's run-key prefix
    (``<room>/<session_start_iso>``) from the object key, exactly like the seal
    step derives the prefix from the recording key. One descriptor is returned
    per run, sorted by prefix for a stable list.

    ``s3_client`` is INJECTED (tests pass a fake returning canned
    ``list_objects_v2`` pages), so this stays pure and offline-testable.

    🩹 Shortcut: records are discovered by SCANNING the bucket's run-key
    prefixes for the signed artifact, not read from a records index / database;
    and listing is NOT paginated (a single ``list_objects_v2`` page). → Production:
    maintain a queryable records index (e.g. DynamoDB) written by the pipeline,
    with proper pagination, filtering, and access scoping, instead of a bucket
    scan on every request.
    """
    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    contents = resp.get("Contents", []) or []
    prefixes: List[str] = []
    seen = set()
    for obj in contents:
        key = obj.get("Key") if isinstance(obj, dict) else None
        if not isinstance(key, str) or not key.endswith("/" + SIGNED_OBJECT):
            continue
        run_prefix = _prefix_from_object_key(key)
        if run_prefix and run_prefix not in seen:
            seen.add(run_prefix)
            prefixes.append(run_prefix)
    prefixes.sort()
    return [_descriptor_for_prefix(p) for p in prefixes]


def _get_object_bytes(s3_client: Any, bucket: str, key: str) -> bytes:
    """Read an S3 object body as bytes via the injected client.

    Tolerates the real boto3 shape (``{'Body': <StreamingBody>}`` whose
    ``read()`` returns bytes) and a simple fake that returns ``{'Body': bytes}``.
    """
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    body = resp["Body"] if isinstance(resp, dict) else resp
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return body


# ---------------------------------------------------------------------------
# Decode + verify the sealed artifacts (server-side crypto).
# ---------------------------------------------------------------------------


def decode_full_record(full_jwe: str, private_key) -> Dict[str, Any]:
    """Decrypt the full JWE then verify the inner JWS (the DOCTOR view).

    Reuses the authoritative seal core: ``sealing.decrypt_vcon`` to unwrap the
    ``vcon-full.jwe`` into the signed JWS, then ``sealing.verify_vcon`` with the
    public half to return the full readable vCon dict. A wrong key or tampered
    ciphertext raises (loudly), so a bad artifact never silently yields a
    partial document. No JOSE is reimplemented here.
    """
    signed_jws = sealing.decrypt_vcon(full_jwe, private_key)
    return sealing.verify_vcon(signed_jws, sealing.public_jwk(private_key))


def decode_redacted_record(redacted_jws: str, key) -> Dict[str, Any]:
    """Verify the redacted JWS (the ANALYST view).

    Reuses ``sealing.verify_vcon`` to return the readable redacted vCon dict,
    which carries the real top-level ``redacted`` pointer back to the full copy.
    ``key`` may be the private JWK (its public half is used) or a public JWK.
    A wrong key / tampered payload raises.
    """
    public = _as_public(key)
    return sealing.verify_vcon(redacted_jws, public)


def _as_public(key):
    """Return the public JWK for ``key`` (idempotent for an already-public JWK).

    ``sealing.public_jwk`` re-exports the public half; calling it on a key that
    is already public is harmless, so callers can pass either half.
    """
    return sealing.public_jwk(key)


def load_full_record(
    s3_client: Any,
    private_key,
    bucket: str,
    full_key: str,
) -> Dict[str, Any]:
    """Read ``vcon-full.jwe`` from S3 and decode it to the full doctor view.

    Reads the JWE bytes via the injected client, then delegates to
    :func:`decode_full_record` (decrypt + verify using the seal core). Pure
    with respect to AWS: the client is injected.

    🩹 Shortcut: the server decrypts the full copy with the demo key on every
    request and returns plaintext JSON. → Production: KMS-backed decryption gated
    behind authZ, with the plaintext scoped to authorized clinicians and
    audit-logged.
    """
    full_jwe = _get_object_bytes(s3_client, bucket, full_key).decode("utf-8")
    return decode_full_record(full_jwe, private_key)


def load_redacted_record(
    s3_client: Any,
    key,
    bucket: str,
    redacted_key: str,
) -> Dict[str, Any]:
    """Read ``vcon-redacted.jws`` from S3 and verify it to the analyst view.

    Reads the JWS bytes via the injected client, then delegates to
    :func:`decode_redacted_record` (verify using the seal core). The returned
    redacted vCon carries the real ``redacted`` pointer.
    """
    redacted_jws = _get_object_bytes(s3_client, bucket, redacted_key).decode(
        "utf-8"
    )
    return decode_redacted_record(redacted_jws, key)


# ---------------------------------------------------------------------------
# Offline fixtures fallback (no BUCKET_NAME -> local files, never touch AWS).
# ---------------------------------------------------------------------------


@dataclass
class RecordsStore:
    """Resolves records from S3 or a local fixtures directory.

    Mirrors persistence.py's env-optional branch: when ``BUCKET_NAME`` is set
    the store reads from S3 (scan + get_object); when it is unset the store
    reads the local fixtures pair (``full`` + ``redacted``) so the viewer side
    is demoable with NO live call and NO AWS. Either way the OUTPUT shape is
    identical: the same record descriptors and the same readable-vCon dicts
    carrying the same real ``redacted`` pointer.
    """

    bucket: Optional[str]
    private_key: Any
    s3_client: Optional[Any] = None
    region: Optional[str] = None
    fixtures_dir: str = DEFAULT_FIXTURES_DIR
    # Fixture object names inside fixtures_dir (the readable full + redacted
    # vCon JSON produced by running the real seal core and decoding it once).
    _FULL_FIXTURE = "vcon-full.json"
    _REDACTED_FIXTURE = "vcon-redacted.json"
    _FIXTURE_PREFIX = "fixtures/sample-call"

    @property
    def is_offline(self) -> bool:
        """True when running from local fixtures (no bucket configured)."""
        return not self.bucket

    def _ensure_s3(self) -> Any:
        if self.s3_client is None:
            self.s3_client = make_s3_client(self.region)
        return self.s3_client

    # -- fixtures-dir helpers -------------------------------------------------

    def _fixture_path(self, name: str) -> Path:
        return Path(self.fixtures_dir) / name

    def _read_fixture_json(self, name: str) -> Optional[Dict[str, Any]]:
        path = self._fixture_path(name)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- unified API ----------------------------------------------------------

    def list_records(self) -> List[RecordDescriptor]:
        """Return the record descriptors from S3 or the local fixtures."""
        if self.is_offline:
            # One synthetic record if the fixtures pair is present; an empty
            # list (guarded gracefully) if the fixtures have not been authored
            # yet. The descriptor shape matches the S3 path exactly.
            if self._read_fixture_json(self._FULL_FIXTURE) is None:
                return []
            return [_descriptor_for_prefix(self._FIXTURE_PREFIX)]
        return list_records(self._ensure_s3(), self.bucket)

    def full_record(self, run_prefix: str) -> Dict[str, Any]:
        """Return the decoded full doctor-view vCon for ``run_prefix``."""
        if self.is_offline:
            doc = self._read_fixture_json(self._FULL_FIXTURE)
            if doc is None:
                raise FileNotFoundError(
                    "offline full fixture not found in {}".format(
                        self.fixtures_dir
                    )
                )
            return doc
        descriptor = _descriptor_for_prefix(run_prefix)
        return load_full_record(
            self._ensure_s3(), self.private_key, self.bucket, descriptor.full_key
        )

    def redacted_record(self, run_prefix: str) -> Dict[str, Any]:
        """Return the verified redacted analyst-view vCon for ``run_prefix``."""
        if self.is_offline:
            doc = self._read_fixture_json(self._REDACTED_FIXTURE)
            if doc is None:
                raise FileNotFoundError(
                    "offline redacted fixture not found in {}".format(
                        self.fixtures_dir
                    )
                )
            return doc
        descriptor = _descriptor_for_prefix(run_prefix)
        return load_redacted_record(
            self._ensure_s3(), self.private_key, self.bucket, descriptor.redacted_key
        )


def build_records_store(
    env: Optional[Dict[str, str]] = None,
    s3_client: Optional[Any] = None,
    private_key: Any = None,
) -> RecordsStore:
    """Construct a :class:`RecordsStore` from the environment.

    Reads ``BUCKET_NAME`` / ``AWS_REGION`` from ``env`` (defaults to
    ``os.environ``). When ``private_key`` is omitted the demo key is loaded via
    :func:`load_demo_private_key`. ``s3_client`` may be injected for tests.
    """
    if env is None:
        env = os.environ
    if private_key is None:
        private_key = load_demo_private_key(env=env)
    return RecordsStore(
        bucket=env.get(ENV_BUCKET_NAME) or None,
        private_key=private_key,
        s3_client=s3_client,
        region=env.get(ENV_AWS_REGION),
    )


# ---------------------------------------------------------------------------
# Record shaping for the frontend.
# ---------------------------------------------------------------------------


def shape_record_for_frontend(vcon_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a readable vCon dict for the frontend.

    The frontend's audio-seek + provenance click-through work in NUMERIC
    seconds, but ``VConBuilder`` defaults a dialog turn ``start`` to an ISO
    string. This helper coerces a numeric-looking ``start`` / ``duration`` on
    speech dialog turns to a float so the viewer can seek the audio element,
    and leaves non-numeric values (e.g. an ISO instant it cannot parse)
    untouched. It never drops fields and returns a new dict (pure).

    The real ``redacted`` pointer, when present, is passed through unchanged so
    the frontend can show the full <-> redacted linkage.
    """
    shaped = json.loads(json.dumps(vcon_doc))
    for turn in shaped.get("dialog", []):
        if not isinstance(turn, dict):
            continue
        for numeric_field in ("start", "duration"):
            if numeric_field in turn:
                turn[numeric_field] = _coerce_number(turn[numeric_field])
    return shaped


def _coerce_number(value: Any) -> Any:
    """Return ``value`` as a float if it looks numeric, else unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# LiveKit patient token: pure claim builder + injectable signing seam.
# ---------------------------------------------------------------------------


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
