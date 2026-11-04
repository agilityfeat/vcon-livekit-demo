# vCon + LiveKit demo

A demo that pairs a [LiveKit](https://livekit.io/) voice agent with an AWS enrichment
pipeline. A patient places an intake call to a bot from the browser; the call is
recorded and turned into a [vCon](https://vcon.dev/) document, enriched (transcription,
medical entities, summary), signed, encrypted and redacted, and then reviewed in a small
web app by a doctor (full record) or an analyst (redacted record).

```
call (browser + LiveKit) -> record (audio egress to S3) -> enrich (Step Functions:
Transcribe Medical, Comprehend Medical, Bedrock) -> seal (sign, encrypt, redact) -> view (FastAPI + web UI)
```

> **Compliance.** This PoC demonstrates the mechanisms vCon provides for consent,
> integrity, and redaction. It is not a HIPAA-certified deployment and must not be used
> with real PHI.

## Prerequisites

- **Recommended:** [Nix](https://nixos.org/download) (flakes enabled) and
  [direnv](https://direnv.net/). Run `direnv allow` once (or `nix develop`) and you get
  Python 3.12, the native libraries, the SAM CLI, and a `.venv` with everything
  installed.
- **Without Nix:** Python 3.12 and a virtualenv in the repo root:

  ```bash
  python3 -m venv .venv && . .venv/bin/activate
  pip install -e '.[app,dev]'
  ```

- **Only for deploying to AWS:** Docker (running) and the AWS SAM CLI (included in the Nix
  shell), plus AWS credentials.

More detail is in [docs/getting-started.md](docs/getting-started.md).

## Quickstart A: browse a record offline

No credentials, no AWS, no call. The backend serves a committed sample record.

```bash
cp .env.example .env     # leave BUCKET_NAME empty (or skip .env entirely)
python -m uvicorn vcon_agent.app.main:app --reload
```

Open <http://127.0.0.1:8000/>, choose **Doctor** (full record) or **Analyst** (redacted
record), and sign in (no credentials are checked). The sample record is listed on the
dashboard. The patient call page needs real LiveKit credentials to connect.

## Quickstart B: live call end to end

1. **Create a signing key.** The Seal Lambda and the backend must share it:

   ```bash
   python -c "from jwcrypto import jwk; print(jwk.JWK.generate(kty='RSA', size=2048).export(private_key=True))"
   ```

2. **Enable Bedrock model access** once per account (Anthropic needs a first-use form):
   [Bedrock model access](docs/infrastructure.md#bedrock-model-access).
3. **Deploy the AWS stack**, passing the key as `VconSigningPrivateJwk`:

   ```bash
   sam build --use-container --template infrastructure/template.yaml
   sam deploy --guided   # add VconSigningPrivateJwk='<the key>' to the parameter overrides
   ```

4. **Fill in `.env`.** Copy `.env.example`, paste the stack Outputs (`BucketName` to
   `BUCKET_NAME`, `StateMachineArn` to `STATE_MACHINE_ARN`, `Region` to `AWS_REGION`), the
   **same** key as `VCON_SIGNING_PRIVATE_JWK`, and your LiveKit, Deepgram, ElevenLabs
   (`ELEVEN_API_KEY`), and OpenAI keys. Details:
   [docs/infrastructure.md](docs/infrastructure.md#read-the-outputs-into-env) and
   [docs/getting-started.md](docs/getting-started.md#configuration-env).
5. **Run the agent** in one terminal:

   ```bash
   python -m vcon_agent.agent dev
   ```

6. **Run the app** in another, open <http://127.0.0.1:8000/>, click **Start an intake
   call**, and talk to the bot:

   ```bash
   python -m uvicorn vcon_agent.app.main:app --reload
   ```

When you end the call, the pipeline runs in AWS; the record then appears on the doctor
and analyst dashboards. Tests run offline with `python3 -m pytest tests/ -q`.

## Documentation

| Doc | What it covers |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Full dev environment, `.env` variables per mode, running things and tests. |
| [docs/architecture.md](docs/architecture.md) | Components and data flow. |
| [docs/intake-agent.md](docs/intake-agent.md) | The voice agent, the live-built vCon, persistence, call recording. |
| [docs/enrichment-pipeline.md](docs/enrichment-pipeline.md) | The Step Functions pipeline and its steps. |
| [docs/sign-encrypt-redact.md](docs/sign-encrypt-redact.md) | Signing, encryption, redaction, and their limits. |
| [docs/infrastructure.md](docs/infrastructure.md) | Deploy runbook, Bedrock prerequisite, S3 layout, teardown. |
| [docs/validation.md](docs/validation.md) | Offline checks and the live end-to-end check. |
| [docs/demo-app.md](docs/demo-app.md) | The web app, its API, offline mode, and fixtures. |
| [docs/production-considerations.md](docs/production-considerations.md) | Every demo shortcut and its production counterpart. |
