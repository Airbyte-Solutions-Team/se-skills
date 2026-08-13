"""Deterministic integration tests for the trusted post-call orchestrator."""
from __future__ import annotations

import asyncio
import json
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

pytestmark = [pytest.mark.asyncio, pytest.mark.hosted]


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
                content_storage_path, title, sidecar, skill
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'post-call')
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

    # Second attempt with the same running lease: the unvalidated row already
    # exists, the object is missing, and the orchestrator re-uploads the content.
    result = await executor.execute(job)
    assert result.error_category is None
    assert result.output_id is not None
    assert result.validation_status == "valid"

    async with worker_pool.acquire() as conn:
        await conn.execute(
            """
            SELECT public.complete_job(
                $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9
            )
            """,
            job_id,
            job["attempt_number"],
            job["lease_token"],
            result.output_id,
            result.validation_status,
            json.dumps(result.token_usage),
            result.cost,
            result.runtime_version,
            result.model,
        )

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
