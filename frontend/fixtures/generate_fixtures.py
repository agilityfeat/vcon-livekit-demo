#!/usr/bin/env python3
"""Generate the offline sample fixture pair by RUNNING the authoritative seal core.

The demo backend has an offline fallback (``BUCKET_NAME`` unset): instead of
scanning S3 it reads a committed full + redacted vCon pair from this directory,
so the records list and the doctor/analyst views are demoable with NO live call
and NO AWS. This script PRODUCES that pair by running the SAME sign / encrypt /
redact core the deployed Seal Lambda runs (``infrastructure/functions/seal/
sealing.py``), so the committed fixtures carry main's REAL redacted-pointer shape
and are regenerable and self-checking (see ``tests/test_fixtures.py``).

What it writes into this directory:

  * ``demo-signing-key.jwk``  -- the committed throwaway demo keypair (private
    JWK). Generated ONCE and committed so the committed JWE + its content_hash
    stay a consistent captured pair (the JWE carries a random nonce, so it is
    only byte-stable when produced once with a fixed key). THIS IS A DEMO-ONLY
    THROWAWAY KEY checked into the repo on purpose; it signs/encrypts nothing
    real. 🩹 Shortcut: a self-signed demo keypair shipped with the app.
    -> Production: AWS KMS / ACM PCA, key rotation, HSM-backed signing; never
    ship keys with the app.
  * ``vcon-signed.jws``   -- the enriched full vCon, signed (JWS).
  * ``vcon-full.jwe``     -- the signed full copy, encrypted (JWE).
  * ``vcon-redacted.jws`` -- the redacted sibling, signed (JWS), carrying the
    real ``redacted`` pointer back to the full copy.
  * ``vcon-full.json``    -- the DECODED, verified full vCon (NO top-level
    ``redacted`` pointer). The backend's offline fallback serves this as the
    doctor view.
  * ``vcon-redacted.json`` -- the DECODED, verified redacted vCon, carrying the
    real ``redacted`` pointer {uuid, type:'application/vcon+jws',
    url:'s3://.../vcon-full.jwe', content_hash:'sha512-...'}. The backend's
    offline fallback serves this as the analyst view.

Run it from the repo root (the seal core is imported via the same ``sys.path``
shim the pytest suite uses):

    PYTHONPATH=.vconlib:src:infrastructure/functions/seal \
        python3 frontend/fixtures/generate_fixtures.py

Re-running with the committed ``demo-signing-key.jwk`` present reuses that key
(so the JWS/JWE are regenerated consistently). Delete the key file first to mint
a brand-new throwaway key.

🩹 Shortcut: the recording dialog's ``url`` is an ``s3://`` reference and no real
audio is bundled, and the run prefix is a fixed ``demo-bucket`` placeholder.
-> Production: the artifacts come from a real pipeline run against a real bucket;
the viewer streams the recording from a signed URL.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the authoritative seal core + the vCon model.
# ---------------------------------------------------------------------------
# frontend/fixtures/generate_fixtures.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEAL_PKG = str(_REPO_ROOT / "infrastructure" / "functions" / "seal")
_SRC = str(_REPO_ROOT / "src")
for _p in (_SEAL_PKG, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sealing  # noqa: E402  (import after the sys.path shim)
from vcon_agent.vcon import VConBuilder  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent

# The committed throwaway demo key (private JWK JSON). Kept in this directory so
# the committed JWE + its content_hash are a consistent pair generated once with
# a fixed key.
KEY_FILE = FIXTURES_DIR / "demo-signing-key.jwk"

# Fixed run prefix the offline fallback advertises (see RecordsStore in
# vcon_agent.app.records). The redacted pointer's s3:// url is built from it so
# the url ends with ``vcon-full.jwe`` exactly like a real pipeline run.
RUN_PREFIX = "fixtures/sample-call"
DEMO_BUCKET = "demo-bucket"


def load_or_create_key() -> "sealing.jwk.JWK":
    """Load the committed throwaway demo key, or mint + commit one if absent.

    🩹 Shortcut: a self-signed demo keypair shipped with the app, loaded from a
    file in the repo. -> Production: AWS KMS / ACM PCA, key rotation, HSM-backed
    signing; never ship keys with the app.
    """
    if KEY_FILE.is_file():
        return sealing.load_jwk(KEY_FILE.read_text(encoding="utf-8"))
    key = sealing.generate_demo_keypair(size=2048)
    # Export the FULL (private) JWK so the same key can decrypt/verify later.
    KEY_FILE.write_text(key.export(private_key=True), encoding="utf-8")
    return key


def build_enriched_vcon() -> dict:
    """Build a representative ENRICHED vCon the seal core can sign/redact.

    Mirrors what the live agent + enrichment pipeline would produce, but with
    deliberately DEMO-FRIENDLY data:

    * two fixed parties (patient index 0, agent index 1);
    * dialog text turns with NUMERIC ``start`` + ``duration`` (seconds) so the
      viewer's audio-seek + medication->provenance click-through work;
    * a ``recording`` dialog object (external s3:// reference);
    * transcript / entities / summary analysis entries with dialog turn-index
      provenance; the entities entry carries a MEDICATION ('ibuprofen') and a
      MEDICAL_CONDITION ('headache') pinned to the dialog turn where they are
      mentioned;
    * a consent attachment recorded before any clinical content;
    * a PII-looking token (a phone number) in a dialog body so redaction is
      visibly demonstrated in the analyst view.
    """
    builder = VConBuilder(subject="Patient intake call (demo fixture)")
    builder.add_consent(granted=True)

    # Numeric start/duration (seconds) so the audio-seek works. The builder
    # accepts a ``start`` override; duration is set directly afterwards because
    # the text-turn helper does not take one.
    turns = [
        (1, "Hi, thanks for calling. What brings you in today?", 0.0, 4.0),
        (0, "I've had a bad headache since yesterday.", 4.0, 3.5),
        (1, "I'm sorry to hear that. Have you taken anything for it?", 7.5, 4.0),
        (0, "I took some ibuprofen this morning but it didn't help much.", 11.5, 4.5),
        (1, "Understood. What is the best callback number for you?", 16.0, 3.5),
        # PII-looking token (phone number) so redaction is visible in the
        # analyst view. The naive regex PII pass in sealing.py masks it.
        (0, "You can reach me at 415-555-0137.", 19.5, 3.0),
    ]
    for party_index, text, start, duration in turns:
        turn = builder.add_dialog_turn(party_index, text, start=start)
        turn["start"] = float(start)
        turn["duration"] = float(duration)

    # The whole-call recording as an external s3:// reference (no audio bytes
    # are ever inlined). Numeric start keeps it on the same timeline.
    rec = builder.add_recording_dialog(
        url="s3://{}/{}/recording.ogg".format(DEMO_BUCKET, RUN_PREFIX),
        start=0.0,
    )
    rec["start"] = 0.0
    rec["duration"] = 23.0

    # Transcript analysis (authoritative), covering the four speech turns.
    builder.add_transcript_analysis(
        transcript_text=(
            "Agent: What brings you in today? "
            "Patient: I've had a bad headache since yesterday. "
            "Agent: Have you taken anything for it? "
            "Patient: I took some ibuprofen this morning but it didn't help much."
        ),
        dialog_indices=[0, 1, 2, 3],
    )

    # Entities analysis pinned to dialog turn 3 (the ibuprofen/headache turn),
    # so the viewer's medication->audio provenance resolves MEDICATION
    # 'ibuprofen' to dialog[3].start (11.5s).
    builder.add_entities_analysis(
        entities=[
            {"category": "MEDICATION", "text": "ibuprofen", "score": 0.99},
            {"category": "MEDICAL_CONDITION", "text": "headache", "score": 0.98},
        ],
        dialog_index=3,
    )

    # Physician-facing summary over the clinical turns.
    builder.add_summary_analysis(
        summary_text=(
            "Patient reports a headache since yesterday, self-treated with "
            "ibuprofen with little relief."
        ),
        dialog_indices=[0, 1, 2, 3],
    )

    return builder.to_dict()


def main() -> None:
    key = load_or_create_key()
    pub = sealing.public_jwk(key)

    vcon = build_enriched_vcon()
    full_uuid = vcon["uuid"]

    # 1) Sign the enriched full vCon (JWS), then 2) encrypt the signed copy
    # (JWE). This mirrors seal_handler.py exactly.
    signed_jws = sealing.sign_vcon(vcon, key)
    full_jwe = sealing.encrypt_vcon(signed_jws, pub)

    # 3) content_hash over the EXACT bytes stored as the full JWE, then 4) build
    # the real redacted pointer and 5) redact + 6) sign the redacted sibling.
    content_hash = sealing.compute_content_hash(full_jwe)
    full_url = "s3://{}/{}/vcon-full.jwe".format(DEMO_BUCKET, RUN_PREFIX)
    reference = sealing.build_redacted_reference(
        full_uuid, url=full_url, content_hash=content_hash
    )
    redacted = sealing.redact_vcon(vcon, reference)
    redacted_jws = sealing.sign_vcon(redacted, key)

    # Decode the sealed artifacts back to verified readable JSON (exactly what
    # the backend's offline fallback serves):
    #   * full view  = decrypt JWE -> verify inner JWS (NO redacted pointer)
    #   * analyst view = verify redacted JWS (carries the real redacted pointer)
    full_readable = sealing.verify_vcon(sealing.decrypt_vcon(full_jwe, key), pub)
    redacted_readable = sealing.verify_vcon(redacted_jws, pub)

    # Sanity: the full copy must NOT carry a redacted pointer; the redacted copy
    # must carry the real one.
    assert "redacted" not in full_readable, "full copy must not carry a redacted pointer"
    assert redacted_readable["redacted"]["uuid"] == full_uuid

    writes = {
        "vcon-signed.jws": signed_jws,
        "vcon-full.jwe": full_jwe,
        "vcon-redacted.jws": redacted_jws,
        "vcon-full.json": json.dumps(full_readable, indent=2, sort_keys=True) + "\n",
        "vcon-redacted.json": json.dumps(redacted_readable, indent=2, sort_keys=True)
        + "\n",
    }
    for name, text in writes.items():
        (FIXTURES_DIR / name).write_text(text, encoding="utf-8")
        print("wrote {}".format(FIXTURES_DIR / name))

    print("\nfull uuid:      {}".format(full_uuid))
    print("content_hash:   {}".format(content_hash))
    print("redacted.url:   {}".format(full_url))


if __name__ == "__main__":
    main()
