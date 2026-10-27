"""Unit tests for the persistence layer.

No real AWS and no network: boto3 clients are injected as fakes that record
their calls, and the local-file fallback uses pytest's tmp_path.
"""

import json

import pytest

from vcon_agent.persistence import persist_vcon
from vcon_agent.vcon import VConBuilder


class FakeS3Client:
    """Records put_object calls instead of hitting S3."""

    def __init__(self):
        self.put_object_calls = []

    def put_object(self, **kwargs):
        self.put_object_calls.append(kwargs)
        return {"ETag": "fake-etag"}


class FakeSfnClient:
    """Records start_execution calls instead of hitting Step Functions."""

    def __init__(self):
        self.start_execution_calls = []

    def start_execution(self, **kwargs):
        self.start_execution_calls.append(kwargs)
        return {"executionArn": "arn:aws:states:us-east-1:123:execution:fake:run"}


def _sample_doc():
    builder = VConBuilder()
    builder.add_consent(granted=True)
    builder.add_dialog_turn(0, "I have a headache.")
    builder.add_dialog_turn(1, "How long?")
    return builder.to_dict()


def test_s3_put_and_start_execution_with_injected_fakes():
    doc = _sample_doc()
    s3 = FakeS3Client()
    sfn = FakeSfnClient()
    env = {
        "BUCKET_NAME": "my-bucket",
        "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123:stateMachine:enrich",
        "AWS_REGION": "us-east-1",
    }

    result = persist_vcon(
        doc,
        room_name="room-xyz",
        session_start_iso="2024-01-01T00:00:00+00:00",
        env=env,
        s3_client=s3,
        sfn_client=sfn,
    )

    # put_object called exactly once with the exact unique-key path.
    assert len(s3.put_object_calls) == 1
    call = s3.put_object_calls[0]
    assert call["Bucket"] == "my-bucket"
    assert call["Key"] == (
        "room-xyz/2024-01-01T00:00:00+00:00/vcon-skeleton.json"
    )
    assert call["ContentType"] == "application/json"
    # Body round-trips back to the vCon doc.
    assert json.loads(call["Body"].decode("utf-8")) == doc

    # start_execution called exactly once with the {"vcon": <doc>} envelope.
    assert len(sfn.start_execution_calls) == 1
    sfn_call = sfn.start_execution_calls[0]
    assert sfn_call["stateMachineArn"] == env["STATE_MACHINE_ARN"]
    parsed_input = json.loads(sfn_call["input"])
    assert parsed_input["vcon"] == doc

    # Result reflects the S3 destination and triggered enrichment.
    assert result.destination == "s3"
    assert result.location == (
        "s3://my-bucket/room-xyz/2024-01-01T00:00:00+00:00/"
        "vcon-skeleton.json"
    )
    assert result.enrichment_triggered is True
    assert result.execution_arn == (
        "arn:aws:states:us-east-1:123:execution:fake:run"
    )


def test_s3_without_state_machine_skips_enrichment():
    doc = _sample_doc()
    s3 = FakeS3Client()
    sfn = FakeSfnClient()
    env = {"BUCKET_NAME": "my-bucket", "AWS_REGION": "us-east-1"}

    result = persist_vcon(
        doc,
        room_name="room-xyz",
        session_start_iso="2024-01-01T00:00:00+00:00",
        env=env,
        s3_client=s3,
        sfn_client=sfn,
    )

    assert len(s3.put_object_calls) == 1
    assert len(sfn.start_execution_calls) == 0
    assert result.destination == "s3"
    assert result.enrichment_triggered is False


def test_local_file_fallback_when_bucket_unset(tmp_path):
    doc = _sample_doc()
    s3 = FakeS3Client()
    sfn = FakeSfnClient()
    # BUCKET_NAME absent -> local file, no AWS calls, no enrichment.
    env = {}

    result = persist_vcon(
        doc,
        room_name="room-xyz",
        session_start_iso="2024-01-01T00:00:00+00:00",
        env=env,
        s3_client=s3,
        sfn_client=sfn,
        local_output_dir=str(tmp_path),
    )

    expected_path = (
        tmp_path
        / "room-xyz"
        / "2024-01-01T00:00:00+00:00"
        / "vcon-skeleton.json"
    )
    assert expected_path.is_file()
    assert json.loads(expected_path.read_text(encoding="utf-8")) == doc

    # No AWS client was touched and enrichment was not triggered.
    assert s3.put_object_calls == []
    assert sfn.start_execution_calls == []
    assert result.destination == "local"
    assert result.location == str(expected_path)
    assert result.enrichment_triggered is False
    assert result.key == (
        "room-xyz/2024-01-01T00:00:00+00:00/vcon-skeleton.json"
    )


class FailingS3Client:
    """Raises on put_object to simulate a transient S3 outage at flush time."""

    def __init__(self):
        self.put_object_calls = []

    def put_object(self, **kwargs):
        self.put_object_calls.append(kwargs)
        raise RuntimeError("simulated S3 failure")


class FailingSfnClient:
    """Raises on start_execution to simulate a StartExecution throttle."""

    def __init__(self):
        self.start_execution_calls = []

    def start_execution(self, **kwargs):
        self.start_execution_calls.append(kwargs)
        raise RuntimeError("simulated StartExecution throttle")


def test_s3_put_failure_salvages_to_local(tmp_path):
    """A failing S3 PUT must not lose the vCon: it is written locally instead."""
    doc = _sample_doc()
    s3 = FailingS3Client()
    env = {"BUCKET_NAME": "my-bucket", "AWS_REGION": "us-east-1"}

    result = persist_vcon(
        doc,
        room_name="room-xyz",
        session_start_iso="2024-01-01T00:00:00+00:00",
        env=env,
        s3_client=s3,
        local_output_dir=str(tmp_path),
    )

    # The S3 PUT was attempted and raised.
    assert len(s3.put_object_calls) == 1

    # The document was salvaged to the identical run-key path locally.
    expected_path = (
        tmp_path
        / "room-xyz"
        / "2024-01-01T00:00:00+00:00"
        / "vcon-skeleton.json"
    )
    assert expected_path.is_file()
    assert json.loads(expected_path.read_text(encoding="utf-8")) == doc

    assert result.destination == "local"
    assert result.location == str(expected_path)
    assert result.enrichment_triggered is False
    assert result.s3_fallback_to_local is True
    assert result.key == (
        "room-xyz/2024-01-01T00:00:00+00:00/vcon-skeleton.json"
    )


def test_start_execution_failure_salvages_to_local(tmp_path):
    """A failing StartExecution (after a successful PUT) still salvages locally."""
    doc = _sample_doc()
    s3 = FakeS3Client()
    sfn = FailingSfnClient()
    env = {
        "BUCKET_NAME": "my-bucket",
        "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123:stateMachine:enrich",
        "AWS_REGION": "us-east-1",
    }

    result = persist_vcon(
        doc,
        room_name="room-xyz",
        session_start_iso="2024-01-01T00:00:00+00:00",
        env=env,
        s3_client=s3,
        sfn_client=sfn,
        local_output_dir=str(tmp_path),
    )

    # PUT succeeded, StartExecution was attempted and raised.
    assert len(s3.put_object_calls) == 1
    assert len(sfn.start_execution_calls) == 1

    expected_path = (
        tmp_path
        / "room-xyz"
        / "2024-01-01T00:00:00+00:00"
        / "vcon-skeleton.json"
    )
    assert expected_path.is_file()
    assert json.loads(expected_path.read_text(encoding="utf-8")) == doc

    assert result.destination == "local"
    assert result.enrichment_triggered is False
    assert result.s3_fallback_to_local is True


def test_local_fallback_via_monkeypatched_cwd(tmp_path, monkeypatch):
    """Default local dir is vcon-output/ under the cwd when no dir is given."""
    doc = _sample_doc()
    monkeypatch.chdir(tmp_path)

    result = persist_vcon(
        doc,
        room_name="room-xyz",
        session_start_iso="2024-01-01T00:00:00+00:00",
        env={},
    )

    expected_path = (
        tmp_path
        / "vcon-output"
        / "room-xyz"
        / "2024-01-01T00:00:00+00:00"
        / "vcon-skeleton.json"
    )
    assert expected_path.is_file()
    assert result.destination == "local"
