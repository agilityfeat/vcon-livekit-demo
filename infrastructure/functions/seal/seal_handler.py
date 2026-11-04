"""Packaged Seal Lambda handler: sign (JWS), encrypt (JWE), redact, PUT to S3.

This is the standalone, container-built Seal pipeline step. Unlike the other
seven pipeline Lambdas (which are inline ``Code.ZipFile`` bodies that mirror the
pure cores by hand because inline code cannot bundle third-party deps), the Seal
step is packaged with AWS SAM (``sam build --use-container``) so it can ship the
``jwcrypto`` dependency AND import the authoritative sign/encrypt/redact logic
directly instead of re-mirroring it.

The crypto core lives right here in the package as ``sealing.py``, the single
authoritative module that both this Lambda and the pytest suite import. It is
shipped in the SAM build artifact so it is present at AWS runtime; there is no
separate source copy or vendoring/regeneration step.

What stays HERE (AWS-only glue that is intentionally NOT in the pure core):
  * the boto3 S3 and Comprehend Medical clients;
  * the fail-open per-field DetectPHI seam fed into ``redact_vcon``;
  * the run-key prefix helper and the three S3 PUTs
    (vcon-signed.jws / vcon-full.jwe / vcon-redacted.jws);
  * populating the redacted pointer (url + content_hash of the full JWE) as soon
    as the prefix is known, signing the FINAL redacted document over that
    populated pointer, and the fail-open PUT behavior.

The returned dict shape is identical to the previous inline body:
``{vcon, step: 'seal', status: 'ok', redacted, seal_keys, seal_degraded}``.
"""

import json
import logging
import os

import boto3

from sealing import (
    build_redacted_reference,
    compute_content_hash,
    encrypt_vcon,
    generate_demo_keypair,
    load_jwk_from_env,
    public_jwk,
    redact_vcon,
    sign_vcon,
    VCON_JWS_CONTENT_TYPE,
)

logger = logging.getLogger(__name__)


def _load_or_generate_key():
    """Load the demo signing key from the env var, or generate one in-memory.

    Reuses the pure core's env loader (``VCON_SIGNING_PRIVATE_JWK`` as JWK or
    PEM) with a fallback to a generated 2048-bit RSA demo keypair, matching the
    AWS-optional style used elsewhere.

    Shortcut: self-signed demo keypair from an env var / generated locally, not a
    managed key. Production: AWS KMS / ACM PCA, key rotation, HSM-backed signing;
    never ship keys with the app.
    """
    key = load_jwk_from_env("VCON_SIGNING_PRIVATE_JWK")
    if key is not None:
        return key
    logger.warning(
        "VCON_SIGNING_PRIVATE_JWK not set: generating an EPHEMERAL in-memory "
        "signing key; records sealed this run CANNOT be decrypted or verified "
        "by the backend - set the same VCON_SIGNING_PRIVATE_JWK on this Lambda "
        "(CloudFormation parameter VconSigningPrivateJwk) and in the backend "
        ".env."
    )
    return generate_demo_keypair(size=2048)


def _make_detect_phi_spans(cm_client, degraded_sink):
    """Build the fail-open DetectPHI seam fed into ``redact_vcon``.

    Mirrors the AWS Comprehend Medical DetectPHI pass: returns
    (BeginOffset, EndOffset) spans for a piece of text. The pure core in
    ``sealing.py`` never calls AWS; this callable supplies the real spans while
    the regex pass always runs too.

    Fail-open for demo-runnability: the per-field DetectPHI call is wrapped so a
    throttle / 5xx / model-access failure on ANY field degrades to regex-only
    redaction rather than failing the whole Seal step, matching how the
    Comprehend / Bedrock steps wrap their real call. The first failure is
    recorded in ``degraded_sink`` so an operator can tell regex-only redaction
    apart from a clean pass. Production: a Catch to a visible alarm state, retries
    with backoff, and a hard failure path surfaced to operators.
    """

    def _detect_phi_spans(text):
        try:
            resp = cm_client.detect_phi(Text=text)
        except Exception as exc:
            if not degraded_sink:
                degraded_sink.append(
                    "comprehend-medical detect_phi failed: {}".format(
                        type(exc).__name__
                    )
                )
            return []
        spans = []
        for ent in resp.get("Entities", []) or []:
            begin = ent.get("BeginOffset")
            end = ent.get("EndOffset")
            if begin is not None and end is not None:
                spans.append((begin, end))
        return spans

    return _detect_phi_spans


def _prefix_from_recording_key(recording_key):
    """Derive the run-key prefix from the recording object key.

    The recording object and vcon-skeleton.json share a run prefix:
    ``<room_name>/<session_start_iso>/<file>``. Strip the trailing file name
    so the sibling seal objects land next to vcon-skeleton.json.
    """
    if isinstance(recording_key, str) and "/" in recording_key:
        return recording_key.rsplit("/", 1)[0]
    return None


def handler(event, context):
    vcon = event.get("vcon", event)
    bucket = os.environ.get("BUCKET_NAME", "")

    # Clients are created inside the handler (not at import) so the offline
    # wiring tests can monkeypatch boto3.client without an AWS session.
    s3 = boto3.client("s3")
    cm = boto3.client("comprehendmedical")

    # Sign the enriched vCon (JWS), then encrypt the SIGNED full copy (JWE). The
    # demo keypair is loaded from the env var or generated.
    private_key = _load_or_generate_key()
    pub_key = public_jwk(private_key)
    signed_jws = sign_vcon(vcon, private_key)
    full_jwe = encrypt_vcon(signed_jws, pub_key)

    # Build the redacted sibling. The Comprehend Medical DetectPHI pass over the
    # known text fields is the detect_phi seam; regex spans are always applied
    # too. The per-field call is wrapped (fail-open): the first failure degrades
    # to regex-only and records a marker in `degraded_spans`, so no single field
    # failure can crash the step.
    degraded_spans = []
    detect_phi = _make_detect_phi_spans(cm, degraded_spans)

    full_uuid = vcon.get("uuid")

    # PUT the three sibling objects next to vcon-skeleton.json under the run-key
    # prefix. The redacted sibling's `redacted` pointer carries the full JWE
    # object url AND content_hash, both populated BEFORE redaction so the
    # redacted document the redactor signs covers the completed pointer. Per
    # draft-ietf-vcon-vcon-container 4.1.6 the redactor SHOULD sign the redacted
    # vCon, and for the external reference the url SHOULD and the content_hash
    # MUST be included.
    prefix = _prefix_from_recording_key(event.get("recording_key"))
    put_keys = {}
    if bucket and prefix:
        signed_key = "{}/vcon-signed.jws".format(prefix)
        full_key = "{}/vcon-full.jwe".format(prefix)
        redacted_key = "{}/vcon-redacted.jws".format(prefix)
        full_url = "s3://{}/{}".format(bucket, full_key)
        # content_hash is over the EXACT bytes that will be PUT as the full JWE.
        content_hash = compute_content_hash(full_jwe)
        reference = build_redacted_reference(
            full_uuid, url=full_url, content_hash=content_hash
        )
        redacted = redact_vcon(vcon, reference, detect_phi=detect_phi)
        degraded = degraded_spans[0] if degraded_spans else None
        # Sign the FINAL redacted document, including its now-complete `redacted`
        # pointer (uuid + type + url + content_hash), with the same demo key.
        redacted_jws = sign_vcon(redacted, private_key)
        try:
            s3.put_object(
                Bucket=bucket,
                Key=signed_key,
                Body=signed_jws.encode("utf-8"),
                ContentType=VCON_JWS_CONTENT_TYPE,
            )
            s3.put_object(
                Bucket=bucket,
                Key=full_key,
                Body=full_jwe.encode("utf-8"),
                ContentType="application/jose",
            )
            s3.put_object(
                Bucket=bucket,
                Key=redacted_key,
                Body=redacted_jws.encode("utf-8"),
                ContentType=VCON_JWS_CONTENT_TYPE,
            )
            put_keys = {
                "signed_key": signed_key,
                "full_key": full_key,
                "redacted_key": redacted_key,
            }
        except Exception as exc:
            # Fail-open: a PUT failure does not vanish the sealed record from the
            # step output. Production: Catch to an alarm + retry.
            degraded = (degraded + "; " if degraded else "") + (
                "s3 put_object failed: {}".format(type(exc).__name__)
            )
    else:
        # Offline / no-recording path: no run-key prefix means no stored full
        # copy to reference, so the pointer carries just the uuid (no url /
        # content_hash) and nothing is signed or PUT (seal_keys stays empty).
        reference = build_redacted_reference(full_uuid)
        redacted = redact_vcon(vcon, reference, detect_phi=detect_phi)
        degraded = degraded_spans[0] if degraded_spans else None

    return {
        "vcon": vcon,
        "step": "seal",
        "status": "ok",
        "redacted": redacted,
        "seal_keys": put_keys,
        "seal_degraded": degraded,
    }
