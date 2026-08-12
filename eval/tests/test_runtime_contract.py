"""Deterministic fake-only tests for the Slice 5A runtime contract."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from webapp.hosted.runtime_contract import (
    Allowlist,
    ExecutionMetadata,
    InputManifest,
    NetworkDestination,
    OutputSidecar,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    RuntimeValidationError,
    SkillRuntime,
    TokenUsage,
    ToolName,
)


def _now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _minimal_manifest() -> InputManifest:
    tid = uuid.uuid4()
    return InputManifest(
        transcript_id=tid,
        account_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
    )


def _minimal_job() -> RuntimeJob:
    manifest = _minimal_manifest()
    return RuntimeJob(
        job_id=uuid.uuid4(),
        org_id=manifest.org_id,
        account_id=manifest.account_id,
        transcript_id=manifest.transcript_id,
        requester_id=uuid.uuid4(),
        input_manifest=manifest,
        execution_deadline=_now(),
    )


class _FakeCancellationToken:
    """Host-side cancellation token for tests."""

    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self._cancelled


class FakeSkillRuntime:
    """A fake runtime that returns a deterministic RuntimeResult."""

    async def execute(self, job: RuntimeJob, cancellation: object) -> RuntimeResult:
        return RuntimeResult(
            output_artifact="# Call Summary: Acme",
            sidecar=OutputSidecar(
                skill=job.skill,
                skill_version=job.skill_version,
                mode=job.mode,
                title="Call Summary: Acme",
                date="2026-08-11",
            ),
            execution_metadata=ExecutionMetadata(
                runtime_version="fake-1.0",
                model="fake-model",
                token_usage=TokenUsage(input_tokens=100, output_tokens=50),
                cost=0.001,
            ),
        )


# ---------------------------------------------------------------------------
# RuntimeJob validation
# ---------------------------------------------------------------------------

def test_runtime_job_requires_timezone_aware_deadline() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        job = _minimal_job()
        RuntimeJob(
            job_id=job.job_id,
            org_id=job.org_id,
            account_id=job.account_id,
            transcript_id=job.transcript_id,
            requester_id=job.requester_id,
            input_manifest=job.input_manifest,
            execution_deadline=datetime(2026, 6, 11, 12, 0, 0),
        )


def test_runtime_job_requires_input_manifest() -> None:
    with pytest.raises(ValidationError, match="input_manifest"):
        RuntimeJob(
            job_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            transcript_id=uuid.uuid4(),
            requester_id=uuid.uuid4(),
            execution_deadline=_now(),
        )


def test_runtime_job_rejects_input_manifest_identity_mismatch() -> None:
    manifest = _minimal_manifest()
    with pytest.raises(ValidationError, match="transcript_id"):
        RuntimeJob(
            job_id=uuid.uuid4(),
            org_id=manifest.org_id,
            account_id=manifest.account_id,
            transcript_id=uuid.uuid4(),
            requester_id=uuid.uuid4(),
            input_manifest=manifest,
            execution_deadline=_now(),
        )


def test_runtime_job_rejects_output_workspace_outside_tmp() -> None:
    job = _minimal_job()
    with pytest.raises(ValidationError, match="output_workspace"):
        RuntimeJob(
            job_id=job.job_id,
            org_id=job.org_id,
            account_id=job.account_id,
            transcript_id=job.transcript_id,
            requester_id=job.requester_id,
            input_manifest=job.input_manifest,
            execution_deadline=_now(),
            output_workspace="/etc/runtime-output",
        )


def test_runtime_job_rejects_traversal_in_output_workspace() -> None:
    job = _minimal_job()
    with pytest.raises(ValidationError, match="output_workspace"):
        RuntimeJob(
            job_id=job.job_id,
            org_id=job.org_id,
            account_id=job.account_id,
            transcript_id=job.transcript_id,
            requester_id=job.requester_id,
            input_manifest=job.input_manifest,
            execution_deadline=_now(),
            output_workspace="/tmp/../etc/runtime-output",
        )


def test_runtime_job_serializes_to_json_and_back() -> None:
    job = _minimal_job()
    payload = job.model_dump_json()
    data = json.loads(payload)
    restored = RuntimeJob(**data)
    assert restored.job_id == job.job_id
    assert restored.input_manifest.transcript_id == job.transcript_id
    assert restored.execution_deadline == job.execution_deadline


# ---------------------------------------------------------------------------
# InputManifest validation
# ---------------------------------------------------------------------------

def test_input_manifest_rejects_arbitrary_browser_paths() -> None:
    with pytest.raises(ValidationError, match="Invalid prior context reference"):
        InputManifest(
            transcript_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            prior_context_refs=["/etc/passwd"],
        )


def test_input_manifest_rejects_traversal_in_prior_refs() -> None:
    with pytest.raises(ValidationError, match="Invalid prior context reference"):
        InputManifest(
            transcript_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            prior_context_refs=["../secrets"],
        )


# ---------------------------------------------------------------------------
# Allowlist / tool registry
# ---------------------------------------------------------------------------

def test_allowlist_rejects_unregistered_tools() -> None:
    for tool in ("bash", "shell", "exec", "git", "browser", "http", "mcpdiscover", "bypasspermissions"):
        with pytest.raises(ValidationError, match="read_transcript"):
            Allowlist(tools={tool})  # type: ignore[arg-type]


def test_allowlist_rejects_lookalike_tool_names() -> None:
    # Substring/lookalike matches must not bypass the closed registry.
    for tool in ("bash_script", "git-clone", "my_browser", "http_request", "mcp_discover"):
        with pytest.raises(ValidationError, match="read_transcript"):
            Allowlist(tools={tool})  # type: ignore[arg-type]


def test_allowlist_accepts_typed_tools() -> None:
    allowlist = Allowlist(
        tools={"read_transcript", "write_output", "list_priors"},
        network={NetworkDestination(host="api.anthropic.com", port=443)},
    )
    assert allowlist.tools == {"read_transcript", "write_output", "list_priors"}


# ---------------------------------------------------------------------------
# NetworkDestination validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host, reason",
    [
        pytest.param("", "non-empty", id="empty-host"),
        pytest.param("*", "wildcards", id="wildcard-host"),
        pytest.param("127.0.0.1", "IP address", id="ipv4-host"),
        pytest.param("::1", "IP address", id="ipv6-host"),
    ],
)
def test_network_destination_rejects_invalid_host(host: str, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        NetworkDestination(host=host)


def test_network_destination_rejects_invalid_port() -> None:
    with pytest.raises(ValidationError, match="port"):
        NetworkDestination(host="api.anthropic.com", port=70000)


def test_network_destination_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError, match="path_prefix"):
        NetworkDestination(host="api.anthropic.com", path_prefix="/../etc")


def test_network_destination_accepts_localhost() -> None:
    dest = NetworkDestination(host="localhost", scheme="http")
    assert str(dest) == "http://localhost"


# ---------------------------------------------------------------------------
# RuntimeResult validation
# ---------------------------------------------------------------------------

def test_runtime_result_cannot_be_both_output_and_failure() -> None:
    with pytest.raises(ValidationError, match="cannot contain both"):
        RuntimeResult(
            output_artifact="# Title",
            sidecar=OutputSidecar(skill="post-call"),
            failure=RedactedFailure(category="executor_error", message="nope"),
        )


def test_runtime_result_must_be_output_or_failure() -> None:
    with pytest.raises(ValidationError, match="must contain output_artifact or failure"):
        RuntimeResult()


def test_runtime_result_output_requires_sidecar() -> None:
    with pytest.raises(ValidationError, match="sidecar"):
        RuntimeResult(output_artifact="# Title")


def test_runtime_result_rejects_sandbox_supplied_validation_status() -> None:
    # The sandbox must not be able to claim its own output is valid; the worker
    # owns validation. A RuntimeResult has no validation field.
    result = RuntimeResult(
        output_artifact="# Title",
        sidecar=OutputSidecar(skill="post-call"),
    )
    assert not hasattr(result, "validation")


# ---------------------------------------------------------------------------
# SkillRuntime protocol
# ---------------------------------------------------------------------------

def test_skill_runtime_protocol_is_satisfied_by_fake() -> None:
    assert isinstance(FakeSkillRuntime(), SkillRuntime)


@pytest.mark.asyncio
async def test_fake_runtime_returns_contract_result() -> None:
    job = _minimal_job()
    runtime = FakeSkillRuntime()
    result = await runtime.execute(job, _FakeCancellationToken())
    assert result.output_artifact
    assert result.sidecar is not None
    assert result.sidecar.skill == job.skill
    assert result.failure is None
    assert result.execution_metadata.runtime_version == "fake-1.0"


@pytest.mark.asyncio
async def test_fake_runtime_stops_on_cancellation() -> None:
    class CancellingRuntime:
        async def execute(self, job: RuntimeJob, cancellation: object) -> RuntimeResult:
            if cancellation.is_cancelled():
                return RuntimeResult(
                    failure=RedactedFailure(category="cancelled", message="Cancelled by worker"),
                )
            return RuntimeResult(
                output_artifact="# Title",
                sidecar=OutputSidecar(skill=job.skill, skill_version=job.skill_version, mode=job.mode),
            )

    job = _minimal_job()
    result = await CancellingRuntime().execute(job, _FakeCancellationToken(cancelled=True))
    assert result.failure is not None
    assert result.output_artifact is None
