"""Offline unit tests for the dependency-free config helper.

``vcon_agent.config`` is a leaf module: its only third-party dependency is
``python-dotenv`` (no LiveKit, no boto3). To keep these tests runnable fully
offline -- even where ``python-dotenv`` itself is not installed -- we install a
small fake ``dotenv`` module into ``sys.modules`` BEFORE importing the config
module (mirroring the ``sys.path`` shim / ``importorskip`` idioms the rest of
the suite uses to stay self-contained). The fake faithfully reproduces the one
behaviour under test: ``load_dotenv`` reads ``KEY=VALUE`` lines and, with the
default ``override=False``, does NOT clobber variables already in the
environment.

The fake is only a stand-in for the file reader; the branching logic of
``load_env`` (project-root ``.env`` first, then ``find_dotenv`` fallback, then a
harmless no-op) is the real code under test.
"""

import os
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Fake python-dotenv: used only when the real package is not importable so the
# tests stay offline. When the real package IS present we leave it in place.
# ---------------------------------------------------------------------------
if "dotenv" not in sys.modules:
    try:  # pragma: no cover - exercised implicitly depending on environment
        import dotenv  # noqa: F401
    except ModuleNotFoundError:
        fake = types.ModuleType("dotenv")

        def _fake_load_dotenv(dotenv_path=None, override=False):
            if not dotenv_path or not os.path.isfile(dotenv_path):
                return False
            with open(dotenv_path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if not override and key in os.environ:
                        continue
                    os.environ[key] = value
            return True

        def _fake_find_dotenv():  # pragma: no cover - overridden per test
            return ""

        fake.load_dotenv = _fake_load_dotenv
        fake.find_dotenv = _fake_find_dotenv
        sys.modules["dotenv"] = fake

from vcon_agent.config import load_env  # noqa: E402


def test_import_is_dependency_free():
    """The module imports cleanly and exposes a callable ``load_env``.

    A LiveKit or boto3 import creeping into ``config.py`` would make this import
    fail wherever those heavy packages are unavailable (the whole point of the
    leaf module), so a clean import here is itself the assertion.
    """
    assert callable(load_env)


def test_load_env_sets_unset_variable(tmp_path, monkeypatch):
    """A variable absent from the environment is loaded from the ``.env`` file."""
    env_file = tmp_path / ".env"
    env_file.write_text("VCON_TEST_FROM_FILE=from_file\n")

    monkeypatch.delenv("VCON_TEST_FROM_FILE", raising=False)
    # Drive the find_dotenv fallback branch at a temp file so we never read or
    # write the real repo-root .env.
    monkeypatch.setattr("vcon_agent.config.find_dotenv", lambda: str(env_file))

    load_env()

    assert os.environ["VCON_TEST_FROM_FILE"] == "from_file"


def test_load_env_does_not_override_existing_variable(tmp_path, monkeypatch):
    """An already-set variable wins over the ``.env`` file (``override=False``)."""
    env_file = tmp_path / ".env"
    env_file.write_text("VCON_TEST_PRESET=from_file\n")

    monkeypatch.setenv("VCON_TEST_PRESET", "from_env")
    monkeypatch.setattr("vcon_agent.config.find_dotenv", lambda: str(env_file))

    load_env()

    assert os.environ["VCON_TEST_PRESET"] == "from_env"
