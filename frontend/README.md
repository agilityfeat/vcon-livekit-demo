# Demo application (backend + frontend)

A single, locally-runnable end-to-end demo built on top of the intake agent and
the AWS enrichment + seal pipeline. It makes the whole flow visible from one
app that looks like a real product:

1. A **human patient** opens the app and places a **real** intake call to the
   bot from the browser (the mic is published to the same LiveKit room the agent
   worker auto-joins).
2. The call is recorded, enriched, signed, encrypted, and redacted by the
   deployed pipeline + seal step, producing vCon artifacts in S3.
3. A **doctor** reviews the full record and an **analyst** reviews the redacted
   record, both as **asynchronous viewers** of the produced record.

The backend does ALL the crypto server-side (verify + decrypt) and serves
readable vCon JSON, so the browser never handles the demo signing key.

## Layout

```
frontend/
  index.html / call.js     Patient "Start call" page (real patient<->bot call)
  records.html / records.js Records list (GET /api/records)
  record.html / record.js   Record detail, doctor/analyst toggle
  vcon-helpers.js           Pure, DOM-free vCon helpers (shared, Node-checkable)
  app.js                    App-shell nav highlighting
  styles.css                Shared stylesheet
  fixtures/                 Offline sample pair + the generator script + demo key
```

The backend lives in `src/vcon_agent/app/` (a pure, framework-free
`records.py` core + a thin `main.py` FastAPI route layer that also serves this
`frontend/` directory as static files).

## Run it

Install the app extra and start one process that serves both the API and the UI:

```bash
pip install -e '.[app]'
PYTHONPATH=src:infrastructure/functions/seal \
    uvicorn vcon_agent.app.main:app --reload
# open http://127.0.0.1:8000/
```

### Live mode (real call + real records)

Set the real credentials in `.env` (same ones the agent uses: `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, plus `BUCKET_NAME` / `AWS_REGION` and
the signing key). Start the agent worker in another terminal so the bot is in
the room:

```bash
python -m vcon_agent.agent dev
```

Then open the app, click **Start call**, and talk to the bot. Once the pipeline
finishes, the record shows up on the **Records** page.

### Offline mode (viewer side, no call, no AWS)

Leave `BUCKET_NAME` unset. The backend serves one synthetic record from the
committed sample fixture pair under `fixtures/`, so the records list and the
doctor / analyst views are fully demoable with no live call and no AWS. (The
patient Start-call page still needs real LiveKit creds to actually connect.)

## Offline fixtures

The fixture pair is PRODUCED by running the authoritative seal core
(`infrastructure/functions/seal/sealing.py`) over a representative enriched
vCon, so it carries the REAL redacted-pointer shape
(`{uuid, type:'application/vcon+jws', url:'s3://.../vcon-full.jwe',
content_hash:'sha512-...'}`). Regenerate (and re-verify) with:

```bash
PYTHONPATH=.vconlib:src:infrastructure/functions/seal \
    python3 frontend/fixtures/generate_fixtures.py
PYTHONPATH=.vconlib:src:infrastructure/functions/seal \
    python3 -m pytest tests/test_fixtures.py -q
```

`fixtures/demo-signing-key.jwk` is a **throwaway demo key committed on purpose**
so the committed JWE and its `content_hash` stay a consistent captured pair (the
JWE carries a random nonce, so it is only byte-stable when produced once with a
fixed key). It signs/encrypts nothing real.

## Shortcuts (demo) vs. production

- 🩹 **No authentication.** The patient / doctor / analyst "roles" are just
  which page you open; the doctor/analyst view is a client-side toggle, so
  anyone who can reach the backend can read either view.
  → **Production:** real authN/authZ (OIDC + per-role scopes), the full
  (decrypted) view gated to authorized clinicians and audit-logged.
- 🩹 **The live human doctor is stubbed.** The intake is patient ↔ bot only; the
  doctor is an asynchronous viewer of the produced record, not a second live
  participant. → **Production:** real live consultations and/or a clinician
  queue, with the record still produced for the audit trail.
- 🩹 **Self-signed demo key, shipped with the app**, used for both JWS and JWE,
  and `fixtures/demo-signing-key.jwk` is committed. → **Production:** AWS KMS /
  ACM PCA, key rotation, HSM-backed signing, a separate JWE key with least
  privilege, and never shipping keys with the app.
- 🩹 **Records are discovered by scanning** the bucket's run-key prefixes (or
  the single offline fixture), with no pagination. → **Production:** a queryable
  records index (e.g. DynamoDB) written by the pipeline, with pagination,
  filtering, and per-role access scoping.
- 🩹 **The LiveKit browser client is CDN-loaded** (ESM) for a no-build demo, and
  the patient identity / room are chosen client-side. → **Production:** a
  pinned, bundled client served from your own origin, and server-minted tokens
  bound to a verified record.
- 🩹 **The recording `url` is an `s3://` reference** (not playable in a browser)
  and no audio is bundled offline, so the audio element may have nothing to play
  (the medication → audio provenance seek still sets the playhead to the mapped
  dialog turn start). → **Production:** stream the recording from a signed URL.
- 🩹 **CORS is wide open** on the backend for local dev. → **Production:**
  restrict origins to the known frontend origin(s).
