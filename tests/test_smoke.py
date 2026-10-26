"""Smoke test: prove the vcon_agent package installs and imports cleanly."""

import vcon_agent


def test_version_is_non_empty_string():
    assert isinstance(vcon_agent.__version__, str)
    assert vcon_agent.__version__ != ""
