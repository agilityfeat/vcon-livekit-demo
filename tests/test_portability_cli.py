"""Offline tests for the portability CLI (the "second tool").

These tests prove the portability claim the same way the CLI does: they load the
bundled sample vCons through the independent ``vcon`` PyPI library and assert the
standard fields come back via the library's accessors. They call the CLI's
importable core (``load_vcon`` / ``build_report``) directly, so no subprocess is
needed and the assertions fail if the CLI stops reading a field.

The ``vcon`` library is an OPTIONAL external dependency, so the whole module is
skipped with ``pytest.importorskip`` when it is absent: the base offline suite
(``PYTHONPATH=src``) still passes without the library installed. When the library
IS on the path (``PYTHONPATH=.vconlib:src``) every assertion runs. Mirrors the
fully-offline, canned-input style of ``tests/test_enrichment.py``.
"""

import os
import sys

import pytest

# Skip the whole module unless the independent vcon library is importable; this
# keeps the base offline suite green when it is not installed.
pytest.importorskip("vcon")

# The CLI lives in viewer/ (alongside the static viewer it documents), which is
# not on PYTHONPATH in the offline test command. Put it on sys.path so the core
# functions can be imported and exercised directly (no subprocess).
_VIEWER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "viewer")
if _VIEWER_DIR not in sys.path:
    sys.path.insert(0, _VIEWER_DIR)

from portability_cli import (  # noqa: E402 - after importorskip + path setup
    PortabilityReport,
    build_report,
    format_report,
    load_vcon,
)

# Resolve the bundled sample fixtures relative to this test file so the tests do
# not depend on the current working directory.
_FULL_SAMPLE = os.path.join(_VIEWER_DIR, "samples", "vcon-full.sample.json")
_REDACTED_SAMPLE = os.path.join(_VIEWER_DIR, "samples", "vcon-redacted.sample.json")

# The known uuids of the bundled full/redacted pair (hand-authored fixtures).
_FULL_UUID = "11111111-1111-4111-8111-111111111111"
_REDACTED_UUID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def full_report() -> PortabilityReport:
    return build_report(load_vcon(_FULL_SAMPLE))


@pytest.fixture
def redacted_report() -> PortabilityReport:
    return build_report(load_vcon(_REDACTED_SAMPLE))


# ----------------------------------------------------------------------------
# The library reads the standard fields back (portability)
# ----------------------------------------------------------------------------


def test_full_sample_standard_fields_read_through_library(full_report):
    # uuid / subject / created_at are read via the library's accessors.
    assert full_report.uuid == _FULL_UUID
    assert full_report.subject == "Patient intake call"
    assert full_report.created_at == "2024-01-01T15:04:05+00:00"


def test_full_sample_exposes_party_and_dialog_counts(full_report):
    # Two parties (patient, agent); five dialog entries (four text turns + the
    # recording dialog object). If the CLI stopped reading parties/dialog these
    # would be zero.
    assert full_report.party_count == 2
    assert full_report.dialog_count == 5


def test_analysis_types_surface_in_pipeline_order(full_report):
    # The deterministic transcript -> entities -> summary order must survive the
    # round-trip through the library's .analysis accessor.
    assert full_report.analysis_types == ["transcript", "entities", "summary"]


# ----------------------------------------------------------------------------
# The redacted <-> full link is readable via .redacted
# ----------------------------------------------------------------------------


def test_full_sample_redacted_link_points_at_redacted_uuid(full_report):
    assert full_report.redacted is not None
    assert full_report.redacted.get("type") == "redacted"
    # The full doc's redacted link points at the redacted sample's uuid.
    assert full_report.redacted_uuid == _REDACTED_UUID
    # And carries the relative sibling URL the static viewer fetches.
    assert full_report.redacted.get("url") == "vcon-redacted.sample.json"


def test_redacted_sample_back_references_full(redacted_report):
    # The redacted doc has its own distinct uuid and a back-reference naming the
    # origin full doc, so the two are visibly the same vCon from either side.
    assert redacted_report.uuid == _REDACTED_UUID
    assert redacted_report.redacted is not None
    assert redacted_report.redacted.get("type") == "redacted_from"
    assert redacted_report.redacted_uuid == _FULL_UUID


def test_pair_links_are_mutually_consistent(full_report, redacted_report):
    # Full points at redacted, redacted points back at full: the standard
    # redacted link ties the pair together both ways.
    assert full_report.redacted_uuid == redacted_report.uuid
    assert redacted_report.redacted_uuid == full_report.uuid


# ----------------------------------------------------------------------------
# is_valid() is reported, never enforced (the demo 0.0.1 shape)
# ----------------------------------------------------------------------------


def test_demo_shape_reported_invalid_but_still_read(full_report):
    # The strict library validation rejects the 0.0.1 demo shape, but that does
    # NOT stop the record from being read: every standard field is present.
    assert full_report.is_valid is False
    assert full_report.validation_errors  # non-empty, surfaced for transparency
    # Reading succeeded despite is_valid() being False.
    assert full_report.uuid == _FULL_UUID
    assert full_report.analysis_types == ["transcript", "entities", "summary"]


# ----------------------------------------------------------------------------
# format_report renders the fields a human sees
# ----------------------------------------------------------------------------


def test_format_report_surfaces_key_fields(full_report):
    text = format_report(full_report)
    assert _FULL_UUID in text
    assert "transcript, entities, summary" in text
    # The redacted link and the acknowledged is_valid() note are both visible.
    assert _REDACTED_UUID in text
    assert "is_valid()" in text


# ----------------------------------------------------------------------------
# Optional sign/verify round-trip (the "signed vCon" story)
# ----------------------------------------------------------------------------


def test_optional_signature_round_trip_verifies():
    from vcon import Vcon

    vcon = load_vcon(_FULL_SAMPLE)
    private_key, public_key = Vcon.generate_key_pair()
    vcon.sign(private_key)
    report = build_report(vcon, verify_public_key=public_key)
    # A signed vCon verifies in this second tool, proving the signed-vCon story.
    assert report.signature_verified is True
