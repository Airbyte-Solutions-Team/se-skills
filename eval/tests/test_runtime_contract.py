"""Deterministic fake-only tests for the Slice 5A runtime contract."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from webapp.hosted.runtime_contract import (
    Allowlist,
    InputManifest,
    NetworkDestination,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    RuntimeValidationError,
    SkillRuntime,
    ValidationResult,
)


def _minimal_job() -> RuntimeJob:
    return RuntimeJob(
        job_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        transcript_id=uuid.uuid4(),
        requester_id=uuid.uuid4(),
    )


class FakeSkillRuntime:
    """A fake runtime that returns a deterministic RuntimeResult."""

    async def execute(self, job: RuntimeJob) -> RuntimeResult:
        return RuntimeResult(
            output_artifact="# Call Summary: Acme",
            sidecar={"skill": job.skill, "mode": "full"},
            actual_runtime_version="fake-1.0",
            actual_model="fake-model",
            token_usage={"input_tokens": 100, "output_tokens": 50},
            cost=0.001,
            validation=ValidationResult(status="valid"),
        )


def test_runtime_job_requires_timezone_aware_deadline() -> None:
    with pytest.raises(RuntimeValidationError, match="timezone-aware"):
        RuntimeJob(
            job_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            transcript_id=uuid.uuid4(),
            requester_id=uuid.uuid4(),
            execution_deadline=datetime(2026, 6, 11, 12, 0, 0),
        )


def test_runtime_job_rejects_arbitrary_browser_paths() -> None:
    with pytest.raises(RuntimeValidationError, match="Invalid prior context reference"):
        InputManifest(
            transcript_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            prior_context_refs=["/etc/passwd"],
        )


def test_allowlist_rejects_forbidden_tools() -> None:
    for tool in ("Bash", "Git", "Browser", "Http", "McpDiscover", "BypassPermissions"):
        with pytest.raises(RuntimeValidationError, match="Forbidden tool"):
            Allowlist(tools=[tool])


def test_allowlist_accepts_typed_tools() -> None:
    allowlist = Allowlist(
        tools=["read_transcript", "write_output", "list_priors"],
        network=[NetworkDestination(host="api.anthropic.com", port=443)],
    )
    assert allowlist.tools == ["read_transcript", "write_output", "list_priors"]


def test_runtime_result_cannot_be_both_output_and_failure() -> None:
    with pytest.raises(RuntimeValidationError, match="cannot contain both"):
        RuntimeResult(
            output_artifact="# Title",
            failure=RedactedFailure(category="executor_error", message="nope"),
        )


def test_runtime_result_must_be_output_or_failure() -> None:
    with pytest.raises(RuntimeValidationError, match="must contain output_artifact or failure"):
        RuntimeResult()


def test_validation_result_status_domain() -> None:
    for status in ("valid", "invalid", "unvalidated"):
        assert ValidationResult(status=status).status == status
    with pytest.raises(RuntimeValidationError, match="Invalid validation status"):
        ValidationResult(status="maybe")


def test_skill_runtime_protocol_is_satisfied_by_fake() -> None:
    assert isinstance(FakeSkillRuntime(), SkillRuntime)


@pytest.mark.asyncio
async def test_fake_runtime_returns_contract_result() -> None:
    job = _minimal_job()
    runtime = FakeSkillRuntime()
    result = await runtime.execute(job)
    assert result.output_artifact
    assert result.validation and result.validation.status == "valid"
    assert result.failure is None
    assert result.actual_runtime_version == "fake-1.0"
