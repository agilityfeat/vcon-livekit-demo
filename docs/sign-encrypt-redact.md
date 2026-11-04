# Sign, encrypt and redact

The seal step turns the **enriched** vCon into the signed, sealed record and
produces a **redacted sibling** linked back to the full version through the vCon
`redacted` chain. The correctness-critical logic lives in the pure, offline-tested
core `infrastructure/functions/seal/sealing.py`.

Unlike the other pipeline steps (inline Lambdas that mirror their pure core by
hand), the Seal Lambda **imports the sealing core packaged alongside its handler**
(`seal_handler.py`). It is a container-built function, packaged with AWS SAM so it
can ship the `jwcrypto` dependency, wired after the merge step. The pytest suite
imports the same `sealing.py`, and so does the demo backend, which uses it to
decrypt and verify. There is no vendored copy. For the build and deploy flow, see
[infrastructure.md](infrastructure.md#deploy).

## What the seal step does

1. **Sign (JWS).** The enriched vCon is serialized to canonical JSON and signed with
   a demo RSA keypair (`PS256`). The signed document is the authoritative,
   integrity-protected record. `verify_vcon` recovers the exact payload and fails
   loudly on a tampered payload or a wrong key.
2. **Encrypt the signed copy (JWE).** The signed JWS is encrypted with `RSA-OAEP-256`
   key wrap and `A256GCM` content encryption, so the complete, un-redacted record is
   stored encrypted and only a holder of the private key can read it. The demo uses
   the same key for the JWS and the JWE.
3. **Produce the redacted sibling.** A copy of the vCon is created with PII removed
   from free text while the document structure stays intact: the same top-level
   keys, the same `parties` / `dialog` / `analysis` / `attachments` shape and
   ordering, and the same party indices. Redacted spans are replaced with a stable
   `[REDACTED]` placeholder rather than dropped, so a consumer can see where
   redaction happened. See [Redaction scope and limitations](#redaction-scope-and-limitations).
4. **Link via the `redacted` pointer.** The redacted copy is itself a **signed vCon
   (JWS)**, and it carries a top-level `redacted` reference to the encrypted full
   copy (`vcon-full.jwe`): the full vCon `uuid`, a content `type`
   (`application/vcon+jws`), the `url` of the encrypted copy (`s3://<bucket>/<key>`),
   and a `content_hash` of that copy. This matches the repo's by-`uuid` / by-index
   convention (never embedded identities). Only the redacted copy carries the
   pointer; the full copy never does.

### The three sibling objects

The step derives the run prefix from the `recording_key` on its input
(`rsplit('/', 1)[0]`) and writes three objects next to the skeleton:

```
<room_name>/<session_start_iso>/vcon-signed.jws     (application/vcon+jws)
<room_name>/<session_start_iso>/vcon-full.jwe       (application/jose)
<room_name>/<session_start_iso>/vcon-redacted.jws   (application/vcon+jws)
```

Its output carries `seal_keys` (`{signed_key, full_key, redacted_key}`) and an
optional `seal_degraded` string. With no bucket or no recording key (the canned
validation input), the step derives no prefix, writes **nothing**, returns an empty
`seal_keys`, and the pointer carries only `uuid` and `type`. If an S3 PUT fails the
step fails open and records the error in `seal_degraded`.

## Why this runs in the pipeline, not the agent

Signing, encryption, and redaction happen in the enrichment pipeline after the merge
step, never in the agent process. This keeps the "no AWS in the agent process"
design note intact. The Comprehend Medical `DetectPHI` pass is confined to the Seal
Lambda's handler glue. The pure core never calls AWS; it accepts an injected
`detect_phi` callable (or pre-computed `phi_spans`) so it stays testable offline with
canned spans.

## Signing key

The Seal Lambda reads `VCON_SIGNING_PRIVATE_JWK` (JWK JSON or PEM), set from the
`VconSigningPrivateJwk` CloudFormation parameter. The demo backend reads the same
variable from `.env`. **They must be the same key.** If the Lambda has none, it
generates an ephemeral per-invocation key and logs a warning; records sealed that way
can never be read back. On a mismatch the backend returns HTTP 409 with "This record
was sealed with a different signing key ...". Only the private key must match; the
public half is derived from it. Setup steps are in
[infrastructure.md](infrastructure.md#shared-signing-key) and the symptom is covered
in [demo-app.md](demo-app.md#signing-key-mismatch).

## Library choice: jwcrypto

The seal uses [`jwcrypto`](https://jwcrypto.readthedocs.io/), a pure-Python JWS / JWE
/ JWK library on top of `cryptography`. The vCon spec models a signed vCon as a JWS
and an encrypted vCon as a JWE over the JSON document, and the vCon reference Python
library (`python-vcon`) itself uses `jwcrypto`. Taking `python-vcon` as a direct
dependency would pull a large, heavier tree that is less reliably installable across
`requires-python >=3.9`; `jwcrypto` provides exactly the JOSE primitives needed while
keeping the dependency surface small and offline-testable. It is a main dependency
because the pure core imports it at runtime, and the Seal Lambda pins it in
`infrastructure/functions/seal/requirements.txt`.

## Redaction scope and limitations

The redacted sibling strips **HIPAA identifiers**, not the clinical narrative.

**What is visited.** The redactor works deny-by-default. It hard-replaces every
`parties[].name` with the placeholder, clears the `url` of every recording dialog
(so the `s3://` audio location does not ride along), and then walks the whole
document except the top-level `redacted` pointer. Every string whose key is not in
a small structural allowlist (`vcon`, `uuid`, `type`, `vendor`, `role`, `mimetype`,
`speaker`, `filename`, `created_at`, `timestamp`, `start_iso`, `content_hash`,
`degraded`, `category`) goes through the masking pass. That covers `subject`, the
consent `script`, dialog `body`, analysis `body`, entity `text`, and `turns[].text`,
and any future free-text field by default. This is a reachability guarantee, not a
content guarantee: what is stripped from a visited field depends on the two layers
below.

**What strips content.** Two layers:

- A **naive local regex** pass catches emails, SSN-like digit runs, and phone numbers.
- An **AWS Comprehend Medical `DetectPHI`** pass over the same fields detects the
  HIPAA identifier entity types it supports: `AGE`, `DATE`, `NAME`, `PHONE_OR_FAX`,
  `EMAIL`, `ID`, `URL`, `ADDRESS`, and `PROFESSION` (see the
  [DetectPHI documentation](https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-phi.html)).
  A failure on any field degrades that run to regex-only and is recorded in
  `seal_degraded`.

Offline, only regex-matchable PII is removed; real PHI removal needs a live
`DetectPHI` run.

**What stays in cleartext.** The **clinical narrative**: medical conditions and
medications. That is deliberate. Those entities come from a different Comprehend
Medical API, `DetectEntitiesV2` (categories `MEDICAL_CONDITION` and `MEDICATION`; see
the [DetectEntitiesV2 documentation](https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-entitiesv2.html)),
and they are not "PHI" in Comprehend's model. `DetectPHI` leaving them in place is the
service working as designed, not a defect. Note that the entity `text` is still run
through the masking pass; only the `category` label is allowlisted, because the viewer
depends on it. The vCon container draft also leaves the method out of scope:
[draft-ietf-vcon-vcon-container](https://datatracker.ietf.org/doc/html/draft-ietf-vcon-vcon-container)
section 4.1.6 states that "The method of redaction is out of scope of this document".

**Production direction.** Vetted de-identification is multi-layered rather than a
single `DetectPHI` call. The AWS
[prescriptive guidance for Comprehend Medical](https://docs.aws.amazon.com/prescriptive-guidance/latest/generative-ai-nlp-healthcare/comprehend-medical-rag.html)
combines Comprehend Medical PHI and entity detection with human review and/or LLM
guardrails. A production pipeline would add confidence-threshold tuning and review
against the HIPAA **Safe Harbor** 18-identifier standard or **expert determination**.
AWS gives its own caveat on the `DetectPHI` page: the detected entities "don't map 1:1
to the list specified by the Safe Harbor method", and AWS recommends that you "use
additional human review or other methods to confirm the accuracy of detected PHI". The
demo keeps redaction naive on purpose.

**vCon conformance of the redacted sibling.** Two behaviors align it with section
4.1.6 of the container draft:

- The redacted sibling is a **signed vCon (JWS)**, signed with the same demo RSA key
  (`PS256`) as the full copy. This follows the draft's "the redactor SHOULD sign the
  redacted version of the vCon".
- The `redacted` pointer carries a **`content_hash`** of the referenced encrypted full
  copy alongside its `uuid`, `type`, and `url`. This follows the draft's "url SHOULD be
  included unless redacted and content_hash MUST be included". The format is
  `sha512-<base64url>`: the literal prefix `sha512-` followed by the unpadded base64url
  encoding of the SHA-512 digest of the exact stored JWE bytes, matching the draft's
  Externally Referenced Files examples.

## Offline verification

The sign/verify, encrypt/decrypt, and redaction logic are pure functions in
`sealing.py`, unit-tested in `tests/test_sealing.py` with an in-memory demo keypair,
canned PHI spans, and no network. `tests/test_seal_pipeline_wiring.py` covers the
handler wiring with fakes. The crypto-dependent tests use
`pytest.importorskip('jwcrypto')`, so they skip cleanly if the library is not
installed. Shortcuts for this step are in
[production-considerations.md](production-considerations.md#sign-encrypt-and-redact).
