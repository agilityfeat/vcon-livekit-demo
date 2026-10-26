"""Unit tests for the framework-independent incremental vCon model."""

import uuid as uuid_module

import pytest

from vcon_agent.vcon import (
    AGENT_INDEX,
    PATIENT_INDEX,
    VCON_VERSION,
    VConBuilder,
    recording_key,
    role_to_index,
    run_key,
)


def _is_uuid(value):
    try:
        uuid_module.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def test_skeleton_has_version_and_uuid():
    doc = VConBuilder().to_dict()
    assert doc["vcon"] == "0.0.1"
    assert doc["vcon"] == VCON_VERSION
    assert _is_uuid(doc["uuid"])
    assert doc["subject"]
    assert doc["created_at"]


def test_skeleton_starts_empty_dialog_and_analysis():
    doc = VConBuilder().to_dict()
    assert doc["dialog"] == []
    assert doc["analysis"] == []
    assert doc["attachments"] == []


def test_parties_index_zero_patient_one_agent():
    doc = VConBuilder().to_dict()
    assert doc["parties"][PATIENT_INDEX]["role"] == "patient"
    assert doc["parties"][AGENT_INDEX]["role"] == "agent"
    assert PATIENT_INDEX == 0
    assert AGENT_INDEX == 1


def test_add_consent_writes_attachment_referencing_patient():
    builder = VConBuilder()
    entry = builder.add_consent(granted=True)
    doc = builder.to_dict()

    assert len(doc["attachments"]) == 1
    assert doc["attachments"][0] is entry
    assert entry["type"] == "consent"
    assert entry["party"] == PATIENT_INDEX
    assert entry["granted"] is True
    assert entry["script"]
    assert entry["timestamp"]


def test_add_consent_records_granted_false():
    builder = VConBuilder()
    entry = builder.add_consent(granted=False)
    assert entry["granted"] is False
    assert isinstance(entry["granted"], bool)


def test_consent_captured_before_any_dialog():
    builder = VConBuilder()
    assert builder.consent_recorded is False
    # Dialog before consent is rejected: enforces the ordering requirement.
    with pytest.raises(RuntimeError):
        builder.add_dialog_turn(PATIENT_INDEX, "I have a headache.")

    builder.add_consent(granted=True)
    assert builder.consent_recorded is True
    # Consent is recorded ahead of any clinical content in the document.
    doc = builder.to_dict()
    assert doc["attachments"][0]["type"] == "consent"
    assert doc["dialog"] == []


def test_dialog_turns_append_in_order_referencing_party_indices():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(PATIENT_INDEX, "I have a headache.")
    builder.add_dialog_turn(AGENT_INDEX, "How long has that been going on?")
    builder.add_dialog_turn(PATIENT_INDEX, "Two days.")

    dialog = builder.to_dict()["dialog"]
    assert [d["party"] for d in dialog] == [0, 1, 0]
    assert [d["body"] for d in dialog] == [
        "I have a headache.",
        "How long has that been going on?",
        "Two days.",
    ]
    for d in dialog:
        assert d["type"] == "text"
        assert d["mimetype"] == "text/plain"
        assert d["start"]
        # Dialog references the party index, not an embedded identity.
        assert "name" not in d
        assert isinstance(d["party"], int)


def test_add_dialog_turn_rejects_bad_index():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    with pytest.raises(ValueError):
        builder.add_dialog_turn(2, "nope")


def test_role_to_index_mapping():
    assert role_to_index("user") == 0
    assert role_to_index("patient") == 0
    assert role_to_index("assistant") == 1
    assert role_to_index("agent") == 1
    # Case-insensitive / whitespace tolerant.
    assert role_to_index("USER") == 0
    assert role_to_index("  Assistant ") == 1


def test_role_to_index_unknown_raises():
    with pytest.raises(ValueError):
        role_to_index("system")


def test_add_dialog_turn_by_role():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn_by_role("user", "hello")
    builder.add_dialog_turn_by_role("assistant", "hi")
    dialog = builder.to_dict()["dialog"]
    assert [d["party"] for d in dialog] == [0, 1]


def test_run_key_layout_exact():
    key = run_key("room-abc", "2024-01-01T00:00:00+00:00", timestamp="1700000000")
    assert key == "room-abc/2024-01-01T00:00:00+00:00/1700000000/vcon-skeleton.json"


def test_run_key_defaults_timestamp():
    key = run_key("room-abc", "2024-01-01T00:00:00+00:00")
    assert key.startswith("room-abc/2024-01-01T00:00:00+00:00/")
    assert key.endswith("/vcon-skeleton.json")


def test_recording_key_layout_exact():
    key = recording_key("room-abc", "2024-01-01T00:00:00+00:00", timestamp="1700000000")
    assert key == "room-abc/2024-01-01T00:00:00+00:00/1700000000/recording.ogg"


def test_recording_key_defaults_timestamp():
    key = recording_key("room-abc", "2024-01-01T00:00:00+00:00")
    assert key.startswith("room-abc/2024-01-01T00:00:00+00:00/")
    assert key.endswith("/recording.ogg")


def test_recording_key_and_run_key_share_run_prefix():
    # Given the SAME (room, session_start, timestamp) triple, recording.ogg and
    # vcon-skeleton.json must live under the identical run prefix.
    room = "room-abc"
    session_start = "2024-01-01T00:00:00+00:00"
    ts = "1700000000"
    rec = recording_key(room, session_start, timestamp=ts)
    vcon = run_key(room, session_start, timestamp=ts)

    assert rec.rsplit("/", 1)[0] == vcon.rsplit("/", 1)[0]
    assert rec.endswith("/recording.ogg")
    assert vcon.endswith("/vcon-skeleton.json")


def test_add_recording_dialog_external_reference_no_inline_bytes():
    builder = VConBuilder()
    url = "s3://my-bucket/room-abc/2024-01-01T00:00:00+00:00/1700000000/recording.ogg"
    entry = builder.add_recording_dialog(url)

    doc = builder.to_dict()
    assert len(doc["dialog"]) == 1
    assert doc["dialog"][0] is entry

    assert entry["type"] == "recording"
    # External reference: the url locates the audio, bytes are never inlined.
    assert entry["url"] == url
    assert entry["mimetype"] == "audio/ogg"
    assert entry["start"]
    # No inline audio body/payload of any kind.
    assert "body" not in entry
    assert "payload" not in entry
    # The recording dialog is whole-call audio, not a party turn.
    assert "party" not in entry


def test_add_recording_dialog_does_not_require_consent_or_party():
    # Unlike add_dialog_turn, a recording dialog is whole-call audio and does not
    # require consent ordering or a party index.
    builder = VConBuilder()
    assert builder.consent_recorded is False
    entry = builder.add_recording_dialog("s3://b/k/recording.ogg")
    assert entry["type"] == "recording"
    assert "party" not in entry


def test_add_recording_dialog_custom_mimetype_and_filename():
    builder = VConBuilder()
    entry = builder.add_recording_dialog(
        "s3://b/k/recording.ogg",
        start="2024-01-01T00:00:00+00:00",
        mimetype="audio/ogg",
        filename="recording.ogg",
    )
    assert entry["start"] == "2024-01-01T00:00:00+00:00"
    assert entry["mimetype"] == "audio/ogg"
    assert entry["filename"] == "recording.ogg"


def test_add_recording_dialog_appends_after_turns():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(PATIENT_INDEX, "I have a headache.")
    builder.add_recording_dialog("s3://b/k/recording.ogg")

    dialog = builder.to_dict()["dialog"]
    assert [d["type"] for d in dialog] == ["text", "recording"]
