# Demo application

A single, locally runnable app ("vCon Care") built on the intake agent and the AWS
enrichment and seal pipeline. One `uvicorn` process serves both the API and the static
UI.

1. A **patient** opens the app and places a real intake call to the bot from the
   browser. The microphone is published to the LiveKit room the agent worker joins.
2. The call is recorded, enriched, signed, encrypted, and redacted by the deployed
   pipeline, producing vCon objects in S3.
3. A **doctor** reviews the full record, with call audio and extracted clinical detail.
   An **analyst** reviews the redacted record.

The backend does all the crypto server-side (it decrypts the JWE and verifies the JWS
with the demo signing key) and returns readable vCon JSON, so the browser never handles
the key.

## Run it

Install the `app` extra (the Nix shell already does) and start one process:

```bash
pip install -e '.[app]'
python -m uvicorn vcon_agent.app.main:app --reload
# open http://127.0.0.1:8000/
```

No `PYTHONPATH` is needed. `records.py` adds `infrastructure/functions/seal/` to
`sys.path` itself. Use `python -m uvicorn` so the server runs under your active
virtualenv's interpreter.

### Offline mode: no call, no AWS

If `BUCKET_NAME` is absent or empty, the backend serves one synthetic record from the
committed fixtures in `frontend/fixtures/`. Doctor and analyst views are fully
demoable; the patient call page still needs real LiveKit credentials to connect.

The backend loads the repo-root `.env` at startup, so the switch is whether
`BUCKET_NAME` has a value **in the loaded environment**. If you have a populated `.env`
from the live demo, **comment out the `BUCKET_NAME` line** (prefix it with `#`) and
restart the server.

> **Why not `env -u BUCKET_NAME`?** The backend re-reads `.env` after the process
> starts, so the unset is undone. Commenting the line out in `.env` is the reliable
> switch.

The offline record has the run prefix `fixtures/sample-call`. In offline mode the
backend ignores the run prefix and returns that one pair for any request, and
`/api/records/.../audio` returns 404, so the record view shows "Audio is not available
in offline/demo mode".

### Live mode: real call, real records

Prerequisites: a deployed stack, the `.env` filled in from its Outputs, and the shared
signing key set on both sides (see
[infrastructure.md](infrastructure.md#shared-signing-key)). Then run the agent in one
terminal and the backend in another:

```bash
python -m vcon_agent.agent dev
python -m uvicorn vcon_agent.app.main:app --reload
```

Open the app, go to **Start call**, and talk to the bot. When you end the call the agent
uploads the vCon and starts the pipeline; once it has run, the record appears on the
doctor and analyst dashboards. This is not instant, because Transcribe Medical is a
batch job and the Seal step runs last; a record only lists once `vcon-signed.jws` exists.

#### Signing key mismatch

In live mode the backend decrypts `vcon-full.jwe` and verifies the JWS with
`VCON_SIGNING_PRIVATE_JWK` from `.env`, while the Seal Lambda sealed them with its own
`VCON_SIGNING_PRIVATE_JWK`, or with an ephemeral in-memory key if it had none. If they
differ, the doctor view fails to decrypt and the analyst view fails to verify. The
backend then returns **HTTP 409** with "This record was sealed with a different signing
key ...", instead of a raw 500. Records sealed with a mismatched or ephemeral key can
never be read back. Fix it by setting the same key on both sides and placing a **new**
call.

## What each page does

All pages live in `frontend/` and share `styles.css` and the nav highlighting in
`app.js`.

| Page | Files | Behavior |
|---|---|---|
| Landing | `index.html` | Static product page with entry points for the three roles. |
| Patient call | `call.html`, `call.js` | Room (default `intake`) and name fields. **Start call** posts to `/api/token`, connects with the LiveKit browser client (loaded from a CDN as an ES module), publishes the mic with a level meter, and plays the bot's audio. The LiveKit server URL comes from the token response. A `?room=` query parameter prefills the room. |
| Doctor sign-in | `doctor-signin.html` | Presentational form; submitting navigates to the dashboard. No credentials are checked. |
| Doctor dashboard | `doctor.html`, `doctor.js` | Lists call records from `GET /api/records`. Each links to `record.html?run=<run_prefix>&view=doctor`. A **Schedule a call** form is UI-only and saves nothing. |
| Analyst sign-in | `analyst-signin.html` | Same as the doctor sign-in, for the analyst. |
| Analyst dashboard | `analyst.html`, `analyst.js` | Lists the same records. Each links to `record.html?run=<run_prefix>&view=analyst`. |
| Record viewer | `record.html`, `record.js` | Renders the report selected by `?view=`. See below. |
| Helpers | `vcon-helpers.js` | Pure, DOM-free vCon helpers used by the viewer. |

There is no standalone records list and no on-page doctor/analyst toggle. The report you
see is decided by the `view` query parameter of the link you followed (`analyst` gives
the redacted view; anything else gives the full view).

### Record viewer

- **Doctor view** (`GET /api/records/{run}/full`): the decrypted, verified full vCon. It
  shows the record metadata, **Call audio** (a presigned URL from
  `GET /api/records/{run}/audio`), parties, extracted medications and conditions, the
  summary with its provenance, the **authoritative clinical transcript**, and the **live
  transcript** for reference. Clicking a medication seeks the audio to the dialog turn it
  came from.
- **Analyst view** (`GET /api/records/{run}/redacted`): the verified redacted vCon led by
  a "De-identified, PHI masked" headline with `[REDACTED]` tokens highlighted. The
  full-to-redacted linkage proof (uuids, pointer type, `content_hash`) sits in a
  collapsed verification section, the full copy's `s3://` URL is not shown, and the call
  audio is withheld.

## Backend API

Routes in `src/vcon_agent/app/main.py`. `main.py` is a thin route layer; the logic is in
the framework-free `records.py` and `livekit_tokens.py`.

| Route | Purpose |
|---|---|
| `POST /api/token` | Mint a LiveKit patient token. Body `{room, identity, name?}`. Returns `{token, room, identity, url}`. |
| `GET /api/records` | List records. In S3 mode, scans the bucket for `vcon-signed.jws`, newest session first. Offline, returns the one fixture record. |
| `GET /api/records/{run_prefix}/full` | Decrypted and verified full vCon. |
| `GET /api/records/{run_prefix}/redacted` | Verified redacted vCon. |
| `GET /api/records/{run_prefix}/audio` | Short-lived presigned GET URL for `<run_prefix>/recording.ogg`. 404 offline or when there is no recording. |

Errors: **404** if an artifact is missing, **409** on a signing-key mismatch. `frontend/`
is mounted at `/`, so `GET /` serves the landing page.

## Offline fixtures

The fixture set in `frontend/fixtures/` is produced by running the real seal core
(`infrastructure/functions/seal/sealing.py`) over a representative enriched vCon, so it
carries the real `redacted` pointer shape (`{uuid, type: 'application/vcon+jws', url:
's3://.../vcon-full.jwe', content_hash: 'sha512-...'}`):

| File | Contents |
|---|---|
| `vcon-full.json` | Decoded full vCon served as the doctor view offline. |
| `vcon-redacted.json` | Decoded redacted vCon served as the analyst view offline. |
| `vcon-signed.jws`, `vcon-full.jwe`, `vcon-redacted.jws` | The sealed artifacts the JSON files were decoded from. |
| `demo-insecure-do-not-use.jwk` | Throwaway demo key, committed on purpose so the JWE and its `content_hash` stay a consistent captured pair. It protects nothing real. |
| `generate_fixtures.py` | The generator. |

Regenerate and re-verify from the repo root:

```bash
python3 frontend/fixtures/generate_fixtures.py
python3 -m pytest tests/test_fixtures.py -q
```

Re-running reuses the committed key but produces new random JWS/JWE bytes, a new vCon
`uuid`, and a new `content_hash`, so the fixtures show up as modified in `git status`.
Commit them only if you mean to refresh the fixtures. Delete the key file first to mint a
brand-new throwaway key.

## Shortcuts

The demo shortcuts in the backend and UI (no authentication, wide-open CORS, scanned
record listing, role-agnostic audio presign, and so on) are listed with their production
counterparts in
[production-considerations.md](production-considerations.md#demo-application).
