"""Deterministic integration tests for the trusted post-call orchestrator."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from eval.tests.hosted_helpers import (
    _auth_header,
    _context_token,
    _seed_account,
    _seed_member,
    _seed_opportunity,
    _seed_transcript,
)
from hosted import storage as hosted_storage
from hosted.post_call_orchestrator import OrchestratorContext, PostCallOrchestrator
from hosted.runtime_contract import RuntimeResult, SkillRuntime
from hosted import config as hosted_config

pytestmark = [pytest.mark.asyncio, pytest.mark.hosted]


@pytest.fixture(autouse=True)
def worker_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the provisioned workspace root for deterministic orchestrator tests."""
    workspace_root = tmp_path / "worker-workspaces"
    workspace_root.mkdir()
    monkeypatch.setattr(hosted_config, "RUNSC_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(
        "hosted.post_call_orchestrator.config.RUNSC_WORKSPACE_ROOT",
        str(workspace_root),
    )


@pytest.fixture
def backend(app_client: TestClient) -> Any:
    """Return the configured in-memory Storage backend after app startup.

    The `app_client` fixture sets the singleton backend; this fixture re-imports
    the `hosted.storage` module so tests do not hold a stale module object.
    """
    from hosted import storage

    return storage.get_backend()


@pytest.fixture(autouse=True)
async def _clean_outputs(admin_pool: asyncpg.Pool) -> None:
    """Reset the output ledger between orchestrator tests."""
    async with admin_pool.acquire() as conn:
        await conn.execute("TRUNCATE public.reviews, public.output_versions, public.outputs CASCADE")
        await conn.execute("TRUNCATE public.job_attempts, public.jobs CASCADE")


class FakePostCallRuntime:
    """A deterministic runtime that writes a candidate output.md + sidecar.json."""

    def __init__(
        self,
        markdown: str,
        sidecar: dict,
        *,
        fail: Any = None,
        delay: float = 0.0,
        execution_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.markdown = markdown
        self.sidecar = sidecar
        self.fail = fail
        self.delay = delay
        self.execution_metadata = execution_metadata or {}

    async def execute(self, job: Any, cancellation: Any) -> Any:
        from hosted.runtime_contract import CancellationToken, RedactedFailure, RuntimeResult

        if self.delay:
            # Check cancellation frequently so the worker can interrupt the fake runtime.
            for _ in range(int(self.delay * 10)):
                if cancellation.is_cancelled():
                    return RuntimeResult(failure=RedactedFailure(category="cancelled"))
                await asyncio.sleep(0.1)
        if cancellation.is_cancelled():
            return RuntimeResult(failure=RedactedFailure(category="cancelled"))
        if self.fail is not None:
            return self.fail
        output_dir = Path(job.output_workspace)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
        (output_dir / "sidecar.json").write_text(json.dumps(self.sidecar), encoding="utf-8")
        return RuntimeResult(
            output_artifact=self.markdown,
            sidecar=self.sidecar,
            execution_metadata=self.execution_metadata,
        )


def _job_from_claim(claim: asyncpg.Record) -> dict[str, Any]:
    """Convert a raw asyncpg claim record into the dict the orchestrator expects."""
    job: dict[str, Any] = dict(claim)
    for key in (
        "job_id",
        "attempt_number",
        "lease_token",
        "org_id",
        "account_id",
        "transcript_id",
        "opportunity_id",
        "requester_id",
    ):
        if job.get(key) is not None and not isinstance(job[key], (str, int)):
            job[key] = str(job[key])
    for key in ("payload", "input_refs", "source_manifest"):
        if isinstance(job.get(key), str):
            job[key] = json.loads(job[key])
    if "v_timeout" in job and job["v_timeout"] is not None:
        job["deadline_ts"] = job["v_timeout"]
    elif "deadline" in job and job["deadline"] is not None:
        job["deadline_ts"] = job["deadline"]
    else:
        job["deadline_ts"] = time.time() + 60
    job.setdefault("worker_id", "test-worker")
    return job


async def _seed_job_ready_org(
    admin_pool: asyncpg.Pool,
    email: str = "orch-test@airbyte.io",
    *,
    with_opportunity: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    user_id, org_id, _ = await _seed_member(admin_pool, email)
    account_id = await _seed_account(admin_pool, org_id, user_id)
    opportunity_id = None
    if with_opportunity:
        opportunity_id = await _seed_opportunity(admin_pool, org_id, account_id, user_id)
    transcript_id = await _seed_transcript(
        admin_pool, org_id, account_id, opportunity_id, user_id
    )
    return user_id, org_id, account_id, transcript_id, opportunity_id


async def _upload_transcript_content(
    backend: Any,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    text: str,
    filename: str = "test-transcript.txt",
) -> str:
    """Upload transcript bytes to the in-memory storage backend at the expected path."""
    storage_path = f"{org_id}/{account_id}/{transcript_id}-{filename}"
    data = text.encode("utf-8")

    async def _stream() -> AsyncGenerator[bytes, None]:
        yield data

    await backend.upload(
        user_id,
        storage_path,
        _stream(),
        "text/plain; charset=utf-8",
    )
    return storage_path


def _full_sidecar(title: str = "Test call", date: str = "2026-08-10") -> dict:
    return {
        "skill": "post-call",
        "skill_version": "1.0",
        "mode": "full",
        "title": title,
        "date": date,
        "source_coverage": "Read transcript.txt in full (100 / 100 lines).",
    }


def _valid_full_output() -> str:
    return Path("eval/fixtures/outputs/post-call-full.md").read_text(encoding="utf-8")


def _valid_brief_output() -> str:
    text = _valid_full_output()
    keep = [
        "### At a Glance",
        "## Key Takeaways",
        "## Action Items",
        "## Next Step",
        "## Source Coverage",
    ]
    lines = []
    in_removed = False
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            in_removed = line not in keep
        if not in_removed:
            lines.append(line)
    return "\n".join(lines)


def _invalid_output_missing_source() -> str:
    text = _valid_full_output()
    return text.replace(
        "Read Acme-06.11.26.txt in full (612 / 612 lines).",
        "",
    )


async def test_worker_runs_post_call_and_persists_valid_output(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """End-to-end fake runtime produces a valid output row and completes the job."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    transcript_text = "Discovery call with Acme. They use Salesforce and Snowflake systems."
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, transcript_text
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-1", timeout_seconds=30)
    assert await worker.run_once() is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "success"
    assert detail["job"]["validation_status"] == "valid"
    assert detail["job"]["result_output_id"] is not None
    output_id = detail["job"]["result_output_id"]

    out = app_client.get(
        f"/api/hosted/accounts/{account_id}/outputs/{output_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert out["output"]["validation_status"] == "valid"
    assert out["output"]["job_id"] == str(job_id)
    assert out["output"]["org_id"] == str(org_id)

    content = app_client.get(
        f"/api/hosted/accounts/{account_id}/outputs/{output_id}/content",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert content["validation_status"] == "valid"
    assert "<h1" in content["html"]
    assert "Call Summary" in content["markdown"]


async def test_brief_mode_is_accepted(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A valid brief-mode output completes without the extended sections."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Brief follow-up call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(_valid_brief_output(), {**_full_sidecar(), "mode": "brief"})
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-brief", timeout_seconds=30)
    assert await worker.run_once() is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "success"
    assert detail["job"]["validation_status"] == "valid"


@pytest.mark.parametrize(
    "transcript_text,markdown",
    [
        pytest.param(
            "We talked about Salesforce and Snowflake systems and connectors.",
            _valid_full_output().replace("## Sources & Destinations\n", ""),
            id="missing_sources_when_systems_mentioned",
        ),
        pytest.param(
            "We discussed the data warehouse and CDC requirements.",
            _valid_full_output().replace("## Technical Notes\n", ""),
            id="missing_technical_when_scope_discussed",
        ),
        pytest.param(
            "The AE led a discovery call with MEDDPICC scoring.",
            _valid_full_output().replace("## MEDDPICC Quick Pass\n", ""),
            id="missing_meddpicc_when_discovery_call",
        ),
    ],
)
async def test_conditional_section_triggers_fail_when_missing(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
    transcript_text: str,
    markdown: str,
) -> None:
    """Missing conditional sections required by the transcript fail validation."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, transcript_text
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "skill": "post-call",
            "max_attempts": 1,
        },
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(markdown, _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-cond", timeout_seconds=30)
    assert await worker.run_once() is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "failure"
    assert detail["job"]["validation_status"] == "invalid"
    assert detail["job"]["error"] is not None
    assert detail["attempts"][0]["error_category"] == "output_error"
    assert detail["job"]["result_output_id"] is None


async def test_invalid_artifact_leaves_no_output_or_storage(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A validation-failed attempt leaves no outputs row and no storage object."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Short call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "skill": "post-call",
            "max_attempts": 1,
        },
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(_invalid_output_missing_source(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-invalid", timeout_seconds=30)
    assert await worker.run_once() is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "failure"
    assert detail["job"]["validation_status"] == "invalid"
    assert detail["job"]["result_output_id"] is None

    async with admin_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM public.outputs WHERE job_id = $1", job_id
        )
    assert count == 0
    assert len(backend.objects) == 1  # only the transcript object


async def test_runtime_failure_is_not_validation_error(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A runtime-reported failure is recorded as a redacted model/failure category."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.runtime_contract import RedactedFailure, RuntimeResult
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "skill": "post-call",
            "max_attempts": 1,
        },
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    fail_result = RuntimeResult(failure=RedactedFailure(category="model_error"))
    runtime = FakePostCallRuntime("", {}, fail=fail_result)
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-fail", timeout_seconds=30)
    assert await worker.run_once() is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "failure"
    assert detail["job"]["error"] is not None
    assert detail["attempts"][0]["error_category"] == "model_error"
    assert detail["job"]["result_output_id"] is None


async def test_manifest_mismatch_rejects_cross_org_and_cross_account(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """The orchestrator rejects a manifest whose org or account does not match the job."""
    from hosted.post_call_orchestrator import PostCallExecutor

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    other_user, other_org, other_account, other_transcript, _ = await _seed_job_ready_org(
        admin_pool, "other-orch@airbyte.io"
    )
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Our call."
    )
    await _upload_transcript_content(
        backend, other_user, other_org, other_account, other_transcript, "Other call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "orch-xorg", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)
    manifest = job["source_manifest"]
    manifest["transcript_id"] = str(other_transcript)
    manifest["storage_path"] = f"{other_org}/{other_account}/{other_transcript}-test-transcript.txt"
    job["source_manifest"] = manifest

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    result = await executor.execute(job)
    assert result.error_category == "input_error"
    assert result.output_id is None


async def test_manifest_unlisted_storage_path_fails(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """The orchestrator refuses to read a transcript path not in the manifest."""
    from hosted.post_call_orchestrator import PostCallExecutor

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Our call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "orch-unlisted", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)
    manifest = job["source_manifest"]
    manifest["storage_path"] = f"{org_id}/{account_id}/{uuid.uuid4()}-fake.txt"
    job["source_manifest"] = manifest

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    result = await executor.execute(job)
    assert result.error_category in {"input_error", "runtime_error"}
    assert result.output_id is None


async def test_sidecar_validation_field_injection_is_rejected(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A sidecar that carries a validation_status field is rejected at the boundary."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "skill": "post-call",
            "max_attempts": 1,
        },
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    sidecar = _full_sidecar()
    sidecar["validation_status"] = "valid"
    runtime = FakePostCallRuntime(_valid_full_output(), sidecar)
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-sidecar", timeout_seconds=30)
    assert await worker.run_once() is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "failure"
    assert detail["job"]["result_output_id"] is None


async def test_idempotent_retry_does_not_duplicate_output(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A second call to the orchestrator with the same attempt re-uses the outputs row."""
    from hosted.post_call_orchestrator import PostCallExecutor

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "orch-idem", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    result1 = await executor.execute(job)
    assert result1.output_id is not None
    assert result1.validation_status == "valid"

    result2 = await executor.execute(job)
    assert result2.output_id == result1.output_id
    assert result2.validation_status == "valid"

    async with admin_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM public.outputs WHERE job_id = $1", job_id
        )
    assert count == 1


async def test_output_api_enforces_org_isolation(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A member cannot read outputs belonging to a different organization."""
    user1, org1, account1, transcript1, _ = await _seed_job_ready_org(admin_pool, "u1@airbyte.io")
    user2, org2, account2, transcript2, _ = await _seed_job_ready_org(admin_pool, "u2@airbyte.io")

    async with admin_pool.acquire() as conn:
        # Create a job row that the output FK can reference.
        job_row = await conn.fetchrow(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, 'post-call', '1.0', 'success', 1)
            RETURNING id
            """,
            org1,
            account1,
            transcript1,
            user1,
        )
        job_id = job_row["id"]
        output_id = uuid.uuid5(uuid.NAMESPACE_URL, f"iso:{org1}:{account1}")
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'post-call', 'valid')
            """,
            output_id,
            org1,
            job_id,
            account1,
            transcript1,
            user1,
            f"{org1}/{account1}/out.md",
            "title",
            "{}",
        )

    response = app_client.get(
        f"/api/hosted/accounts/{account1}/outputs/{output_id}",
        headers=_auth_header(user1, "u1@airbyte.io"),
    )
    assert response.status_code == 200

    response = app_client.get(
        f"/api/hosted/accounts/{account1}/outputs/{output_id}",
        headers=_auth_header(user2, "u2@airbyte.io"),
    )
    assert response.status_code == 404

    response = app_client.get(
        f"/api/hosted/accounts/{account2}/outputs/{output_id}",
        headers=_auth_header(user2, "u2@airbyte.io"),
    )
    assert response.status_code == 404


async def test_worker_cancels_post_call_runtime(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A cancellation request interrupts a long-running fake runtime."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar(), delay=10.0)
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(
        worker_pool,
        executor=executor,
        worker_name="orch-cancel",
        timeout_seconds=30,
        heartbeat_interval=0.2,
    )

    process_task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.3)
    async with user_pool.acquire() as conn:
        await conn.fetchval(
            "SELECT public.request_job_cancellation($1, $2)",
            _context_token(user_id),
            job_id,
        )
    await asyncio.wait_for(process_task, timeout=5)

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "cancelled"


class _DeadlineCapturingRuntime:
    """Runtime that records the wall-clock execution deadline passed by the worker."""

    def __init__(self, markdown: str, sidecar: dict) -> None:
        self.markdown = markdown
        self.sidecar = sidecar
        self.received_deadline: datetime | None = None

    async def execute(self, job: Any, cancellation: Any) -> Any:
        from hosted.runtime_contract import RuntimeResult

        self.received_deadline = job.execution_deadline
        output_dir = Path(job.output_workspace)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
        (output_dir / "sidecar.json").write_text(json.dumps(self.sidecar), encoding="utf-8")
        return RuntimeResult(output_artifact=self.markdown, sidecar=self.sidecar)


async def test_worker_passes_wall_clock_deadline_to_runtime(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """The worker passes a timezone-aware UTC wall-clock deadline, not loop.time()."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    before = datetime.now(tz=timezone.utc)
    runtime = _DeadlineCapturingRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-deadline", timeout_seconds=30)
    await worker.run_once()

    after = datetime.now(tz=timezone.utc)
    assert runtime.received_deadline is not None
    assert runtime.received_deadline.tzinfo is not None
    assert before <= runtime.received_deadline <= after + timedelta(seconds=30)

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "success"


async def test_authoritative_input_rejects_same_org_substituted_transcript(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A manifest tampered to point at another same-org transcript is rejected."""
    from hosted.post_call_orchestrator import PostCallExecutor

    user_id, org_id, account_id, transcript1, _ = await _seed_job_ready_org(admin_pool)
    transcript2 = await _seed_transcript(
        admin_pool, org_id, account_id, None, user_id, filename="other.txt"
    )
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript1, "First transcript."
    )
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript2, "Second transcript."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript1), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "orch-subst", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)
    manifest = job["source_manifest"]
    manifest["transcript_id"] = str(transcript2)
    # Keep the storage_path pointing to the legitimate transcript so the tampering
    # is detected by the canonical DB lookup, not by a missing object.
    job["source_manifest"] = manifest

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    result = await executor.execute(job)
    assert result.error_category == "input_error"
    assert result.output_id is None


async def test_retry_after_storage_upload_failure_recovers_without_duplicate(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A Storage upload failure after the outputs row is created is repaired on retry."""
    from hosted.post_call_orchestrator import PostCallExecutor

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "retry-worker", 60
        )
    job = _job_from_claim(claim)

    from hosted import storage as hosted_storage

    original_upload = backend.upload
    upload_attempts = 0

    async def failing_then_real_upload(*args: Any, **kwargs: Any) -> Any:
        nonlocal upload_attempts
        upload_attempts += 1
        if upload_attempts == 1:
            raise hosted_storage.StorageError("injected upload failure")
        return await original_upload(*args, **kwargs)

    backend.upload = failing_then_real_upload

    runtime = FakePostCallRuntime(_valid_full_output(), _full_sidecar())
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)

    # First attempt: metadata row is inserted, Storage upload fails, orchestrator
    # returns a redacted runtime error without completing the job.
    result = await executor.execute(job)
    assert result.error_category == "runtime_error"
    assert result.output_id is None

    # Second attempt with the same running lease: the unvalidated row does not
    # exist because the failed upload was rolled back, so the runtime runs again
    # and the orchestrator persists and finalizes the job.
    result = await executor.execute(job)
    assert result.error_category is None
    assert result.output_id is not None
    assert result.validation_status == "valid"
    assert result.finalized is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "success"
    assert detail["job"]["result_output_id"] == str(result.output_id)

    # Only one outputs row and one storage object should exist for this job.
    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM public.outputs WHERE job_id = $1", job_id
        )
    assert row["cnt"] == 1
    assert upload_attempts == 2


async def test_runtime_provenance_is_preserved_on_output(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """Token usage, model, runtime version, and cost from the runtime are persisted."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    job_id = response.json()["id"]

    runtime = FakePostCallRuntime(
        _valid_full_output(),
        _full_sidecar(),
        execution_metadata={
            "runtime_version": "test-runtime-9",
            "model": "test-model-x",
            "token_usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 5,
            },
            "cost": 0.00123,
        },
    )
    executor = PostCallExecutor(runtime, db_pool=worker_pool, storage_backend=backend)
    worker = Worker(worker_pool, executor=executor, worker_name="orch-prov", timeout_seconds=30)
    await worker.run_once()

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "success"
    assert detail["job"]["runtime_version"] == "test-runtime-9"
    assert detail["job"]["model"] == "test-model-x"
    assert detail["job"]["token_usage"]["total_tokens"] == 165
    assert detail["job"]["cost"] == pytest.approx(0.00123, abs=1e-4)

    output_id = detail["job"]["result_output_id"]
    response = app_client.get(
        f"/api/hosted/accounts/{account_id}/outputs/{output_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 200
    out = response.json()["output"]
    assert out["runtime_version"] == "test-runtime-9"
    assert out["model"] == "test-model-x"


async def _create_prior_output(
    admin_pool: asyncpg.Pool,
    backend: Any,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    job_status: str,
    validation_status: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert a fake prior job and output row, returning output id and content path."""
    async with admin_pool.acquire() as conn:
        prior_job = await conn.fetchrow(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, 'post-call', '1.0', $5, 1)
            RETURNING id
            """,
            org_id,
            account_id,
            transcript_id,
            user_id,
            job_status,
        )
        prior_job_id = prior_job["id"]
        output_id = uuid.uuid4()
        storage_path = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'Prior', '{}', 'post-call', $8)
            """,
            output_id,
            org_id,
            prior_job_id,
            account_id,
            transcript_id,
            user_id,
            storage_path,
            validation_status,
        )

    from hosted import storage as hosted_storage

    data = b"prior context"

    async def _stream() -> AsyncGenerator[bytes, None]:
        yield data

    await backend.upload(
        user_id, storage_path, _stream(), "text/markdown; charset=utf-8", bucket=hosted_storage.OUTPUTS_BUCKET
    )
    return prior_job_id, output_id, storage_path


@pytest.mark.parametrize(
    "job_status,validation_status,other_org,should_succeed",
    [
        pytest.param("success", "valid", False, True, id="valid_prior_is_accepted"),
        pytest.param("success", "unvalidated", False, False, id="unvalidated_prior_rejected"),
        pytest.param("success", "invalid", False, False, id="invalid_prior_rejected"),
        pytest.param("cancelled", "valid", False, False, id="cancelled_orphan_prior_rejected"),
        pytest.param("success", "valid", True, False, id="cross_scope_prior_rejected"),
    ],
)
async def test_resolve_job_inputs_requires_valid_prior_outputs(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
    job_status: str,
    validation_status: str,
    other_org: bool,
    should_succeed: bool,
) -> None:
    """resolve_job_inputs only allows valid, same-scope, successful-job outputs as priors."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    if other_org:
        other_user, other_org_id, other_account, other_transcript, _ = await _seed_job_ready_org(
            admin_pool, "other@airbyte.io"
        )
        prior_org = other_org_id
        prior_account = other_account
        prior_transcript = other_transcript
        prior_user = other_user
    else:
        prior_org = org_id
        prior_account = account_id
        prior_transcript = transcript_id
        prior_user = user_id

    _, prior_output_id, _ = await _create_prior_output(
        admin_pool,
        backend,
        prior_user,
        prior_org,
        prior_account,
        prior_transcript,
        job_status,
        validation_status,
    )

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "prior-test", 60
        )
    assert claim is not None

    if should_succeed:
        raw = await worker_pool.fetchval(
            "SELECT public.resolve_job_inputs($1, $2, $3, $4::uuid[])",
            job_id,
            claim["attempt_number"],
            claim["lease_token"],
            [prior_output_id],
        )
        result = json.loads(raw)
        prior_ids = [entry["output_id"] for entry in result["prior_outputs"]]
        assert str(prior_output_id) in prior_ids
    else:
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await worker_pool.fetchval(
                "SELECT public.resolve_job_inputs($1, $2, $3, $4::uuid[])",
                job_id,
                claim["attempt_number"],
                claim["lease_token"],
                [prior_output_id],
            )


async def test_materialized_inputs_are_read_only(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """_materialize_inputs writes transcript and prior files with read-only permissions."""
    from hosted.post_call_orchestrator import PostCallOrchestrator
    from hosted.runtime_contract import SkillRuntime

    class DummyRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            raise AssertionError("runtime should not be invoked")

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call with Acme."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "readonly-test", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    orchestrator = PostCallOrchestrator(DummyRuntime(), db_pool=worker_pool, storage_backend=backend)
    source_manifest, manifest, prior_paths = await orchestrator._resolve_inputs(job)

    input_dir = Path(tempfile.mkdtemp(prefix="test-input-"))
    try:
        await orchestrator._materialize_inputs(
            manifest, source_manifest, prior_paths, user_id, input_dir
        )
        transcript_file = input_dir / manifest.transcript_ref
        assert transcript_file.exists()
        file_mode = stat.S_IMODE(transcript_file.stat().st_mode)
        dir_mode = stat.S_IMODE(input_dir.stat().st_mode)
        assert file_mode == 0o444, f"transcript file mode was {oct(file_mode)}"
        assert dir_mode == 0o555, f"input directory mode was {oct(dir_mode)}"
        assert os.access(transcript_file, os.W_OK) is False

        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                (input_dir / "new.txt").write_text("tamper")
            with pytest.raises(PermissionError):
                transcript_file.open("a").write("tamper")
    finally:
        PostCallOrchestrator._make_writable(input_dir)
        shutil.rmtree(input_dir, ignore_errors=True)


@pytest.mark.parametrize(
    "artifact_type",
    [
        pytest.param("symlink", id="symlink_redirect"),
        pytest.param("fifo", id="fifo_substitute"),
        pytest.param("oversized", id="oversized_file"),
    ],
)
async def test_safe_read_output_rejects_hostile_filesystem_objects(artifact_type: str) -> None:
    """_safe_read_text refuses to follow symlinks, open FIFOs, or read oversized files."""
    from hosted.post_call_orchestrator import PostCallOrchestrator, RuntimeOutputError

    tmp = Path(tempfile.mkdtemp(prefix="test-output-"))
    try:
        path = tmp / "output.md"
        if artifact_type == "symlink":
            secret = tmp / "secret.txt"
            secret.write_text("secret content")
            path.symlink_to(secret)
        elif artifact_type == "fifo":
            os.mkfifo(str(path))
        elif artifact_type == "oversized":
            path.write_bytes(b"")
            os.truncate(str(path), 10 * 1024 * 1024 + 1)

        with pytest.raises(RuntimeOutputError):
            await asyncio.to_thread(PostCallOrchestrator._safe_read_text, path, 10 * 1024 * 1024)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _counting_cancel_orchestrator(
    runtime: Any,
    db_pool: asyncpg.Pool,
    backend: Any,
    cancel_after: int,
) -> Any:
    """Return a PostCallOrchestrator whose _cancel_requested returns True after N calls."""
    from hosted.post_call_orchestrator import PostCallOrchestrator

    class CountingCancelOrchestrator(PostCallOrchestrator):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._cancel_calls = 0
            self._cancel_after = cancel_after

        async def _cancel_requested(self, job: Any, context: Any, timeout_seconds: int) -> bool:
            self._cancel_calls += 1
            if self._cancel_calls >= self._cancel_after:
                return True
            return await super()._cancel_requested(job, context, timeout_seconds)

    return CountingCancelOrchestrator(runtime, db_pool=db_pool, storage_backend=backend)


@pytest.mark.parametrize(
    "cancel_after,description",
    [
        pytest.param(2, "after metadata row creation"),
        pytest.param(3, "after storage upload"),
    ],
)
async def test_cancellation_after_persist_cleans_staged_output(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
    cancel_after: int,
    description: str,
) -> None:
    """A cancellation that wins after the outputs row or object is staged removes both."""
    from hosted.post_call_orchestrator import OrchestratorContext
    from hosted.runtime_contract import RuntimeResult, SkillRuntime

    class CountedRuntime(SkillRuntime):
        def __init__(self, markdown: str, sidecar: dict[str, Any]) -> None:
            self.markdown = markdown
            self.sidecar = sidecar

        async def execute(self, job: Any, cancellation: Any) -> Any:
            output_dir = Path(job.output_workspace)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
            (output_dir / "sidecar.json").write_text(json.dumps(self.sidecar), encoding="utf-8")
            return RuntimeResult(
                output_artifact=self.markdown,
                sidecar=self.sidecar,
            )

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "cancel-test", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    runtime = CountedRuntime(_valid_full_output(), _full_sidecar())
    orchestrator = await _counting_cancel_orchestrator(
        runtime, worker_pool, backend, cancel_after
    )
    result = await orchestrator.execute(job, OrchestratorContext())
    assert result.error_category == "cancelled"
    assert result.finalized is True

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM public.outputs WHERE job_id = $1", job_id
        )
    assert row["cnt"] == 0

    from hosted import storage as hosted_storage

    key = None
    for k in backend.objects:
        if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:"):
            key = k
    assert key is None, backend.objects


async def test_completion_failure_recovers_without_rerunning_runtime(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A failed complete_job leaves staged evidence; the next attempt finalizes it without the runtime."""
    from hosted.post_call_orchestrator import OrchestratorContext, PostCallOrchestrator
    from hosted.runtime_contract import RuntimeResult, SkillRuntime

    class CountedRuntime(SkillRuntime):
        def __init__(self, markdown: str, sidecar: dict[str, Any]) -> None:
            self.markdown = markdown
            self.sidecar = sidecar
            self.calls = 0

        async def execute(self, job: Any, cancellation: Any) -> Any:
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("runtime should only run once")
            output_dir = Path(job.output_workspace)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
            (output_dir / "sidecar.json").write_text(json.dumps(self.sidecar), encoding="utf-8")
            return RuntimeResult(
                output_artifact=self.markdown,
                sidecar=self.sidecar,
            )

    class FailOnceComplete(PostCallOrchestrator):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.complete_calls = 0

        async def _complete_job_sql(
            self,
            job: dict[str, Any],
            attempt_number: int,
            lease_token: uuid.UUID,
            persisted: Any,
        ) -> str:
            self.complete_calls += 1
            if self.complete_calls == 1:
                raise RuntimeError("injected complete_job failure")
            return await super()._complete_job_sql(job, attempt_number, lease_token, persisted)

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "recover-test", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    runtime = CountedRuntime(_valid_full_output(), _full_sidecar())
    orchestrator = FailOnceComplete(runtime, db_pool=worker_pool, storage_backend=backend)

    result1 = await orchestrator.execute(job, OrchestratorContext())
    assert result1.error_category == "runtime_error"
    assert result1.finalized is False
    assert runtime.calls == 1

    # The staged output row and object exist; the next attempt reconciles them.
    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM public.outputs WHERE job_id = $1", job_id
        )
    assert row["cnt"] == 1

    result2 = await orchestrator.execute(job, OrchestratorContext())
    assert result2.error_category is None
    assert result2.output_id is not None
    assert result2.validation_status == "valid"
    assert result2.finalized is True
    assert runtime.calls == 1, "runtime must not be invoked on recovery"

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "success"
    assert detail["job"]["result_output_id"] == str(result2.output_id)


@pytest.mark.parametrize(
    "validation_status,tombstoned",
    [
        pytest.param("valid", False, id="valid_is_visible"),
        pytest.param("unvalidated", False, id="unvalidated_is_hidden"),
        pytest.param("invalid", False, id="invalid_is_hidden"),
        pytest.param("valid", True, id="tombstoned_valid_is_hidden"),
    ],
)
async def test_outputs_rls_hides_unvalidated_invalid_and_tombstoned_from_app_user(
    admin_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    validation_status: str,
    tombstoned: bool,
) -> None:
    """The tenant role can only read valid, non-tombstoned outputs at the DB boundary."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    tombstoned_at = datetime.now(tz=timezone.utc) if tombstoned else None

    async with admin_pool.acquire() as conn:
        job_row = await conn.fetchrow(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, 'post-call', '1.0', 'success', 1)
            RETURNING id
            """,
            org_id,
            account_id,
            transcript_id,
            user_id,
        )
        job_id = job_row["id"]
        output_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status, tombstoned_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'post-call', $10, $11)
            """,
            output_id,
            org_id,
            job_id,
            account_id,
            transcript_id,
            user_id,
            f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md",
            "title",
            json.dumps({}),
            validation_status,
            tombstoned_at,
        )

    context_token = _context_token(user_id)
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.context_token', $1, true)", context_token)
            rows = await conn.fetch(
                "SELECT id, validation_status, tombstoned_at FROM public.outputs WHERE org_id = $1",
                org_id,
            )

    if validation_status == "valid" and not tombstoned:
        assert len(rows) == 1
        assert rows[0]["id"] == output_id
    else:
        assert len(rows) == 0


@pytest.mark.parametrize(
    "cancel_after,description",
    [
        pytest.param(3, "cancellation after metadata row creation", id="after_metadata"),
        pytest.param(4, "cancellation after storage upload", id="after_upload"),
    ],
)
async def test_cleanup_failure_leaves_tombstone_and_cancels_job(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    cancel_after: int,
    description: str,
) -> None:
    """A cancelled/timeout cleanup that cannot delete the Storage object leaves a tombstone."""
    from hosted import storage as hosted_storage
    from hosted.post_call_orchestrator import OrchestratorContext
    from hosted.runtime_contract import RuntimeResult, SkillRuntime

    class FailingDeleteMemoryBackend(hosted_storage.MemoryStorageBackend):
        """Memory backend that fails the Nth delete call to simulate Storage errors."""

        def __init__(self, *args: Any, fail_after: int = 1, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._delete_calls = 0
            self._fail_after = fail_after

        async def delete(
            self,
            user_id: uuid.UUID | None,
            path: str,
            bucket: str = hosted_storage.DEFAULT_BUCKET,
        ) -> None:
            self._delete_calls += 1
            if self._delete_calls == self._fail_after:
                raise hosted_storage.StorageError("injected storage delete failure")
            return await super().delete(user_id, path, bucket)

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    failing_backend = FailingDeleteMemoryBackend(admin_pool=admin_pool, fail_after=1)
    await _upload_transcript_content(
        failing_backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "cleanup-fail", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    class CountedRuntime(SkillRuntime):
        def __init__(self, markdown: str, sidecar: dict[str, Any]) -> None:
            self.markdown = markdown
            self.sidecar = sidecar

        async def execute(self, job: Any, cancellation: Any) -> Any:
            output_dir = Path(job.output_workspace)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
            (output_dir / "sidecar.json").write_text(json.dumps(self.sidecar), encoding="utf-8")
            return RuntimeResult(output_artifact=self.markdown, sidecar=self.sidecar)

    runtime = CountedRuntime(_valid_full_output(), _full_sidecar())
    orchestrator = await _counting_cancel_orchestrator(
        runtime, worker_pool, failing_backend, cancel_after
    )
    result = await orchestrator.execute(job, OrchestratorContext())
    assert result.error_category == "cleanup_error"
    assert result.output_id is None
    assert result.finalized is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "cancelled"

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE job_id = $1", job_id)
    assert row is not None
    assert row["validation_status"] == "unvalidated"
    assert row["tombstoned_at"] is not None

    output_keys = [
        k for k in failing_backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    if cancel_after == 4:
        # The object was uploaded before cleanup failed, so it remains in Storage.
        assert len(output_keys) == 1
    else:
        # The object was never uploaded, so only the metadata tombstone remains.
        assert len(output_keys) == 0

    # The tenant API never exposes the tombstoned output.
    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/outputs",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert list_resp.json()["outputs"] == []


async def test_delete_job_output_preserves_tombstone_when_attempt_is_finished(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """If delete_job_output cannot run because the attempt finished, the tombstone remains."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    output_id = uuid.uuid4()
    storage_path = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "db-fail", 60
        )
    assert claim is not None

    async with admin_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'post-call', 'unvalidated')
            """,
            output_id,
            org_id,
            job_id,
            account_id,
            transcript_id,
            user_id,
            storage_path,
            "title",
            json.dumps({}),
        )

    await worker_pool.fetchval(
        "SELECT public.tombstone_job_output($1, $2, $3, $4)",
        output_id,
        job_id,
        claim["attempt_number"],
        claim["lease_token"],
    )

    # Simulate a terminal state where delete_job_output cannot proceed.
    async with admin_pool.acquire() as conn:
        await conn.execute("UPDATE public.jobs SET status = 'cancelled' WHERE id = $1", job_id)
        await conn.execute(
            "UPDATE public.job_attempts SET outcome = 'cancelled' WHERE job_id = $1 AND attempt_number = $2",
            job_id,
            claim["attempt_number"],
        )

    with pytest.raises(asyncpg.exceptions.RaiseError):
        await worker_pool.fetchval(
            "SELECT public.delete_job_output($1, $2, $3, $4)",
            output_id,
            job_id,
            claim["attempt_number"],
            claim["lease_token"],
        )

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", output_id)
    assert row is not None
    assert row["tombstoned_at"] is not None
    assert row["validation_status"] == "unvalidated"


async def test_cancellation_inside_complete_job_deletes_staged_evidence(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """Cancellation that wins inside complete_job removes the object and tombstone, not just hides it."""
    from hosted import storage as hosted_storage
    from hosted.post_call_orchestrator import OrchestratorContext, PostCallOrchestrator
    from hosted.runtime_contract import RuntimeResult, SkillRuntime

    class CountedRuntime(SkillRuntime):
        def __init__(self, markdown: str, sidecar: dict[str, Any]) -> None:
            self.markdown = markdown
            self.sidecar = sidecar

        async def execute(self, job: Any, cancellation: Any) -> Any:
            output_dir = Path(job.output_workspace)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
            (output_dir / "sidecar.json").write_text(
                json.dumps(self.sidecar), encoding="utf-8"
            )
            return RuntimeResult(
                output_artifact=self.markdown,
                sidecar=self.sidecar,
            )

    class CancelDuringComplete(PostCallOrchestrator):
        _admin_pool: asyncpg.Pool | None = None

        async def _complete_job_sql(
            self,
            job: dict[str, Any],
            attempt_number: int,
            lease_token: uuid.UUID,
            persisted: Any,
        ) -> str:
            if self._admin_pool is not None:
                async with self._admin_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE public.jobs SET cancel_requested_at = clock_timestamp() WHERE id = $1",
                        uuid.UUID(job["job_id"]),
                    )
            return await super()._complete_job_sql(
                job, attempt_number, lease_token, persisted
            )

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "skill": "post-call",
        },
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "complete-race", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    runtime = CountedRuntime(_valid_full_output(), _full_sidecar())
    orchestrator = CancelDuringComplete(
        runtime, db_pool=worker_pool, storage_backend=backend
    )
    orchestrator._admin_pool = admin_pool

    result = await orchestrator.execute(job, OrchestratorContext())
    assert result.error_category == "cancelled"
    assert result.finalized is True

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM public.outputs WHERE job_id = $1", job_id
        )
    assert row["cnt"] == 0

    output_keys = [
        k for k in backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 0

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/outputs",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert list_resp.json()["outputs"] == []


async def test_tombstone_cleanup_retry_after_storage_delete_failure(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A failed Storage delete leaves a claimable tombstone; a later cleanup pass removes it."""
    from hosted import storage as hosted_storage
    from hosted.post_call_orchestrator import CleanupError, PostCallOrchestrator
    from hosted.runtime_contract import SkillRuntime

    class DummyRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            raise AssertionError("runtime should not be invoked for cleanup")

    class FailingDeleteMemoryBackend(hosted_storage.MemoryStorageBackend):
        def __init__(self, *args: Any, fail_after: int = 1, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._delete_calls = 0
            self._fail_after = fail_after

        async def delete(
            self,
            user_id: uuid.UUID | None,
            path: str,
            bucket: str = hosted_storage.DEFAULT_BUCKET,
        ) -> None:
            self._delete_calls += 1
            if self._delete_calls == self._fail_after:
                raise hosted_storage.StorageError("injected storage delete failure")
            return await super().delete(user_id, path, bucket)

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    output_id = uuid.uuid4()
    job_id = uuid.uuid4()
    storage_path = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"

    async with admin_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.jobs (
                id, org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, $5, 'post-call', '1.0', 'cancelled', 1)
            """,
            job_id,
            org_id,
            account_id,
            transcript_id,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status,
                tombstoned_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'post-call',
                      'unvalidated', clock_timestamp())
            """,
            output_id,
            org_id,
            job_id,
            account_id,
            transcript_id,
            user_id,
            storage_path,
            "title",
            json.dumps({}),
        )

    failing_backend = FailingDeleteMemoryBackend(admin_pool=admin_pool, fail_after=1)

    async def _stream() -> AsyncGenerator[bytes, None]:
        yield b"staged output bytes"

    await failing_backend.upload(
        user_id,
        storage_path,
        _stream(),
        "text/markdown; charset=utf-8",
        bucket=hosted_storage.OUTPUTS_BUCKET,
    )

    context_token = _context_token(user_id)
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", context_token
            )
            rows = await conn.fetch(
                "SELECT id FROM public.outputs WHERE org_id = $1", org_id
            )
    assert len(rows) == 0

    orchestrator = PostCallOrchestrator(
        DummyRuntime(), db_pool=worker_pool, storage_backend=failing_backend
    )
    worker_id = "cleanup-retry-worker"

    with pytest.raises(CleanupError):
        await orchestrator.cleanup_tombstone(output_id, worker_id)

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", output_id)
    assert row is not None
    assert row["tombstoned_at"] is not None
    assert row["cleanup_claimed_by"] == worker_id
    output_keys = [
        k
        for k in failing_backend.objects
        if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 1

    success = await orchestrator.cleanup_tombstone(output_id, worker_id)
    assert success is True

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", output_id)
    assert row is None
    output_keys = [
        k
        for k in failing_backend.objects
        if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 0

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", context_token
            )
            rows = await conn.fetch(
                "SELECT id FROM public.outputs WHERE org_id = $1", org_id
            )
    assert len(rows) == 0


async def test_concurrent_cleanup_workers_cannot_claim_same_tombstone(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    backend: Any,
) -> None:
    """Two cleanup workers cannot both claim the same tombstone; the loser sees the lease conflict."""
    from hosted import storage as hosted_storage
    from hosted.post_call_orchestrator import PostCallOrchestrator
    from hosted.runtime_contract import SkillRuntime

    class DummyRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            raise AssertionError("runtime should not be invoked for cleanup")

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    output_id = uuid.uuid4()
    job_id = uuid.uuid4()
    storage_path = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"

    async with admin_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.jobs (
                id, org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, $5, 'post-call', '1.0', 'cancelled', 1)
            """,
            job_id,
            org_id,
            account_id,
            transcript_id,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status,
                tombstoned_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'post-call',
                      'unvalidated', clock_timestamp())
            """,
            output_id,
            org_id,
            job_id,
            account_id,
            transcript_id,
            user_id,
            storage_path,
            "title",
            json.dumps({}),
        )

    async def _stream() -> AsyncGenerator[bytes, None]:
        yield b"staged output bytes"

    await backend.upload(
        user_id,
        storage_path,
        _stream(),
        "text/markdown; charset=utf-8",
        bucket=hosted_storage.OUTPUTS_BUCKET,
    )

    context_token = _context_token(user_id)
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", context_token
            )
            rows = await conn.fetch(
                "SELECT id FROM public.outputs WHERE org_id = $1", org_id
            )
    assert len(rows) == 0

    barrier = asyncio.Barrier(2)

    async def claim(worker_id: str) -> tuple[str, str, Any]:
        async with worker_pool.acquire() as conn:
            await barrier.wait()
            try:
                row = await conn.fetchrow(
                    "SELECT content_storage_path FROM public.claim_tombstoned_output($1, $2, $3)",
                    output_id,
                    worker_id,
                    60,
                )
            except asyncpg.exceptions.RaiseError as exc:
                return (worker_id, "conflict", str(exc))
            return (worker_id, "claimed", row["content_storage_path"] if row else None)

    results = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    claimed = [r for r in results if r[1] == "claimed"]
    conflicts = [r for r in results if r[1] == "conflict"]
    assert len(claimed) == 1
    assert len(conflicts) == 1

    winner_id = claimed[0][0]
    orchestrator = PostCallOrchestrator(
        DummyRuntime(), db_pool=worker_pool, storage_backend=backend
    )
    success = await orchestrator.cleanup_tombstone(output_id, winner_id)
    assert success is True

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", output_id)
    assert row is None
    output_keys = [
        k for k in backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 0

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", context_token
            )
            rows = await conn.fetch(
                "SELECT id FROM public.outputs WHERE org_id = $1", org_id
            )
    assert len(rows) == 0


async def test_symlink_in_output_workspace_is_redacted_and_removed(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A runtime that writes a symlink as output.md produces a redacted output_error and cleanup."""
    from hosted.post_call_orchestrator import OrchestratorContext, PostCallOrchestrator
    from hosted.runtime_contract import RuntimeResult, SkillRuntime

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "symlink-test", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    class SymlinkRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            output_dir = Path(job.output_workspace)
            output_dir.mkdir(parents=True, exist_ok=True)
            self.output_workspace = str(output_dir)
            self.secret_path = Path(tempfile.mktemp(prefix="symlink-target-"))
            self.secret_path.write_text("secret content", encoding="utf-8")
            (output_dir / "output.md").symlink_to(self.secret_path)
            (output_dir / "sidecar.json").write_text(
                json.dumps(_full_sidecar()), encoding="utf-8"
            )
            return RuntimeResult(output_artifact="", sidecar=_full_sidecar())

    runtime = SymlinkRuntime()
    orchestrator = PostCallOrchestrator(runtime, db_pool=worker_pool, storage_backend=backend)
    result = await orchestrator.execute(job, OrchestratorContext())
    assert result.error_category == "output_error"
    assert result.output_id is None
    assert result.finalized is False

    assert not Path(runtime.output_workspace).exists()
    assert runtime.secret_path.exists()
    assert runtime.secret_path.read_text(encoding="utf-8") == "secret content"


class _FailingDeleteMemoryBackend(hosted_storage.MemoryStorageBackend):
    """Memory backend that fails the Nth delete call to simulate Storage errors."""

    def __init__(self, *args: Any, fail_after: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._delete_calls = 0
        self._fail_after = fail_after

    async def delete(
        self,
        user_id: uuid.UUID | None,
        path: str,
        bucket: str = hosted_storage.DEFAULT_BUCKET,
    ) -> None:
        self._delete_calls += 1
        if self._delete_calls == self._fail_after:
            raise hosted_storage.StorageError("injected storage delete failure")
        return await super().delete(user_id, path, bucket)


async def _create_tombstone_output(
    admin_pool: asyncpg.Pool,
    backend: Any,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    worker_name: str | None = None,
    claimed_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert a cancelled job and a tombstoned output row with a Storage object."""
    async with admin_pool.acquire() as conn:
        job_row = await conn.fetchrow(
            """
            INSERT INTO public.jobs (
                id, org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES (gen_random_uuid(), $1, $2, $3, $4, 'post-call', '1.0', 'cancelled', 1)
            RETURNING id
            """,
            org_id,
            account_id,
            transcript_id,
            user_id,
        )
        job_id = job_row["id"]
        output_id = uuid.uuid4()
        storage_path = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, validation_status,
                tombstoned_at, cleanup_claimed_by, cleanup_claimed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'post-call',
                      'unvalidated', clock_timestamp(), $10, $11)
            """,
            output_id,
            org_id,
            job_id,
            account_id,
            transcript_id,
            user_id,
            storage_path,
            "title",
            json.dumps({}),
            worker_name,
            claimed_at,
        )

    async def _stream() -> AsyncGenerator[bytes, None]:
        yield b"staged output bytes"

    await backend.upload(
        user_id,
        storage_path,
        _stream(),
        "text/markdown; charset=utf-8",
        bucket=hosted_storage.OUTPUTS_BUCKET,
    )
    return job_id, output_id, storage_path


async def _seed_and_upload_transcript(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
    text: str = "Discovery call.",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a user/org/account/transcript and upload the transcript file."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(backend, user_id, org_id, account_id, transcript_id, text)
    return user_id, org_id, account_id, transcript_id


class _CountedRuntime(SkillRuntime):
    """Runtime that records how many times it was invoked."""

    def __init__(self, markdown: str, sidecar: dict[str, Any]) -> None:
        self.markdown = markdown
        self.sidecar = sidecar
        self.calls = 0

    async def execute(self, job: Any, cancellation: Any) -> Any:
        self.calls += 1
        output_dir = Path(job.output_workspace)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "output.md").write_text(self.markdown, encoding="utf-8")
        (output_dir / "sidecar.json").write_text(json.dumps(self.sidecar), encoding="utf-8")
        return RuntimeResult(output_artifact=self.markdown, sidecar=self.sidecar)


class _FailFirstCompleteThenCancel(PostCallOrchestrator):
    """Orchestrator that raises on the first complete_job call and cancels on the second."""

    _admin_pool: asyncpg.Pool | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.complete_calls = 0

    async def _complete_job_sql(
        self,
        job: dict[str, Any],
        attempt_number: int,
        lease_token: uuid.UUID,
        persisted: Any,
    ) -> str:
        self.complete_calls += 1
        if self.complete_calls == 1:
            raise RuntimeError("injected complete_job failure")
        if self._admin_pool is not None:
            async with self._admin_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE public.jobs SET cancel_requested_at = clock_timestamp() WHERE id = $1",
                    uuid.UUID(job["job_id"]),
                )
        return await super()._complete_job_sql(job, attempt_number, lease_token, persisted)


async def test_reconciliation_cancel_inside_complete_job_deletes_staged_evidence(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    backend: Any,
) -> None:
    """A staged output being reconciled is removed when cancellation wins inside complete_job."""
    from hosted.post_call_orchestrator import OrchestratorContext, PostCallOrchestrator
    from hosted.runtime_contract import SkillRuntime
    from hosted.worker import Worker

    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _upload_transcript_content(
        backend, user_id, org_id, account_id, transcript_id, "Discovery call."
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id), "skill": "post-call"},
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "reconcile-cancel", 60
        )
    assert claim is not None
    job = _job_from_claim(claim)

    runtime = _CountedRuntime(_valid_full_output(), _full_sidecar())
    orchestrator = _FailFirstCompleteThenCancel(
        runtime, db_pool=worker_pool, storage_backend=backend
    )
    orchestrator._admin_pool = admin_pool

    result1 = await orchestrator.execute(job, OrchestratorContext())
    assert result1.error_category is not None
    assert result1.finalized is False
    assert runtime.calls == 1

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM public.outputs WHERE job_id = $1", job_id
        )
    assert row["cnt"] == 1

    result2 = await orchestrator.execute(job, OrchestratorContext())
    assert result2.error_category == "cancelled"
    assert result2.finalized is True
    assert result2.output_id is None
    assert runtime.calls == 1, "runtime must not be rerun during reconciliation"

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM public.outputs WHERE job_id = $1", job_id
        )
    assert row["cnt"] == 0

    output_keys = [
        k for k in backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 0

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    ).json()
    assert detail["job"]["status"] == "cancelled"
    assert detail["job"]["result_output_id"] is None

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/outputs",
        headers=_auth_header(user_id, "orch-test@airbyte.io"),
    )
    assert list_resp.json()["outputs"] == []


async def test_worker_poll_discovers_and_removes_tombstone_after_storage_delete_failure(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A worker poll discovers a tombstone, retries a failed Storage delete, and removes it."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.runtime_contract import SkillRuntime
    from hosted.worker import Worker

    class DummyRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            raise AssertionError("runtime should not be invoked for cleanup")

    failing_backend = _FailingDeleteMemoryBackend(admin_pool=admin_pool, fail_after=1)
    user_id, org_id, account_id, transcript_id = await _seed_and_upload_transcript(
        admin_pool, app_client, failing_backend
    )

    job_id, output_id, _ = await _create_tombstone_output(
        admin_pool, failing_backend, user_id, org_id, account_id, transcript_id
    )

    executor = PostCallExecutor(
        DummyRuntime(), db_pool=worker_pool, storage_backend=failing_backend
    )
    worker = Worker(worker_pool, executor=executor, worker_name="cleanup-poll-1")

    # First poll: cleanup fails on the Storage delete, leaving a claimed tombstone.
    processed = await worker.run_once()
    assert processed is False

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", output_id)
    assert row is not None
    assert row["tombstoned_at"] is not None
    assert row["cleanup_claimed_by"] == "cleanup-poll-1"

    output_keys = [
        k for k in failing_backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 1

    # Second poll: the same worker re-claims and the Storage delete now succeeds.
    processed = await worker.run_once()
    assert processed is False

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", output_id)
    assert row is None

    output_keys = [
        k for k in failing_backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 0

    context_token = _context_token(user_id)
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", context_token
            )
            rows = await conn.fetch(
                "SELECT id FROM public.outputs WHERE org_id = $1", org_id
            )
    assert len(rows) == 0


@pytest.mark.parametrize("tombstone_count", [1, 2])
async def test_concurrent_cleanup_workers_claim_tombstones(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
    tombstone_count: int,
) -> None:
    """Concurrent cleanup workers claim different tombstones or only one claims a single tombstone."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.runtime_contract import SkillRuntime

    class DummyRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            raise AssertionError("runtime should not be invoked for cleanup")

    test_backend = hosted_storage.MemoryStorageBackend(admin_pool=admin_pool)
    user_id, org_id, account_id, transcript_id = await _seed_and_upload_transcript(
        admin_pool, app_client, test_backend
    )

    tombstones: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for _ in range(tombstone_count):
        job_id, output_id, _ = await _create_tombstone_output(
            admin_pool, test_backend, user_id, org_id, account_id, transcript_id
        )
        tombstones.append((job_id, output_id, _))

    barrier = asyncio.Barrier(2)

    async def cleanup(worker_name: str) -> bool:
        executor = PostCallExecutor(
            DummyRuntime(), db_pool=worker_pool, storage_backend=test_backend
        )
        await barrier.wait()
        try:
            return await executor.cleanup_next_tombstone(worker_name, lease_seconds=60)
        except Exception:
            return False

    results = await asyncio.gather(cleanup("worker-a"), cleanup("worker-b"))
    successes = [r for r in results if r]
    failures = [r for r in results if not r]

    assert len(successes) == tombstone_count
    assert len(failures) == 2 - tombstone_count

    async with admin_pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM public.outputs WHERE job_id = ANY($1)",
            [t[0] for t in tombstones],
        )
    assert remaining == 0

    output_keys = [
        k for k in test_backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 0


async def test_expired_claim_is_reclaimed_and_live_claim_is_not(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """An expired cleanup claim can be stolen by another worker; a live claim cannot."""
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.runtime_contract import SkillRuntime

    class DummyRuntime(SkillRuntime):
        async def execute(self, job: Any, cancellation: Any) -> Any:
            raise AssertionError("runtime should not be invoked for cleanup")

    test_backend = hosted_storage.MemoryStorageBackend(admin_pool=admin_pool)
    user_id, org_id, account_id, transcript_id = await _seed_and_upload_transcript(
        admin_pool, app_client, test_backend
    )

    # Expired claim: worker1 claimed 2 seconds ago with a 1-second lease.
    _, expired_output_id, _ = await _create_tombstone_output(
        admin_pool,
        test_backend,
        user_id,
        org_id,
        account_id,
        transcript_id,
        worker_name="worker-expired",
        claimed_at=datetime.now(tz=timezone.utc) - timedelta(seconds=2),
    )

    # Live claim: worker1 just claimed with a 60-second lease.
    _, live_output_id, _ = await _create_tombstone_output(
        admin_pool,
        test_backend,
        user_id,
        org_id,
        account_id,
        transcript_id,
        worker_name="worker-live",
        claimed_at=datetime.now(tz=timezone.utc),
    )

    executor = PostCallExecutor(
        DummyRuntime(), db_pool=worker_pool, storage_backend=test_backend
    )

    # Another worker can reclaim the expired tombstone.
    success = await executor.cleanup_next_tombstone("worker-2", lease_seconds=1)
    assert success is True

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", expired_output_id)
    assert row is None

    # The live-claimed tombstone is not reclaimed and its object remains.
    success = await executor.cleanup_next_tombstone("worker-2", lease_seconds=60)
    assert success is False

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM public.outputs WHERE id = $1", live_output_id)
    assert row is not None

    output_keys = [
        k for k in test_backend.objects if k.startswith(f"{hosted_storage.OUTPUTS_BUCKET}:")
    ]
    assert len(output_keys) == 1

    context_token = _context_token(user_id)
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", context_token
            )
            rows = await conn.fetch(
                "SELECT id FROM public.outputs WHERE org_id = $1", org_id
            )
    assert len(rows) == 0


async def test_claim_next_tombstoned_output_validates_worker_id_and_lease(
    worker_pool: asyncpg.Pool,
) -> None:
    """The cleanup queue rejects empty worker ids and invalid lease durations."""
    async with worker_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.fetchrow(
                "SELECT * FROM public.claim_next_tombstoned_output($1, $2)",
                "",
                60,
            )
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.fetchrow(
                "SELECT * FROM public.claim_next_tombstoned_output($1, $2)",
                "worker",
                0,
            )
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.fetchrow(
                "SELECT * FROM public.claim_next_tombstoned_output($1, $2)",
                "worker",
                3601,
            )
