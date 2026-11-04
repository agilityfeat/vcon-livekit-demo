# Getting started

The full development environment for the repo: tooling, the `.env` file, and
running the tests. For the short "run the demo" path, see the
[README](../README.md).

## Prerequisites

| Tool | Needed for |
|---|---|
| [Nix](https://nixos.org/download) (flakes enabled) + [direnv](https://direnv.net/) | Recommended: one command gives you the whole toolchain. |
| Python 3.12 | The version the Nix shell uses and the Lambda runtime (`python3.12`). `pyproject.toml` declares `requires-python >=3.9`; the test suite also passes on 3.11. |
| Docker | Only for `sam build --use-container` (deploying the Seal Lambda). Not provided by the Nix shell. |
| AWS SAM CLI | Only for deploying. Provided by the Nix shell. |
| AWS credentials | Only for deploying and for the live pipeline. |

## Development environment

### Option A: Nix + direnv (recommended)

The committed `flake.nix` is a **repo-wide** dev shell, not just for the agent.
It provides:

- Python 3.12;
- the native libraries the pip-installed wheels load at runtime (`libstdc++`,
  `zlib`, and `portaudio` for LiveKit console mode), exported on
  `LD_LIBRARY_PATH`;
- the `aws-sam-cli` (`sam`) CLI;
- a single root `.venv`, created on first entry with
  `pip install -e '.[app,dev]'` (the `app` extra is the FastAPI backend, the
  `dev` extra is pytest) and activated on every entry.

The shell hook also unsets `PYTHONPATH` and sets `PYTHONNOUSERSITE=1`. The Nix
`sam` package pulls its own site-packages onto `PYTHONPATH`, which would
otherwise leak into the venv and leave it half installed.

With Nix and direnv installed, run `direnv allow` once in the repo root; from
then on `cd`-ing into the project drops you into the shell. Without direnv, run
`nix develop` for the same shell. The first entry also generates `flake.lock` if
it is missing.

If you change the extras in `pyproject.toml`, run `rm -rf .venv` once so the
next entry reinstalls them.

### Option B: manual virtualenv

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[app,dev]'
```

Without Nix you also need to supply the native libraries yourself (notably
`libstdc++` and, for `console` mode, PortAudio) and install the
[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
if you intend to deploy. Use `pip install -e '.[dev]'` only if you do not need
the demo backend.

## Configuration (`.env`)

```bash
cp .env.example .env
```

`.env` is gitignored. Both the agent and the backend load it from the repo root
at startup (`vcon_agent.config.load_env`). Variables already set in the real
environment win over the file.

| Variable | Used by | Notes |
|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | agent, backend (`/api/token`), egress | From your LiveKit Cloud project or a self-hosted server. |
| `DEEPGRAM_API_KEY` | agent | Speech-to-text. |
| `ELEVEN_API_KEY` | agent | Text-to-speech. This is the name the `livekit-plugins-elevenlabs` plugin reads (`os.environ.get("ELEVEN_API_KEY")`); `ELEVENLABS_API_KEY` is **not** read. |
| `OPENAI_API_KEY` | agent | The LLM driving the scripted conversation. |
| `BUCKET_NAME` | agent, backend | Stack output `BucketName`. Unset or empty means local mode (see below). |
| `STATE_MACHINE_ARN` | agent | Stack output `StateMachineArn`. If unset, the agent uploads but does not start enrichment. |
| `AWS_REGION` | agent, backend | Stack output `Region`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | agent (egress) | Optional. Used as the S3 upload credentials for LiveKit Egress; otherwise egress falls back to the instance role. The agent's and backend's own boto3 clients use the standard AWS credential chain. |
| `VCON_SIGNING_PRIVATE_JWK` | backend | JWK JSON or PEM. Must equal the Seal Lambda's `VconSigningPrivateJwk`. If unset, the backend generates a throwaway key, which is fine offline and useless against real records. |

### What you need for each mode

| Mode | What it does | Required variables |
|---|---|---|
| **Offline viewer** | Backend serves the committed fixture record. No call, no AWS. | None. `BUCKET_NAME` must be unset, empty, or commented out. |
| **Standalone agent** | Agent holds a real call and writes the vCon to `vcon-output/<room>/<session_start>/vcon-skeleton.json`. No recording, no enrichment. | `LIVEKIT_*`, `DEEPGRAM_API_KEY`, `ELEVEN_API_KEY`, `OPENAI_API_KEY`. `BUCKET_NAME` unset. |
| **Full live** | Call, recording to S3, enrichment, seal, and review in the app. | Everything above plus `BUCKET_NAME`, `STATE_MACHINE_ARN`, `AWS_REGION`, `VCON_SIGNING_PRIVATE_JWK`, and AWS credentials. |

`vcon-output/` is created relative to the working directory the agent runs in.
It is not in `.gitignore`, so do not commit it.

Generate a signing key with:

```bash
python -c "from jwcrypto import jwk; print(jwk.JWK.generate(kty='RSA', size=2048).export(private_key=True))"
```

## Running things

Run everything from the repo root with the venv active.

```bash
# Intake agent: talk to it from your terminal, or connect it to a LiveKit room.
python -m vcon_agent.agent console
python -m vcon_agent.agent dev

# Demo backend + frontend on http://127.0.0.1:8000/
python -m uvicorn vcon_agent.app.main:app --reload
```

`PYTHONPATH=src:infrastructure/functions/seal` is **not** needed. The editable
install puts `vcon_agent` on the path, and `vcon_agent/app/records.py` adds
`infrastructure/functions/seal/` to `sys.path` itself so it imports the same
`sealing.py` the Lambda ships. Use `python -m uvicorn` (not a bare `uvicorn`) so
the server runs under the interpreter of your active virtualenv.

The live voice path needs real LiveKit, Deepgram, ElevenLabs, and OpenAI
credentials, so it cannot be exercised offline. See
[intake-agent.md](intake-agent.md) and [demo-app.md](demo-app.md) for what each
process does.

## Running the tests

The suite needs no network and no credentials. The vCon assembly, persistence,
enrichment shaping, sealing, backend, and fixtures are tested with fakes, and the
inline Lambda bodies in `infrastructure/template.yaml` are checked by extracting
and compiling them.

```bash
python3 -m pytest tests/ -q
```

No `PYTHONPATH` is needed: the tests that import the seal core add
`infrastructure/functions/seal/` to `sys.path` themselves. Tests that need
`jwcrypto` use `pytest.importorskip` and skip cleanly if it is missing, and the
tests that execute the inline Lambda bodies that import `boto3` skip if `boto3`
is missing.

For the template checks (`cfn-lint`, `sam validate`) and the live end-to-end
check, see [validation.md](validation.md).

## Where things are

| Path | What lives there |
|---|---|
| `src/vcon_agent/` | Agent, vCon builder, persistence, enrichment helpers, config. |
| `src/vcon_agent/app/` | Demo backend (`main.py` routes, `records.py` core, `livekit_tokens.py`). |
| `infrastructure/` | CloudFormation/SAM template, the packaged Seal Lambda, validation scripts. |
| `frontend/` | Static UI served by the backend, plus `fixtures/`. |
| `tests/` | Offline test suite. |
| `docs/` | This documentation. |
