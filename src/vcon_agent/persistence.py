"""Persistence layer for the finished vCon skeleton.

Framework-independent (no LiveKit) and unit-testable with no live network: the
boto3 S3 and Step Functions clients are injectable, so tests pass fakes/mocks
and assert the exact S3 key, JSON body, and StartExecution input without any
real AWS call.

Behavior on flush (:func:`persist_vcon`):

* If ``BUCKET_NAME`` is set, PUT the vCon JSON to S3 at the unique run key
  (``<room_name>/<session_start_iso>/vcon-skeleton.json``) with
  ``ContentType=application/json``. Then, if ``STATE_MACHINE_ARN`` is also set,
  trigger enrichment via Step Functions ``start_execution`` with input
  ``json.dumps({"vcon": <doc>})`` -- the exact envelope the enrichment state machine
  unwraps with ``event.get("vcon", event)``.
* If ``BUCKET_NAME`` is NOT set, write the vCon JSON to a local file under a
  ``vcon-output/`` directory using the same run-key path layout, and skip
  StartExecution entirely (no AWS client is created or called).

Under the acknowledged "one flush PUT on session end" shortcut there is no
retry or per-turn history, so the end-of-session write is the one and only
chance to persist the whole call. To keep that one-shot model fail-safe rather
than fail-open, :func:`persist_vcon` makes a last-resort local write under
``vcon-output/`` (same run-key layout) if the S3 path raises, so a transient S3
or StartExecution error does not silently lose the entire vCon.

Config is read from the environment (``BUCKET_NAME``, ``STATE_MACHINE_ARN``,
``AWS_REGION``). These are OPTIONAL: the local-file fallback keeps the
agent demoable standalone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .vcon import execution_name, run_key

# Environment variable names (map of CloudFormation Outputs -> local .env).
ENV_BUCKET_NAME = "BUCKET_NAME"
ENV_STATE_MACHINE_ARN = "STATE_MACHINE_ARN"
ENV_AWS_REGION = "AWS_REGION"

# Directory used by the local-file fallback when BUCKET_NAME is unset.
LOCAL_OUTPUT_DIR = "vcon-output"


@dataclass
class PersistResult:
    """Describes where a vCon was written and whether enrichment was triggered.

    ``destination`` is either ``"s3"`` or ``"local"``. ``location`` is the S3 URI
    (``s3://<bucket>/<key>``) or the local filesystem path. ``key`` is the common
    run-key path used for both destinations. ``enrichment_triggered`` is True only
    when a Step Functions execution was started.
    """

    destination: str
    location: str
    key: str
    enrichment_triggered: bool
    execution_arn: Optional[str] = None
    # True when the S3 path raised and the document was salvaged to a local file
    # as a last resort (see the fail-safe note in the module docstring).
    s3_fallback_to_local: bool = False


def persist_vcon(
    vcon_doc: Dict[str, Any],
    room_name: str,
    session_start_iso: str,
    *,
    env: Optional[Dict[str, str]] = None,
    s3_client: Optional[Any] = None,
    sfn_client: Optional[Any] = None,
    local_output_dir: Optional[str] = None,
) -> PersistResult:
    """Persist a completed vCon document to S3 (+ enrichment) or a local file.

    Parameters
    ----------
    vcon_doc:
        The in-memory vCon document (e.g. ``VConBuilder.to_dict()``).
    room_name, session_start_iso:
        Used to build the unique run key. ``session_start_iso`` is the single
        microsecond-precision run segment, so no separate timestamp is needed.
    env:
        Config source; defaults to ``os.environ``. Injectable so tests can pass
        an explicit mapping without touching the real process environment.
    s3_client, sfn_client:
        Injectable boto3 clients. When omitted and S3 persistence is required,
        real boto3 clients are created lazily (boto3 is only imported then, so
        the local-file path and tests with injected fakes never need boto3).
    local_output_dir:
        Base directory for the local-file fallback; defaults to ``vcon-output/``
        relative to the current working directory.

    Returns
    -------
    PersistResult
        Where the document was written and whether enrichment was triggered.
    """
    if env is None:
        env = os.environ

    key = run_key(room_name, session_start_iso)
    body_bytes = json.dumps(vcon_doc).encode("utf-8")

    bucket_name = env.get(ENV_BUCKET_NAME)

    # Local-file fallback: no bucket configured. Never touch AWS.
    if not bucket_name:
        out_path = _write_local(body_bytes, key, local_output_dir)
        return PersistResult(
            destination="local",
            location=str(out_path),
            key=key,
            enrichment_triggered=False,
        )

    # S3 path. Create a real client lazily only if one was not injected.
    #
    # Fail-safe for the one-PUT shortcut: this is the single persistence
    # opportunity for the whole call, so if the S3 PUT (or the guarded
    # StartExecution) raises, we salvage the document to a last-resort local
    # file instead of letting the entire vCon vanish with only a log line.
    region = env.get(ENV_AWS_REGION)
    try:
        if s3_client is None:
            s3_client = _make_boto3_client("s3", region)
        s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=body_bytes,
            ContentType="application/json",
        )

        # Guarded enrichment hook: only when a state machine ARN is configured.
        enrichment_triggered = False
        execution_arn: Optional[str] = None
        state_machine_arn = env.get(ENV_STATE_MACHINE_ARN)
        if state_machine_arn:
            if sfn_client is None:
                sfn_client = _make_boto3_client("stepfunctions", region)
            # Envelope must match the enrichment pipeline, which unwraps exactly once
            # with event.get("vcon", event). The execution name is a deterministic
            # idempotency key derived from (room_name, session_start_iso), so a
            # retried StartExecution collapses into the original run instead of
            # launching a duplicate pipeline.
            response = sfn_client.start_execution(
                stateMachineArn=state_machine_arn,
                name=execution_name(room_name, session_start_iso),
                input=json.dumps({"vcon": vcon_doc}),
            )
            enrichment_triggered = True
            if isinstance(response, dict):
                execution_arn = response.get("executionArn")
    except Exception:
        # Last resort: don't lose the call. Write the same bytes to the same
        # run-key path locally so the vCon survives a transient S3 outage,
        # expired credentials, or a StartExecution throttle at shutdown.
        out_path = _write_local(body_bytes, key, local_output_dir)
        return PersistResult(
            destination="local",
            location=str(out_path),
            key=key,
            enrichment_triggered=False,
            s3_fallback_to_local=True,
        )

    return PersistResult(
        destination="s3",
        location=f"s3://{bucket_name}/{key}",
        key=key,
        enrichment_triggered=enrichment_triggered,
        execution_arn=execution_arn,
    )


def _write_local(body_bytes: bytes, key: str, local_output_dir: Optional[str]) -> Path:
    """Write ``body_bytes`` to ``<local_output_dir or vcon-output>/<key>``.

    Creates parent directories as needed and returns the path written. Shared by
    the no-bucket fallback and the S3 last-resort salvage so both use the
    identical run-key layout.
    """
    base = Path(local_output_dir) if local_output_dir else Path(LOCAL_OUTPUT_DIR)
    out_path = base / key
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body_bytes)
    return out_path


def _make_boto3_client(service_name: str, region: Optional[str]) -> Any:
    """Create a real boto3 client. Imported lazily so the local-file fallback
    and tests with injected fakes never require boto3 at import time."""
    import boto3  # noqa: WPS433 (intentional lazy import)

    if region:
        return boto3.client(service_name, region_name=region)
    return boto3.client(service_name)
