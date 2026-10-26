"""Unit tests for the pure conversation-item -> dialog-turn glue in agent.py.

These tests exercise the conversation-item glue WITHOUT starting a real AgentSession,
using credentials, or touching the network. Importing ``vcon_agent.agent`` pulls
in the (installed) LiveKit SDK, which is fine; the key constraint is that we only
call the pure :func:`handle_item` helper against an in-memory
:class:`VConBuilder`.
"""

from __future__ import annotations

import asyncio

from livekit import api

from vcon_agent.agent import (
    _maybe_start_recording_egress,
    build_egress_request,
    handle_item,
    recording_url,
)
from vcon_agent.vcon import AGENT_INDEX, PATIENT_INDEX, VConBuilder, recording_key


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


# --- Recording egress seam (FEAT-001), fully offline via injected fakes ------


class _FakeFileInfo:
    """Mimics api.FileInfo with a .location the egress result reports."""

    def __init__(self, location):
        self.location = location
        self.filename = location.rsplit("/", 1)[-1]


class _FakeEgressInfo:
    """Mimics api.EgressInfo returned by start_room_composite_egress."""

    def __init__(self, egress_id="EG_fake", file_results=None):
        self.egress_id = egress_id
        self.file_results = file_results or []


class _FakeEgress:
    """Records start_egress calls instead of talking to LiveKit."""

    def __init__(self, info=None):
        self.calls = []
        self._info = info or _FakeEgressInfo()

    async def __call__(self, url, api_key, api_secret, request):
        self.calls.append((url, api_key, api_secret, request))
        return self._info


_LIVE_ENV = {
    "BUCKET_NAME": "my-bucket",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "AKIA_fake",
    "AWS_SECRET_ACCESS_KEY": "secret_fake",
    "LIVEKIT_URL": "wss://example.livekit.cloud",
    "LIVEKIT_API_KEY": "lk_key",
    "LIVEKIT_API_SECRET": "lk_secret",
}


def test_build_egress_request_audio_only_ogg_s3():
    req = build_egress_request(
        room_name="room-abc",
        bucket="my-bucket",
        region="us-east-1",
        filepath="room-abc/ss/ts/recording.ogg",
        access_key="AKIA_fake",
        secret="secret_fake",
    )

    assert req.room_name == "room-abc"
    assert req.audio_only is True
    assert len(req.file_outputs) == 1
    fo = req.file_outputs[0]
    assert fo.file_type == api.EncodedFileType.OGG
    assert fo.filepath == "room-abc/ss/ts/recording.ogg"
    assert fo.s3.bucket == "my-bucket"
    assert fo.s3.region == "us-east-1"
    assert fo.s3.access_key == "AKIA_fake"
    assert fo.s3.secret == "secret_fake"


def test_recording_url_s3_scheme():
    assert recording_url("my-bucket", "a/b/recording.ogg") == (
        "s3://my-bucket/a/b/recording.ogg"
    )


def test_egress_seam_threads_recording_url_into_vcon():
    builder = VConBuilder()
    fake = _FakeEgress()

    url = asyncio.run(
        _maybe_start_recording_egress(
            builder,
            "room-abc",
            "2024-01-01T00:00:00+00:00",
            "1700000000",
            env=_LIVE_ENV,
            start_egress=fake,
        )
    )

    expected_key = recording_key(
        "room-abc", "2024-01-01T00:00:00+00:00", timestamp="1700000000"
    )
    expected_url = f"s3://my-bucket/{expected_key}"
    assert url == expected_url

    # The fake egress was driven once with an audio-only OGG request at the key.
    assert len(fake.calls) == 1
    _u, _k, _s, request = fake.calls[0]
    assert request.audio_only is True
    assert request.file_outputs[0].file_type == api.EncodedFileType.OGG
    assert request.file_outputs[0].filepath == expected_key

    # The vCon carries the recording as an external-reference recording dialog.
    dialog = builder.to_dict()["dialog"]
    assert len(dialog) == 1
    assert dialog[0]["type"] == "recording"
    assert dialog[0]["url"] == expected_url
    assert "body" not in dialog[0]


def test_egress_seam_prefers_reported_location():
    builder = VConBuilder()
    reported = "s3://my-bucket/room-abc/2024-01-01T00:00:00+00:00/1700000000/recording.ogg"
    fake = _FakeEgress(
        info=_FakeEgressInfo(file_results=[_FakeFileInfo(reported)])
    )

    url = asyncio.run(
        _maybe_start_recording_egress(
            builder,
            "room-abc",
            "2024-01-01T00:00:00+00:00",
            "1700000000",
            env=_LIVE_ENV,
            start_egress=fake,
        )
    )
    assert url == reported
    assert builder.to_dict()["dialog"][0]["url"] == reported


def test_egress_skipped_when_bucket_absent():
    builder = VConBuilder()
    fake = _FakeEgress()
    env = dict(_LIVE_ENV)
    del env["BUCKET_NAME"]

    url = asyncio.run(
        _maybe_start_recording_egress(
            builder,
            "room-abc",
            "2024-01-01T00:00:00+00:00",
            "1700000000",
            env=env,
            start_egress=fake,
        )
    )

    assert url is None
    assert fake.calls == []
    # No recording dialog added when egress is skipped for the local demo.
    assert builder.to_dict()["dialog"] == []


def test_egress_skipped_when_livekit_creds_absent():
    builder = VConBuilder()
    fake = _FakeEgress()
    env = dict(_LIVE_ENV)
    del env["LIVEKIT_API_KEY"]

    url = asyncio.run(
        _maybe_start_recording_egress(
            builder,
            "room-abc",
            "2024-01-01T00:00:00+00:00",
            "1700000000",
            env=env,
            start_egress=fake,
        )
    )

    assert url is None
    assert fake.calls == []
    assert builder.to_dict()["dialog"] == []


def test_egress_failure_still_records_intended_url_and_never_raises():
    builder = VConBuilder()

    async def _boom(url, api_key, api_secret, request):
        raise RuntimeError("simulated LiveKit egress failure")

    # Must not raise; the call must survive an egress failure.
    url = asyncio.run(
        _maybe_start_recording_egress(
            builder,
            "room-abc",
            "2024-01-01T00:00:00+00:00",
            "1700000000",
            env=_LIVE_ENV,
            start_egress=_boom,
        )
    )

    expected_key = recording_key(
        "room-abc", "2024-01-01T00:00:00+00:00", timestamp="1700000000"
    )
    expected_url = f"s3://my-bucket/{expected_key}"
    # Even on failure, Spec 3's S3 poll has a key to look for.
    assert url == expected_url
    assert builder.to_dict()["dialog"][0]["url"] == expected_url
