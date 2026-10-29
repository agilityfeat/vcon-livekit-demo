"""Dependency-free configuration helpers shared across the package.

This is a leaf module: it imports only the standard library and
``python-dotenv``. Both the LiveKit agent and the FastAPI backend import
:func:`load_env` from here so the backend can read the local ``.env`` WITHOUT
transitively importing the LiveKit stack that lives in ``agent.py``.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import find_dotenv, load_dotenv


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
    # src/vcon_agent/config.py -> repo root is three parents up.
    project_root_env = Path(__file__).resolve().parents[2] / ".env"
    if project_root_env.is_file():
        load_dotenv(project_root_env)
        return
    # Fall back to searching upward from the current working directory.
    load_dotenv(find_dotenv())
