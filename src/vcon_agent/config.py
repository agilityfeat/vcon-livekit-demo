"""Dependency-free configuration helpers shared across the package.

This is a leaf module: at import time it touches only the standard library.
``python-dotenv`` is imported lazily inside :func:`load_env`, so importing this
module for its ENV_* name constants needs no third-party dependency. Both the
LiveKit agent and the FastAPI backend import :func:`load_env` from here so the
backend can read the local ``.env`` WITHOUT transitively importing the LiveKit
stack that lives in ``agent.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Environment-variable NAMES shared across the agent, persistence, and app
# layers. They live here, next to load_env, as the single source of truth so the
# three layers reference identical keys instead of maintaining duplicate
# definitions that could drift. Each value is the literal environment-variable
# key read from os.environ / a loaded .env.
ENV_BUCKET_NAME = "BUCKET_NAME"
ENV_AWS_REGION = "AWS_REGION"
ENV_AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
ENV_AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
ENV_STATE_MACHINE_ARN = "STATE_MACHINE_ARN"
ENV_LIVEKIT_URL = "LIVEKIT_URL"
ENV_LIVEKIT_API_KEY = "LIVEKIT_API_KEY"
ENV_LIVEKIT_API_SECRET = "LIVEKIT_API_SECRET"
ENV_PRIVATE_JWK = "VCON_SIGNING_PRIVATE_JWK"

# Module-level handles for the python-dotenv functions, left unbound at import
# time so this module pulls in no third-party dependency merely to expose the
# ENV_* constants. load_env binds them from python-dotenv on first use. They are
# declared here (rather than created lazily) so they remain real, patchable
# vcon_agent.config attributes for tests that monkeypatch the .env lookup.
load_dotenv = None
find_dotenv = None


def load_env() -> None:
    """Load a local ``.env`` into ``os.environ`` early and cwd-independently.

    The LiveKit worker reads ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` /
    ``LIVEKIT_API_SECRET`` to connect and REGISTER, which happens inside
    ``cli.run_app(...)`` in :func:`main` -- *before* any job (so before
    :func:`entrypoint`) ever runs. The ``.env`` must therefore be loaded before
    the worker starts, not per-job, otherwise ``python -m vcon_agent.agent dev``
    fails at registration with "ws_url is required, or set LIVEKIT_URL ...".

    Resolution is cwd-independent so the process can be launched from any
    directory: we first anchor to the project root relative to this file (the
    repo root holds ``.env`` / ``.env.example``), then fall back to
    :func:`find_dotenv`, which searches upward from the cwd. If neither finds a
    file, ``load_dotenv`` is a harmless no-op and the agent still starts from the
    real environment (container-injected vars, a parent shell, etc.).

    We do NOT pass ``override=True``: variables already set in the real
    environment take precedence over the local ``.env`` file.
    """
    # Import python-dotenv lazily so importing this module for its ENV_* name
    # constants needs no third-party dependency; dotenv is only required when
    # .env loading actually runs. Resolve through this module's globals so a
    # test that monkeypatches vcon_agent.config.find_dotenv / load_dotenv wins;
    # otherwise bind the real python-dotenv functions on first use.
    module = sys.modules[__name__]
    load_dotenv = module.load_dotenv
    find_dotenv = module.find_dotenv
    if load_dotenv is None or find_dotenv is None:
        import dotenv

        if load_dotenv is None:
            load_dotenv = dotenv.load_dotenv
        if find_dotenv is None:
            find_dotenv = dotenv.find_dotenv

    # src/vcon_agent/config.py -> repo root is three parents up.
    project_root_env = Path(__file__).resolve().parents[2] / ".env"
    if project_root_env.is_file():
        load_dotenv(project_root_env)
        return
    # Fall back to searching upward from the current working directory.
    load_dotenv(find_dotenv())
