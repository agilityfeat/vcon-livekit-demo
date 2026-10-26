"""Unit tests for the pure conversation-item -> dialog-turn glue in agent.py.

These tests exercise the FEAT-003 glue WITHOUT starting a real AgentSession,
using credentials, or touching the network. Importing ``vcon_agent.agent`` pulls
in the (installed) LiveKit SDK, which is fine; the key constraint is that we only
call the pure :func:`handle_item` helper against an in-memory
:class:`VConBuilder`.
"""

from __future__ import annotations

from vcon_agent.agent import handle_item
from vcon_agent.vcon import AGENT_INDEX, PATIENT_INDEX, VConBuilder


def _consented_builder() -> VConBuilder:
    builder = VConBuilder()
    builder.add_consent(granted=True)
    return builder


def test_user_item_maps_to_patient_party_zero():
    builder = _consented_builder()

    appended = handle_item(builder, "user", "I have a headache.")

    assert appended is True
    dialog = builder.to_dict()["dialog"]
    assert len(dialog) == 1
    assert dialog[0]["party"] == PATIENT_INDEX == 0
    assert dialog[0]["body"] == "I have a headache."


def test_assistant_item_maps_to_agent_party_one():
    builder = _consented_builder()

    appended = handle_item(builder, "assistant", "How long has that lasted?")

    assert appended is True
    dialog = builder.to_dict()["dialog"]
    assert len(dialog) == 1
    assert dialog[0]["party"] == AGENT_INDEX == 1
    assert dialog[0]["body"] == "How long has that lasted?"


def test_sequence_appends_in_order_on_correct_parties():
    builder = _consented_builder()

    items = [
        ("assistant", "Do you consent to recording?"),
        ("user", "Yes, I consent."),
        ("assistant", "What brings you in today?"),
        ("user", "I have a sore throat."),
    ]
    for role, text in items:
        assert handle_item(builder, role, text) is True

    dialog = builder.to_dict()["dialog"]
    assert [turn["party"] for turn in dialog] == [1, 0, 1, 0]
    assert [turn["body"] for turn in dialog] == [text for _role, text in items]


def test_non_dialog_roles_are_skipped():
    builder = _consented_builder()

    assert handle_item(builder, "system", "You are an intake agent.") is False
    assert handle_item(builder, "developer", "debug note") is False

    assert builder.to_dict()["dialog"] == []


def test_empty_or_whitespace_text_is_skipped():
    builder = _consented_builder()

    assert handle_item(builder, "user", "") is False
    assert handle_item(builder, "assistant", "   ") is False
    assert handle_item(builder, "user", None) is False

    assert builder.to_dict()["dialog"] == []


def test_role_is_case_and_whitespace_tolerant():
    builder = _consented_builder()

    assert handle_item(builder, "  USER  ", "hi") is True
    assert handle_item(builder, "Assistant", "hello") is True

    dialog = builder.to_dict()["dialog"]
    assert [turn["party"] for turn in dialog] == [PATIENT_INDEX, AGENT_INDEX]
