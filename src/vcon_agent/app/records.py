"""Pure, framework-free business logic for the demo backend.

This module is the correctness-critical core of the demo backend and is kept
deliberately free of ``fastapi``, top-level ``boto3``, and ``livekit`` imports
so it imports and unit-tests fully offline (no network, no boto3 / livekit
installed), exactly like :mod:`vcon_agent.persistence`. Everything that talks
to AWS, LiveKit, or does server-side JOSE is reached through an INJECTED seam
or a lazy import inside a helper:

* the S3 client is injected (tests pass a fake that returns canned pages);
* ``boto3`` is imported lazily only inside :func:`make_s3_client`;
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
from typing import Any, Dict, List, Optional

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

from ..config import (  # noqa: E402  (import after the sys.path shim)
    ENV_AWS_REGION,
    ENV_BUCKET_NAME,
    ENV_PRIVATE_JWK,
)

# The seal step writes three sibling objects under each run-key prefix
# (<room>/<session_start_iso>/), matching seal_handler.py exactly.
SIGNED_OBJECT = "vcon-signed.jws"
FULL_OBJECT = "vcon-full.jwe"
REDACTED_OBJECT = "vcon-redacted.jws"

# The LiveKit egress writes the raw call recording as a sibling of the seal
# artifacts under the same run-key prefix, so the recording dialog object's
# s3:// url is <run_prefix>/recording.ogg. The viewer plays it via a short-lived
# presigned GET (see presigned_recording_url); it is never streamed in-browser
# straight from the s3:// reference.
RECORDING_OBJECT = "recording.ogg"

# How long a presigned recording GET URL stays valid. Short enough that a leaked
# link expires quickly, long enough to listen through a call.
RECORDING_URL_EXPIRES_SECONDS = 3600


# ---------------------------------------------------------------------------
# Typed errors.
# ---------------------------------------------------------------------------


class RecordKeyMismatchError(Exception):
    """A sealed artifact could not be decrypted/verified with the configured key.

    Raised when the server-side JOSE decrypt/verify of a sealed record fails,
    which in practice means the seal Lambda and the backend are NOT sharing the
    same ``VCON_SIGNING_PRIVATE_JWK`` (a shared-key mismatch), or the ciphertext
    was tampered with. The route layer maps this to a friendly HTTP 409 instead
    of leaking a raw 500 + JOSE stack trace. This is framework-free and carries
    no third-party imports so records.py stays collectable offline.
    """

# Default local directory used by the offline fixtures fallback when
# BUCKET_NAME is unset. The full + redacted sample pair (produced by FEAT-003
# by running the real sealing core) lands here; the fallback guards gracefully
# when the directory does not exist yet.
#
# src/vcon_agent/app/records.py -> repo root is four parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURES_DIR = str(_REPO_ROOT / "frontend" / "fixtures")


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


def presigned_recording_url(
    s3_client: Any,
    bucket: str,
    run_prefix: str,
    expires_in: int = RECORDING_URL_EXPIRES_SECONDS,
) -> str:
    """Return a short-lived presigned GET URL for a run's ``recording.ogg``.

    The recording object is a sibling of the seal artifacts under the run-key
    prefix (``<run_prefix>/recording.ogg``). The browser cannot play the raw
    ``s3://`` reference carried by the recording dialog object, so the viewer
    asks the backend for a presigned GET it CAN play.

    ``s3_client`` is INJECTED exactly like the listing / get helpers, so unit
    tests pass a fake whose ``generate_presigned_url`` returns a canned string
    and no boto3 is needed. The real boto3 client exposes the same method.

    🩹 Shortcut: the presigned URL is minted with NO authZ check, so anyone who
    can reach this endpoint gets the un-redacted audio; it also always targets
    ``recording.ogg`` (the pipeline's fixed object name). → Production: gate the
    presign behind per-role authZ (full audio to authorized clinicians only),
    audit-log the access, and derive the object key from the vCon rather than a
    constant.
    """
    key = "{}/{}".format(run_prefix, RECORDING_OBJECT)
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


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

    A JOSE decrypt/verify failure (wrong shared key or tampered ciphertext) is
    re-raised as :class:`RecordKeyMismatchError` with a user-actionable message
    so the route layer can return a friendly 409 instead of a raw 500. The
    jwcrypto exception types are imported LAZILY here so records.py stays free
    of any module-top jwcrypto import.
    """
    try:
        signed_jws = sealing.decrypt_vcon(full_jwe, private_key)
        return sealing.verify_vcon(signed_jws, sealing.public_jwk(private_key))
    except Exception as exc:  # noqa: BLE001 (narrowed to JOSE below)
        _raise_key_mismatch(exc)


def decode_redacted_record(redacted_jws: str, key) -> Dict[str, Any]:
    """Verify the redacted JWS (the ANALYST view).

    Reuses ``sealing.verify_vcon`` to return the readable redacted vCon dict,
    which carries the real top-level ``redacted`` pointer back to the full copy.
    ``key`` may be the private JWK (its public half is used) or a public JWK.
    A wrong key / tampered payload is re-raised as
    :class:`RecordKeyMismatchError` (the jwcrypto exception types are imported
    lazily; see :func:`decode_full_record`).
    """
    public = _as_public(key)
    try:
        return sealing.verify_vcon(redacted_jws, public)
    except Exception as exc:  # noqa: BLE001 (narrowed to JOSE below)
        _raise_key_mismatch(exc)


def _raise_key_mismatch(exc: Exception) -> None:
    """Map a JOSE decrypt/verify failure to :class:`RecordKeyMismatchError`.

    The specific jwcrypto exception types are imported LAZILY here so no
    jwcrypto import happens at module import time (records.py must stay
    collectable offline where jwcrypto may be absent). Non-JOSE failures are
    re-raised unchanged so unexpected bugs are not masked as a key mismatch.

    Raises :class:`RecordKeyMismatchError` with a user-actionable message and
    preserves the original as ``__cause__`` (never leaking the raw stack trace
    into the message text).
    """
    try:
        from jwcrypto.common import InvalidJWEData
        from jwcrypto.jws import InvalidJWSSignature

        jose_errors: tuple = (InvalidJWEData, InvalidJWSSignature)
    except Exception:  # noqa: BLE001 (jwcrypto absent -> treat any as mismatch)
        jose_errors = (Exception,)

    if not isinstance(exc, jose_errors):
        raise exc
    raise RecordKeyMismatchError(
        "This record was sealed with a different signing key. Ensure the seal "
        "Lambda and the backend share the same VCON_SIGNING_PRIVATE_JWK, then "
        "place a new call."
    ) from exc


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
            # 🩹 Shortcut: in offline/fixtures mode there is a SINGLE committed
            # sample, so ``run_prefix`` is intentionally ignored and that one
            # pair is returned for any prefix. (In production/S3 mode below,
            # ``run_prefix`` keys the real artifacts via ``_descriptor_for_prefix``.)
            # → Production: key fixtures by prefix before adding a second one so
            # a wrong URL can't silently resolve to the sample.
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
            # 🩹 Shortcut: single committed sample, so ``run_prefix`` is ignored
            # offline (see ``full_record`` above); production/S3 keys by prefix.
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

    def recording_url(self, run_prefix: str) -> Optional[str]:
        """Return a presigned GET URL for the run's recording, or ``None``.

        In S3 mode this mints a short-lived presigned GET for
        ``<run_prefix>/recording.ogg`` so the viewer can play the real call
        audio. In OFFLINE/fixtures mode there is no live S3 object (the sample
        pair carries only an ``s3://`` reference, no real recording), so this
        returns ``None`` and the frontend shows a clean "no audio in offline /
        demo mode" note rather than erroring.
        """
        if self.is_offline:
            return None
        return presigned_recording_url(
            self._ensure_s3(), self.bucket, run_prefix
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
