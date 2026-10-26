"""Pure, boto3-free signing / encryption / redaction core for the Spec 4 seal.

This module is the AUTHORITATIVE, unit-tested copy of the correctness-critical
Spec 4 logic: turning the completed, enriched vCon from Spec 3 into a signed
(JWS) and encrypted (JWE) record, and producing a redacted sibling that strips
PII from the dialog / analysis bodies while preserving the document structure and
pointing back at the full signed version via the vCon ``redacted`` reference.

Why a separate pure module? The real pipeline runs this logic as an inline-code
Lambda inside the CloudFormation template (``Code.ZipFile``), which CANNOT import
this repo package at AWS runtime. FEAT-002 therefore MIRRORS this logic into that
inline Seal Lambda body. Keeping the logic here as pure functions means it is
exercised by the offline pytest suite (demo keypair generated in-memory, canned
PHI spans, no live AWS and no live Comprehend Medical), and the inline copy stays
small and checkable with ``py_compile``. This mirror/pure-core split matches the
existing convention already used across the repo (``vcon.py`` and
``enrichment.py`` are the framework-independent cores mirrored into inline
Lambdas).

JOSE library choice: ``jwcrypto`` (pure-Python JWS/JWE/JWK on top of
``cryptography``). The vCon spec models a signed vCon as a JWS and an encrypted
vCon as a JWE over the JSON document, and the vCon reference Python library
(``python-vcon``) itself uses ``jwcrypto`` for its signing/encryption. Taking
``python-vcon`` as a direct dependency pulls a large, heavier tree that is less
reliably installable across ``requires-python >=3.9``; ``jwcrypto`` gives the
exact JWS/JWE/JWK primitives needed to implement the vCon JWS/JWE/redacted
structure directly while keeping the dependency surface small and offline-testable.

Everything here is deterministic where cryptographically possible (JWS/JWE carry
random nonces by construction, so ciphertext is not byte-stable, but round-trips
are) and dependency-light: ``jwcrypto`` (+ its ``cryptography`` transitive dep)
only, no boto3 and no network. The AWS Comprehend Medical ``DetectPHI`` pass is
NOT called here; :func:`redact_vcon` accepts an optional injected ``detect_phi``
callable (or pre-computed ``phi_spans``) so the AWS pass can be mirrored into the
inline Lambda while this pure core stays offline-testable with canned spans.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from jwcrypto import jwe, jwk, jws
from jwcrypto.common import json_encode

# ---------------------------------------------------------------------------
# JOSE algorithm choices.
# ---------------------------------------------------------------------------
# JWS: PS256 (RSASSA-PSS w/ SHA-256) over the canonical JSON of the enriched
# vCon. JWE: RSA-OAEP-256 key wrap + A256GCM content encryption. These are
# widely supported, modern JOSE algs that work with an RSA demo keypair.
JWS_ALG = "PS256"
JWE_ALG = "RSA-OAEP-256"
JWE_ENC = "A256GCM"

# Media type used to tag the signed full vCon and the redacted pointer's content
# type, so a consumer following the `redacted` reference knows the full version
# is a JWS-serialized vCon.
VCON_JWS_CONTENT_TYPE = "application/vcon+jws"

# The `type` carried on the redacted copy's `redacted` reference. The reference
# identifies the full version by uuid (never an embedded identity), matching the
# repo's by-index / by-uuid convention.
REDACTED_POINTER_TYPE = VCON_JWS_CONTENT_TYPE

# Stable placeholder substituted for any redacted span. Structure is preserved:
# fields are never dropped, their sensitive content is blanked to this marker.
REDACTION_PLACEHOLDER = "[REDACTED]"

# 🩹 Shortcut: naive regex PII patterns (emails, phone numbers, SSN-like digit
# runs) applied over known dialog/analysis body fields. These are deliberately
# coarse and English/US-centric. → Production: vetted de-identification against
# the full PHI surface, reviewed for the applicable safe-harbor /
# expert-determination standard.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# SSN-like: 3-2-4 digit runs (with optional - or space separators).
_SSN_RE = re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b")
# Phone-like: 10+ digit runs with common separators / optional country code.
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,2}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\w)"
)
# Order matters: SSN before phone so a 3-2-4 run is tagged as an SSN, not a
# phone number.
_NAIVE_PII_PATTERNS = (_EMAIL_RE, _SSN_RE, _PHONE_RE)


# A phi-span is a plain (start, end) pair of character offsets into a given text.
# This is the shape of a Comprehend Medical DetectPHI "Entity" reduced to the
# offsets the redactor needs (BeginOffset / EndOffset). The optional injected
# ``detect_phi`` callable returns these spans for a piece of text so the AWS pass
# is mirrored into the inline Lambda while this core stays offline-testable.
PhiSpan = Sequence[int]  # (begin_offset, end_offset)
DetectPhi = Callable[[str], Sequence[PhiSpan]]


# ---------------------------------------------------------------------------
# Demo keypair handling.
# ---------------------------------------------------------------------------
# 🩹 Shortcut: self-signed demo keypair generated/loaded locally (env/local),
# not a managed key. → Production: AWS KMS / ACM PCA, key rotation, HSM-backed
# signing; never ship keys with the app. (Ties to the AWS-plugin
# long-term-credential caveat - #1719.)


def generate_demo_keypair(size: int = 2048) -> jwk.JWK:
    """Generate an RSA demo keypair as a jwcrypto JWK (private key included).

    Returns a single :class:`jwcrypto.jwk.JWK` holding the private key; the
    matching public JWK is obtained with :func:`public_jwk`. Tests call this to
    create a key fully in-memory (no network, no managed KMS). 2048-bit RSA is a
    sensible default that stays fast enough for the offline suite.

    🩹 Shortcut: self-signed demo keypair generated locally, not a managed key.
    → Production: AWS KMS / ACM PCA, key rotation, HSM-backed signing; never ship
    keys with the app. (Ties to the AWS-plugin long-term-credential caveat -
    #1719.)
    """
    return jwk.JWK.generate(kty="RSA", size=size)


def public_jwk(key: jwk.JWK) -> jwk.JWK:
    """Return the public-only JWK for ``key`` (strips any private parameters).

    Signing / decryption need the private JWK; verification / encryption need
    only the public half. This re-imports the exported public key so callers can
    hand the public JWK to an encryptor or verifier without leaking private
    parameters.
    """
    return jwk.JWK.from_json(key.export_public())


def load_jwk(text: str) -> jwk.JWK:
    """Load a JWK from its JSON serialization or a PEM string.

    Accepts either a JWK JSON document (what :meth:`jwk.JWK.export` produces) or a
    PEM-encoded key (public or private). This lets the pipeline load a demo key
    supplied via an env var or file in whichever of the two common formats it was
    saved as, and lets tests round-trip an exported key.

    🩹 Shortcut: self-signed demo keypair loaded locally from env/local text, not
    a managed key. → Production: AWS KMS / ACM PCA, key rotation, HSM-backed
    signing; never ship keys with the app.
    """
    if text is None:
        raise ValueError("key text must not be None")
    stripped = text.strip()
    if not stripped:
        raise ValueError("key text must not be empty")
    # PEM keys begin with a "-----BEGIN ..." armor line; JWK is JSON.
    if stripped.startswith("-----BEGIN"):
        return jwk.JWK.from_pem(stripped.encode("utf-8"))
    return jwk.JWK.from_json(stripped)


def load_jwk_from_env(
    env_var: str, env: Optional[Dict[str, str]] = None
) -> Optional[jwk.JWK]:
    """Load a demo JWK/PEM from an environment variable, or ``None`` if unset.

    ``env`` defaults to ``os.environ`` but can be injected (a plain dict) so tests
    stay offline and deterministic without mutating the process environment. A
    missing or empty variable returns ``None`` so the caller can fall back to a
    generated demo key, matching the AWS-optional style used elsewhere in the
    repo.
    """
    source = os.environ if env is None else env
    value = source.get(env_var)
    if not value or not value.strip():
        return None
    return load_jwk(value)


# ---------------------------------------------------------------------------
# Canonical JSON.
# ---------------------------------------------------------------------------


def canonical_json(vcon_doc: Dict[str, Any]) -> str:
    """Serialize a vCon document to canonical JSON for signing / encryption.

    Keys are sorted and separators are compact so the same document always
    produces the same payload bytes, which keeps sign/verify deterministic over
    equal documents.
    """
    return json.dumps(vcon_doc, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# JWS: sign / verify the enriched vCon.
# ---------------------------------------------------------------------------


def sign_vcon(vcon_doc: Dict[str, Any], private_key: jwk.JWK) -> str:
    """Sign the completed / enriched vCon, returning a JWS JSON serialization.

    The payload is the canonical JSON of ``vcon_doc``. The protected header
    carries the JWS alg (:data:`JWS_ALG`), the content type (:data:`VCON_JWS_CONTENT_TYPE`),
    and the signing key's ``kid`` when present so a verifier can select the key.
    Returns the general JWS JSON serialization string.
    """
    token = jws.JWS(canonical_json(vcon_doc).encode("utf-8"))
    header: Dict[str, Any] = {"alg": JWS_ALG, "cty": VCON_JWS_CONTENT_TYPE}
    kid = private_key.get("kid")
    if kid:
        header["kid"] = kid
    token.add_signature(
        private_key,
        alg=JWS_ALG,
        protected=json_encode(header),
    )
    return token.serialize()


def verify_vcon(jws_serialization: str, public_key: jwk.JWK) -> Dict[str, Any]:
    """Verify a JWS produced by :func:`sign_vcon` and return the vCon payload.

    Raises a :class:`jwcrypto.jws.InvalidJWSSignature` (via ``verify``) if the
    signature does not match the key, so a tampered payload or a wrong key fails
    loudly. On success the JSON payload is parsed back into a dict.
    """
    token = jws.JWS()
    token.deserialize(jws_serialization)
    token.verify(public_key)
    return json.loads(token.payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# JWE: encrypt / decrypt the full vCon (or its JWS).
# ---------------------------------------------------------------------------
# 🩹 Shortcut: JWE encryption with a demo key, stored in the same bucket.
# → Production: SSE-KMS, separate access scopes for full vs. redacted, least
# privilege.


def encrypt_vcon(
    vcon_doc_or_jws: Union[Dict[str, Any], str], public_key: jwk.JWK
) -> str:
    """Encrypt the full vCon (a dict) or its JWS serialization into a JWE.

    Accepts either a vCon dict (serialized to canonical JSON) or an already-signed
    JWS string, so the pipeline can encrypt the signed full copy. Produces a JWE
    compact serialization using RSA-OAEP-256 key wrap and A256GCM content
    encryption.

    🩹 Shortcut: JWE encryption with a demo key, stored in the same bucket.
    → Production: SSE-KMS, separate access scopes for full vs. redacted, least
    privilege.
    """
    if isinstance(vcon_doc_or_jws, str):
        plaintext = vcon_doc_or_jws
    else:
        plaintext = canonical_json(vcon_doc_or_jws)
    token = jwe.JWE(
        plaintext.encode("utf-8"),
        recipient=public_key,
        protected={"alg": JWE_ALG, "enc": JWE_ENC},
    )
    return token.serialize(compact=True)


def decrypt_vcon(jwe_serialization: str, private_key: jwk.JWK) -> str:
    """Decrypt a JWE produced by :func:`encrypt_vcon`, returning the plaintext.

    Returns the plaintext string (either the canonical vCon JSON or the JWS,
    depending on what was encrypted). Raises on a wrong key / tampered ciphertext,
    so a wrong JWE key fails to decrypt loudly.
    """
    token = jwe.JWE()
    token.deserialize(jwe_serialization)
    token.decrypt(private_key)
    return token.payload.decode("utf-8")


# ---------------------------------------------------------------------------
# Redaction.
# ---------------------------------------------------------------------------
# 🩹 Shortcut: Redaction = naive PII removal (regex / Comprehend Medical PHI pass
# over known fields). → Production: vetted de-identification against the full PHI
# surface, reviewed for the applicable safe-harbor/expert-determination standard.


def _spans_from_regex(text: str) -> List[PhiSpan]:
    """Collect (begin, end) spans for every naive-PII match in ``text``."""
    spans: List[PhiSpan] = []
    for pattern in _NAIVE_PII_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    return spans


def _apply_spans(text: str, spans: Sequence[PhiSpan]) -> str:
    """Replace each (begin, end) span in ``text`` with the redaction placeholder.

    Overlapping / adjacent spans are merged so the placeholder is not duplicated.
    Spans are clamped to the text bounds and invalid (empty / reversed) spans are
    ignored, so a canned or Comprehend-derived span list can be applied safely.
    """
    normalized: List[tuple] = []
    for span in spans:
        if span is None or len(span) < 2:
            continue
        begin, end = int(span[0]), int(span[1])
        begin = max(0, min(begin, len(text)))
        end = max(0, min(end, len(text)))
        if end <= begin:
            continue
        normalized.append((begin, end))
    if not normalized:
        return text
    normalized.sort()
    # Merge overlapping / touching spans.
    merged: List[List[int]] = [list(normalized[0])]
    for begin, end in normalized[1:]:
        if begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    # Rebuild the string, replacing each merged span with the placeholder.
    out: List[str] = []
    cursor = 0
    for begin, end in merged:
        out.append(text[cursor:begin])
        out.append(REDACTION_PLACEHOLDER)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _redact_text(text: str, detect_phi: Optional[DetectPhi]) -> str:
    """Redact one text field: naive regex spans plus any injected PHI spans.

    ``detect_phi`` (when supplied) is the seam the AWS Comprehend Medical
    DetectPHI pass is mirrored into: it is called with the raw text and returns
    (begin, end) offset spans, which are merged with the regex spans before
    replacement. The pure core never calls AWS itself; tests pass a canned
    callable.
    """
    if not isinstance(text, str) or not text:
        return text
    spans: List[PhiSpan] = _spans_from_regex(text)
    if detect_phi is not None:
        spans.extend(detect_phi(text) or [])
    return _apply_spans(text, spans)


def _redact_entities(
    entities: Sequence[Dict[str, Any]], detect_phi: Optional[DetectPhi]
) -> List[Dict[str, Any]]:
    """Redact the ``text`` of each entity dict in an analysis entities body.

    Keeps the list length and each entity's other keys (e.g. ``category``,
    ``score``) intact so structure is preserved; only the ``text`` value is run
    through :func:`_redact_text`.
    """
    redacted: List[Dict[str, Any]] = []
    for ent in entities:
        if isinstance(ent, dict):
            new_ent = dict(ent)
            if isinstance(new_ent.get("text"), str):
                new_ent["text"] = _redact_text(new_ent["text"], detect_phi)
            redacted.append(new_ent)
        else:
            redacted.append(ent)
    return redacted


def redact_vcon(
    vcon_doc: Dict[str, Any],
    full_reference: Dict[str, Any],
    detect_phi: Optional[DetectPhi] = None,
    phi_spans: Optional[Dict[str, Sequence[PhiSpan]]] = None,
) -> Dict[str, Any]:
    """Produce the redacted sibling of ``vcon_doc`` with PII stripped.

    The returned document keeps the STRUCTURE intact: the same top-level keys, the
    same ``parties`` / ``dialog`` / ``analysis`` / ``attachments`` shape and
    ordering, and the same party indices. Sensitive content in the dialog turn
    ``body`` text and the analysis ``body`` (both free-text summaries/transcripts
    and ``entities`` entity ``text``) is replaced with :data:`REDACTION_PLACEHOLDER`
    rather than dropped, so a consumer sees where redaction happened.

    Redaction combines a NAIVE regex pass (emails, phone numbers, SSN-like digit
    runs) with optional injected PHI spans:

    * ``detect_phi`` — a callable mirroring the AWS Comprehend Medical DetectPHI
      pass. It is called per text field and returns (begin, end) offset spans.
      The pure core NEVER calls AWS; the inline Lambda supplies the real callable
      while tests supply a canned one.
    * ``phi_spans`` — a pre-computed mapping of ``{text: [(begin, end), ...]}``
      applied to any field whose exact text is a key. This is the simplest way to
      feed deterministic canned spans in a test without a callable.

    The ``full_reference`` (built with :func:`build_redacted_reference`) is set as
    the top-level ``redacted`` pointer on the returned copy so it links back to
    the full signed version by uuid. The caller passes the FULL copy's reference;
    the full copy itself must NOT carry a ``redacted`` pointer.

    🩹 Shortcut: Redaction = naive PII removal (regex / Comprehend Medical PHI
    pass over known fields). → Production: vetted de-identification against the
    full PHI surface, reviewed for the applicable safe-harbor/expert-determination
    standard.
    """
    # Compose an effective detect_phi that also honors the canned phi_spans map.
    effective_detect_phi = detect_phi
    if phi_spans is not None:
        span_map = phi_spans

        def _combined(text: str) -> List[PhiSpan]:
            spans: List[PhiSpan] = list(span_map.get(text, []))
            if detect_phi is not None:
                spans.extend(detect_phi(text) or [])
            return spans

        effective_detect_phi = _combined

    # Deep-ish copy via JSON round-trip keeps the structure and ordering while
    # giving us independent, mutable nested dicts/lists to redact in place. vCon
    # docs are plain JSON, so this is lossless.
    redacted = json.loads(json.dumps(vcon_doc))

    for turn in redacted.get("dialog", []):
        if isinstance(turn, dict) and isinstance(turn.get("body"), str):
            turn["body"] = _redact_text(turn["body"], effective_detect_phi)

    for entry in redacted.get("analysis", []):
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        if isinstance(body, str):
            entry["body"] = _redact_text(body, effective_detect_phi)
        elif isinstance(body, list):
            # entities body: a list of {category, text, score?} dicts.
            entry["body"] = _redact_entities(body, effective_detect_phi)

    # The redacted copy carries the pointer back to the full signed version.
    redacted["redacted"] = dict(full_reference)
    return redacted


# ---------------------------------------------------------------------------
# Redacted pointer helper.
# ---------------------------------------------------------------------------


def build_redacted_reference(
    full_uuid: str,
    type_: str = REDACTED_POINTER_TYPE,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the vCon ``redacted`` reference pointing back to the full version.

    Per the vCon redaction model, the redacted copy links to the unredacted full
    version. Consistent with the repo's by-uuid / by-index convention (never
    embedded identities), the reference identifies the full version by its
    ``uuid`` plus a content ``type`` (defaulting to the JWS media type, since the
    full version is the signed vCon). An optional ``url`` placeholder locates the
    stored full copy (e.g. its S3 object) when known.

    Only the redacted copy carries this pointer; the full copy never does.
    """
    reference: Dict[str, Any] = {"uuid": full_uuid, "type": type_}
    if url is not None:
        reference["url"] = url
    return reference
