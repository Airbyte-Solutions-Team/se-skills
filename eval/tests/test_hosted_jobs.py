"""Integration tests for the hosted durable job ledger and worker lifecycle."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

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
    _seed_user_and_membership,
)
from webapp.hosted.worker import Worker


pytestmark = [pytest.mark.asyncio]


@pytest.fixture(autouse=True)
async def _clean_jobs(admin_pool: asyncpg.Pool) -> None:
    """Reset the job ledger between tests so workers always see predictable state."""
    async with admin_pool.acquire() as conn:
        await conn.execute("TRUNCATE public.job_attempts, public.jobs CASCADE")


async def _seed_job_ready_org(admin_pool: asyncpg.Pool) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    """Create a member, account, transcript, and return ids."""
    user_id, org_id, _ = await _seed_member(admin_pool, "job-test@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    transcript_id = await _seed_transcript(admin_pool, org_id, account_id, None, user_id)
    return user_id, org_id, account_id, transcript_id, None


async def _seed_job_ready_org_with_opp(
    admin_pool: asyncpg.Pool,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, org_id, _ = await _seed_member(admin_pool, "job-test-opp@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    opportunity_id = await _seed_opportunity(admin_pool, org_id, account_id, user_id)
    transcript_id = await _seed_transcript(
        admin_pool, org_id, account_id, opportunity_id, user_id
    )
    return user_id, org_id, account_id, transcript_id, opportunity_id


@pytest.mark.hosted
async def test_member_can_enqueue_job_for_transcript(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """An authenticated active member can enqueue a job and receive a durable job_id."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "skill": "post-call",
        },
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["account_id"] == str(account_id)
    assert data["transcript_id"] == str(transcript_id)
    assert data["org_id"] == str(org_id)
    assert data["requester_id"] == str(user_id)
    assert data["status"] == "queued"
    assert data["skill"] == "post-call"
    assert data["payload"] == {
        "skill": "post-call",
        "model": "echo",
        "runtime_version": "slice4",
    }
    assert data["input_refs"] == {"transcript_id": str(transcript_id)}
    assert data["source_manifest"]["transcript_id"] == str(transcript_id)
    assert "storage_path" in data["source_manifest"]
    assert data["attempts"] == 0
    assert data["max_attempts"] == 3
    assert data["dead_lettered"] is False


@pytest.mark.hosted
@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("enqueue", id="enqueue"),
        pytest.param("list", id="list_jobs"),
        pytest.param("cancel", id="cancel_job"),
    ],
)
async def test_unauthenticated_requests_fail(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
    mutation: str,
) -> None:
    """Unauthenticated requests to job APIs are rejected."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)

    if mutation == "enqueue":
        response = app_client.post(
            f"/api/hosted/accounts/{account_id}/jobs",
            json={"account_id": str(account_id), "transcript_id": str(transcript_id)},
        )
        assert response.status_code == 401
    elif mutation == "list_jobs":
        response = app_client.get(f"/api/hosted/accounts/{account_id}/jobs")
        assert response.status_code == 401
    else:
        # Cancel requires a job, but the auth failure happens before lookup.
        response = app_client.post(f"/api/hosted/jobs/{uuid.uuid4()}/cancel")
        assert response.status_code == 401


@pytest.mark.hosted
async def test_inactive_member_cannot_enqueue(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """An inactive member cannot enqueue jobs."""
    user_id, org_id, _ = await _seed_member(admin_pool, "inactive-job@airbyte.io", active=False)
    account_id = await _seed_account(admin_pool, org_id, user_id)
    transcript_id = await _seed_transcript(admin_pool, org_id, account_id, None, user_id)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id)},
        headers=_auth_header(user_id, "inactive-job@airbyte.io"),
    )
    assert response.status_code in (401, 403)


@pytest.mark.hosted
async def test_non_member_cannot_enqueue(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A user with no membership in the target org cannot enqueue jobs."""
    user_a, org_a, _ = await _seed_member(admin_pool, "a@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)
    transcript_a = await _seed_transcript(admin_pool, org_a, account_a, None, user_a)

    user_b, _, _ = await _seed_member(admin_pool, "b@airbyte.io")

    response = app_client.post(
        f"/api/hosted/accounts/{account_a}/jobs",
        json={"account_id": str(account_a), "transcript_id": str(transcript_a)},
        headers=_auth_header(user_b, "b@airbyte.io"),
    )
    assert response.status_code in (401, 403, 404)


@pytest.mark.hosted
async def test_spoofed_org_id_in_body_is_ignored(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """The browser cannot choose the organization; org is derived from membership."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    other_org = uuid.uuid4()

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "org_id": str(other_org),
        },
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code == 201
    assert response.json()["org_id"] == str(org_id)


@pytest.mark.hosted
async def test_cross_org_account_or_transcript_rejected(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A member cannot enqueue a job against another organization's transcript."""
    user_a, org_a, _ = await _seed_member(admin_pool, "org-a@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)
    transcript_a = await _seed_transcript(admin_pool, org_a, account_a, None, user_a)

    user_b, org_b, _ = await _seed_member(admin_pool, "org-b@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)
    _ = await _seed_transcript(admin_pool, org_b, account_b, None, user_b)

    # User B tries to use account_a and transcript_a with their own auth.
    response = app_client.post(
        f"/api/hosted/accounts/{account_a}/jobs",
        json={"account_id": str(account_a), "transcript_id": str(transcript_a)},
        headers=_auth_header(user_b, "org-b@airbyte.io"),
    )
    assert response.status_code in (400, 401, 403, 404)


@pytest.mark.hosted
async def test_opportunity_must_belong_to_account_and_org(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Opportunity must be in the same account and organization as the transcript."""
    user_id, org_id, account_id, transcript_id, opportunity_id = await _seed_job_ready_org_with_opp(
        admin_pool
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "opportunity_id": str(opportunity_id),
        },
        headers=_auth_header(user_id, "job-test-opp@airbyte.io"),
    )
    assert response.status_code == 201


@pytest.mark.hosted
async def test_mismatched_opportunity_for_account_rejected(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A job fails when the opportunity does not belong to the selected account."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    other_account = await _seed_account(admin_pool, org_id, user_id, name="Other Account")
    other_opportunity = await _seed_opportunity(
        admin_pool, org_id, other_account, user_id, name="Other Opp"
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "opportunity_id": str(other_opportunity),
        },
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code in (400, 404)


@pytest.mark.hosted
async def test_worker_claims_job_and_reaches_success(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A worker claims a queued job, runs it, and records a successful terminal state."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={"account_id": str(account_id), "transcript_id": str(transcript_id)},
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    worker = Worker(worker_pool, worker_name="test-worker-1", timeout_seconds=5)
    processed = await worker.run_once()
    assert processed is True

    detail = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["job"]["status"] == "success"
    assert body["job"]["attempts"] == 1
    assert body["job"]["finished_at"] is not None
    assert body["attempts"][0]["outcome"] == "success"
    assert body["attempts"][0]["worker_id"] == "test-worker-1"


@pytest.mark.hosted
async def test_worker_can_heartbeat_and_complete_only_with_current_lease(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A worker with the current lease token can heartbeat and complete."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim = await _claim(worker_pool, "worker-1", timeout_seconds=5)
    assert claim is not None
    assert str(claim["job_id"]) == job_id

    await _heartbeat(worker_pool, claim, 5)
    await _complete_job(
        worker_pool,
        claim,
        token_usage={"input_tokens": 0, "output_tokens": 0},
    )

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "success"


@pytest.mark.hosted
async def test_stale_worker_cannot_complete_after_lease_expiry(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A stale worker with an expired lease cannot complete a reclaimed job."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim1 = await _claim(worker_pool, "worker-1", timeout_seconds=1)
    assert claim1 is not None

    # Wait for the lease to expire, then let a second worker recover and reclaim.
    await asyncio.sleep(1.5)
    recovered = await worker_pool.fetchval("SELECT public.recover_expired_leases($1)", 0)
    assert recovered == 1

    claim2 = await _claim(worker_pool, "worker-2", timeout_seconds=10)
    assert claim2 is not None
    assert claim2["attempt_number"] == 2

    # The first worker's lease token must no longer complete the job.
    with pytest.raises(asyncpg.exceptions.PostgresError):
        await _complete_job(worker_pool, claim1)

    # The current worker can still complete it.
    await _complete_job(worker_pool, claim2)

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "success"
    assert detail["job"]["attempts"] == 2


@pytest.mark.hosted
async def test_two_workers_cannot_claim_same_attempt(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
) -> None:
    """Concurrent workers cannot both claim the same queued job attempt."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    await _enqueue_job_direct(admin_pool, user_id, account_id, transcript_id)

    async def claim() -> dict | None:
        return await _claim(worker_pool, "concurrent-worker", timeout_seconds=60)

    results = await asyncio.gather(claim(), claim())
    claims = [r for r in results if r is not None]
    assert len(claims) == 1


@pytest.mark.hosted
async def test_expired_leases_recover_with_backoff(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """An expired running lease records an abandoned attempt and requeues with backoff."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    await _claim(worker_pool, "worker-1", timeout_seconds=1)
    await asyncio.sleep(1.5)

    recovered = await worker_pool.fetchval("SELECT public.recover_expired_leases($1)", 1)
    assert recovered == 1

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "queued"
    assert detail["job"]["next_attempt_after"] is not None
    assert detail["job"]["attempts"] == 1
    assert detail["attempts"][0]["outcome"] == "timeout"
    assert detail["attempts"][0]["error_category"] == "lease_timeout"


@pytest.mark.hosted
async def test_retries_bounded_and_dead_lettered(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Exhausted retries produce a terminal failure with dead_lettered metadata."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id, max_attempts=2)

    for i in range(2):
        claim = await _claim(worker_pool, f"worker-{i}", timeout_seconds=60)
        assert claim is not None
        await _fail_job(
            worker_pool,
            claim,
            "executor_error",
            "Synthetic test failure",
            backoff_seconds=0,
        )

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "failure"
    assert detail["job"]["dead_lettered"] is True
    assert detail["job"]["attempts"] == 2
    assert detail["job"]["next_attempt_after"] is None
    assert len(detail["attempts"]) == 2
    for attempt in detail["attempts"]:
        assert attempt["outcome"] == "failure"


@pytest.mark.hosted
async def test_timeout_is_distinct_from_failure(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A worker-reported timeout becomes a `timeout` terminal status, not `failure`."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id, max_attempts=1)

    claim = await _claim(worker_pool, "timeout-worker", timeout_seconds=60)
    assert claim is not None
    await _fail_job(
        worker_pool,
        claim,
        "timeout",
        "Execution exceeded timeout",
    )

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "timeout"
    assert detail["attempts"][0]["outcome"] == "timeout"


@pytest.mark.hosted
async def test_queued_job_can_be_cancelled_immediately(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A queued job can be cancelled and will not be claimed."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    response = app_client.post(
        f"/api/hosted/jobs/{job_id}/cancel",
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code == 204

    processed = await Worker(worker_pool, worker_name="no-op-worker").run_once()
    assert processed is False

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "cancelled"
    assert detail["job"]["cancelled_at"] is not None


@pytest.mark.hosted
async def test_running_job_cancellation_is_finalized_by_worker(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A running job records a cancellation request; the worker finalizes it."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim = await _claim(worker_pool, "running-cancel", timeout_seconds=60)
    assert claim is not None

    response = app_client.post(
        f"/api/hosted/jobs/{job_id}/cancel",
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code == 204

    await _complete_job(worker_pool, claim)

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "cancelled"
    assert detail["attempts"][0]["outcome"] == "cancelled"


@pytest.mark.hosted
async def test_illegal_transition_of_terminal_job_fails(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Terminal jobs cannot be claimed or completed again."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim = await _claim(worker_pool, "terminal-worker", timeout_seconds=60)
    await _complete_job(worker_pool, claim)

    assert await _claim(worker_pool, "late-worker", timeout_seconds=60) is None

    with pytest.raises(asyncpg.exceptions.PostgresError):
        await _complete_job(worker_pool, claim)


@pytest.mark.hosted
async def test_idempotent_enqueue_returns_same_job(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Replaying an enqueue with the same idempotency key returns the original job."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    key = "idem-123"
    job1 = _enqueue_via_api(
        app_client, user_id, account_id, transcript_id, idempotency_key=key
    )
    job2 = _enqueue_via_api(
        app_client, user_id, account_id, transcript_id, idempotency_key=key
    )
    assert job1 == job2


@pytest.mark.hosted
async def test_conflicting_idempotency_key_returns_conflict(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Reusing an idempotency key with a different scope returns a safe conflict."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    key = "idem-conflict"
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "idempotency_key": key,
            "skill": "post-call",
        },
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code == 201

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json={
            "account_id": str(account_id),
            "transcript_id": str(transcript_id),
            "idempotency_key": key,
            "skill": "different-skill",
        },
        headers=_auth_header(user_id, "job-test@airbyte.io"),
    )
    assert response.status_code == 409


@pytest.mark.hosted
async def test_app_user_cannot_read_other_org_jobs(
    admin_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Direct app_user access cannot read another organization's jobs."""
    user_a, org_a, account_a, transcript_a, _ = await _seed_job_ready_org(admin_pool)
    user_b, org_b, account_b, transcript_b, _ = await _seed_job_ready_org(admin_pool)

    job_a = _enqueue_via_api(app_client, user_a, account_a, transcript_a)
    job_b = _enqueue_via_api(app_client, user_b, account_b, transcript_b)

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_a),
            )
            row = await conn.fetchrow(
                "SELECT * FROM public.jobs WHERE id = $1", job_a
            )
            assert row is not None
            other = await conn.fetchrow(
                "SELECT * FROM public.jobs WHERE id = $1", job_b
            )
            assert other is None
            # user_a should not see org_b's job even when querying by org_id.
            cross = await conn.fetch(
                "SELECT id FROM public.jobs WHERE org_id = $1",
                org_b,
            )
            assert len(cross) == 0


@pytest.mark.hosted
async def test_worker_role_cannot_read_unrelated_tenant_data(
    worker_pool: asyncpg.Pool,
) -> None:
    """The worker role cannot query accounts, transcripts, or memberships directly."""
    async with worker_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM public.accounts LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM public.transcripts LIMIT 1")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM public.memberships LIMIT 1")


@pytest.mark.hosted
async def test_worker_role_can_only_invoke_required_functions(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """The worker role can invoke queue functions but not app_user helpers."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    # Allowed queue operations as the worker role.
    await worker_pool.fetchval("SELECT public.recover_expired_leases()")
    claim = await _claim(worker_pool, "worker-role-test", 60)
    assert claim is not None
    await _heartbeat(worker_pool, claim, 60)
    await _complete_job(worker_pool, claim)

    # Worker should not be able to call the app_user enqueue function.
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await _enqueue_job_direct(
            worker_pool, user_id, account_id, transcript_id
        )


@pytest.mark.hosted
async def test_job_state_survives_api_and_worker_restart(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Jobs remain queued/running after the worker or API process disconnects."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    # Simulate worker disconnect by claiming and not completing, then closing the pool.
    claim = await _claim(worker_pool, "disconnect-worker", timeout_seconds=300)
    assert str(claim["job_id"]) == job_id

    # Simulate API restart by simply opening a fresh view of the job.
    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "running"
    assert detail["job"]["worker_id"] == "disconnect-worker"


@pytest.mark.hosted
async def test_job_payloads_do_not_contain_secrets_or_transcript_bodies(
    admin_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Enqueue stores only stable references and metadata, never transcript bytes."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    detail = _enqueue_and_get(
        app_client, user_id, account_id, transcript_id, payload={"extra": "metadata"}
    )
    payload = detail["job"]["payload"]
    refs = detail["job"]["input_refs"]
    manifest = detail["job"]["source_manifest"]
    assert "transcript_body" not in payload
    assert "token" not in str(manifest).lower()
    assert "password" not in str(manifest).lower()
    assert refs["transcript_id"] == str(transcript_id)


class _BlockingExecutor:
    """Executor that blocks until released, to test timeouts and cancellation."""

    def __init__(self, runtime_version: str = "slice4-blocking", model: str = "blocking") -> None:
        self.runtime_version = runtime_version
        self.model = model
        self._started = asyncio.Event()
        self._release = asyncio.Event()

    async def execute(self, job: dict[str, Any]) -> object:
        self._started.set()
        await self._release.wait()
        # Should only be reached if the test releases before cancellation/timeout.
        from webapp.hosted.executor import ExecutorResult
        return ExecutorResult(
            output_id=None,
            validation_status="unvalidated",
            token_usage={},
            cost=0.0,
            runtime_version=self.runtime_version,
            model=self.model,
        )

    def release(self) -> None:
        self._release.set()


class _FailingExecutor:
    """Executor that blocks until told to raise, to test cancel-vs-failure races."""

    def __init__(self, runtime_version: str = "slice4-failing", model: str = "failing") -> None:
        self.runtime_version = runtime_version
        self.model = model
        self._started = asyncio.Event()
        self._fail = asyncio.Event()

    async def execute(self, job: dict[str, Any]) -> object:
        self._started.set()
        await self._fail.wait()
        raise RuntimeError("executor failure after cancel requested")

    def trigger_failure(self) -> None:
        self._fail.set()


class _HeartbeatFailingWorker(Worker):
    """Worker that injects a heartbeat failure after a configurable number of calls."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        executor: Any | None = None,
        *,
        fail_after: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(pool, executor, **kwargs)
        self.fail_after = fail_after
        self._heartbeat_calls = 0

    async def heartbeat(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
    ) -> bool:
        self._heartbeat_calls += 1
        if self._heartbeat_calls > self.fail_after:
            raise RuntimeError("injected heartbeat failure")
        return await super().heartbeat(job_id, attempt_number, lease_token)


@pytest.mark.hosted
async def test_worker_enforces_wall_clock_deadline_and_reaches_timeout(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A blocking executor that exceeds the wall-clock deadline ends in `timeout`."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id, max_attempts=1)

    blocking = _BlockingExecutor()
    worker = Worker(
        worker_pool,
        executor=blocking,
        worker_name="blocking-timeout",
        heartbeat_interval=0.2,
        timeout_seconds=1,
    )
    processed = await worker.run_once()
    assert processed is True

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "timeout"
    assert detail["attempts"][0]["outcome"] == "timeout"
    assert detail["attempts"][0]["error_category"] == "timeout"
    assert "wall-clock" in (detail["attempts"][0]["error"] or "").lower()


@pytest.mark.hosted
async def test_worker_cancels_blocking_executor_and_reaches_cancelled(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A cancellation request while the executor is blocked reaches `cancelled`."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    blocking = _BlockingExecutor()
    worker = Worker(
        worker_pool,
        executor=blocking,
        worker_name="blocking-cancel",
        heartbeat_interval=0.2,
        timeout_seconds=10,
    )

    process_task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(blocking._started.wait(), timeout=2)

    # Request cancellation through the API using an app_user connection.
    async with user_pool.acquire() as conn:
        cancelled = await conn.fetchval(
            "SELECT public.request_job_cancellation($1, $2)",
            _context_token(user_id),
            job_id,
        )
    assert cancelled is True

    await asyncio.wait_for(process_task, timeout=3)

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "cancelled"
    assert detail["attempts"][0]["outcome"] == "cancelled"


@pytest.mark.hosted
async def test_worker_heartbeat_failure_is_not_user_cancellation(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A heartbeat failure stops the worker without marking the job as user-cancelled."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(
        app_client, user_id, account_id, transcript_id, max_attempts=1
    )

    blocking = _BlockingExecutor()
    worker = _HeartbeatFailingWorker(
        worker_pool,
        executor=blocking,
        fail_after=0,
        worker_name="heartbeat-fail",
        heartbeat_interval=0.2,
        timeout_seconds=1,
    )
    processed = await worker.run_once()
    assert processed is True

    # The worker should not have finalized the attempt as a user cancellation.
    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, cancel_requested_at, cancelled_at FROM public.jobs WHERE id = $1",
            job_id,
        )
    assert row["status"] == "running"
    assert row["cancel_requested_at"] is None
    assert row["cancelled_at"] is None

    # After the lease expires, recovery should produce a timeout, not cancelled.
    await asyncio.sleep(1.5)
    recovered = await worker_pool.fetchval(
        "SELECT public.recover_expired_leases($1)", 0
    )
    assert recovered == 1

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "timeout"
    assert detail["job"]["cancelled_at"] is None
    assert detail["attempts"][0]["outcome"] == "timeout"
    assert detail["attempts"][0]["error_category"] == "lease_timeout"


@pytest.mark.hosted
async def test_cancel_vs_failure_finalizes_attempt_as_cancelled(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A cancellation request racing with an executor error ends terminal and consistent."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    failing = _FailingExecutor()
    worker = Worker(
        worker_pool,
        executor=failing,
        worker_name="cancel-vs-fail",
        heartbeat_interval=10,
        timeout_seconds=60,
    )
    process_task = asyncio.create_task(worker.process_one())
    await asyncio.wait_for(failing._started.wait(), timeout=2)

    # Request cancellation while the executor is still running; the executor will
    # raise before the next heartbeat observes the cancellation request.
    async with user_pool.acquire() as conn:
        cancelled = await conn.fetchval(
            "SELECT public.request_job_cancellation($1, $2)",
            _context_token(user_id),
            job_id,
        )
    assert cancelled is True
    failing.trigger_failure()
    await asyncio.wait_for(process_task, timeout=3)

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "cancelled"
    assert detail["job"]["finished_at"] is not None
    assert not detail["job"]["dead_lettered"]
    assert detail["job"]["next_attempt_after"] is None
    assert len(detail["attempts"]) == 1
    attempt = detail["attempts"][0]
    assert attempt["outcome"] == "cancelled"
    assert attempt["error_category"] == "executor_error"
    assert "executor failure" in (attempt["error"] or "").lower()


@pytest.mark.hosted
async def test_concurrent_cancel_vs_claim_is_consistent(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A cancel request racing with a worker claim ends terminal with no orphan attempt."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    blocking = _BlockingExecutor()
    worker = Worker(
        worker_pool,
        executor=blocking,
        worker_name="race-claim-cancel",
        heartbeat_interval=0.2,
        timeout_seconds=10,
    )

    async def process() -> bool:
        return await worker.process_one()

    async def cancel() -> bool:
        async with user_pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT public.request_job_cancellation($1, $2)",
                _context_token(user_id),
                job_id,
            )

    results = await asyncio.gather(process(), cancel(), return_exceptions=True)
    assert all(isinstance(r, Exception) is False for r in results)

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "cancelled"
    assert detail["job"]["finished_at"] is not None
    open_attempts = [a for a in detail["attempts"] if a["outcome"] is None]
    assert not open_attempts


@pytest.mark.hosted
async def test_concurrent_cancel_vs_complete_is_terminal_and_consistent(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A cancel request racing with a complete finalizer ends terminal with no orphan attempt."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim = await _claim(worker_pool, "race-complete-cancel", timeout_seconds=60)
    assert claim is not None

    async def cancel() -> bool:
        async with user_pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT public.request_job_cancellation($1, $2)",
                _context_token(user_id),
                job_id,
            )

    async def complete() -> bool:
        await _complete_job(worker_pool, claim, token_usage={"input_tokens": 0})
        return True

    results = await asyncio.gather(cancel(), complete(), return_exceptions=True)
    # One call may legitimately raise a transition-race exception.
    exceptions = [r for r in results if isinstance(r, Exception)]
    assert len(exceptions) <= 1

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] in ("cancelled", "success")
    assert detail["job"]["finished_at"] is not None
    open_attempts = [a for a in detail["attempts"] if a["outcome"] is None]
    assert not open_attempts


@pytest.mark.hosted
async def test_recovery_vs_finalize_does_not_leave_open_attempt(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Recovery of an expired lease beats a stale finalizer and closes the attempt."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim = await _claim(worker_pool, "race-recovery", timeout_seconds=1)
    assert claim is not None

    await asyncio.sleep(1.5)
    recovered = await worker_pool.fetchval("SELECT public.recover_expired_leases($1)", 0)
    assert recovered == 1

    with pytest.raises(asyncpg.exceptions.PostgresError):
        await _complete_job(worker_pool, claim)

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] == "queued"
    assert detail["attempts"][0]["outcome"] == "timeout"
    assert detail["attempts"][0]["error_category"] == "lease_timeout"


@pytest.mark.hosted
async def test_recovery_vs_finalize_race_is_terminal_or_requeued(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Recovery and a stale finalizer racing on an expired lease leave no open attempt."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    claim = await _claim(worker_pool, "race-recovery-2", timeout_seconds=1)
    assert claim is not None

    await asyncio.sleep(1.5)

    async def recover() -> int:
        return await worker_pool.fetchval("SELECT public.recover_expired_leases($1)", 0) or 0

    async def complete() -> None:
        await _complete_job(worker_pool, claim)

    results = await asyncio.gather(recover(), complete(), return_exceptions=True)
    exceptions = [r for r in results if isinstance(r, Exception)]
    assert len(exceptions) <= 1

    detail = _get_job(app_client, user_id, job_id)
    assert detail["job"]["status"] in ("queued", "success")
    open_attempts = [a for a in detail["attempts"] if a["outcome"] is None]
    assert not open_attempts


@pytest.mark.hosted
async def test_worker_persists_actual_executor_runtime_metadata(
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """A successful attempt records the executor's actual model and runtime version."""
    user_id, _, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    job_id = _enqueue_via_api(app_client, user_id, account_id, transcript_id)

    worker = Worker(worker_pool, worker_name="runtime-lineage")
    processed = await worker.run_once()
    assert processed is True

    detail = _get_job(app_client, user_id, job_id)
    attempt = detail["attempts"][0]
    assert attempt["outcome"] == "success"
    assert attempt["runtime_version"] == "slice4-echo"
    assert attempt["model"] == "echo"


@pytest.mark.hosted
async def test_simultaneous_identical_enqueue_returns_one_job(
    admin_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Two concurrent identical enqueue requests produce one job and two 201s."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    key = "concurrent-idem"

    async def enqueue() -> dict:
        async with user_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM public.enqueue_job(
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12::jsonb, $13::jsonb, $14::jsonb
                )
                """,
                _context_token(user_id),
                account_id,
                transcript_id,
                None,
                "post-call",
                "1.0",
                "echo",
                "slice4",
                key,
                3,
                300,
                json.dumps({}),
                json.dumps({"transcript_id": str(transcript_id)}),
                json.dumps({"transcript_id": str(transcript_id)}),
            )
        return dict(row) if row else {}

    results = await asyncio.gather(enqueue(), enqueue())
    assert results[0]["job_id"] == results[1]["job_id"]


@pytest.mark.hosted
@pytest.mark.parametrize(
    "field,new_value",
    [
        pytest.param("skill", "different-skill", id="skill"),
        pytest.param("skill_version", "2.0", id="skill_version"),
        pytest.param("model", "different-model", id="model"),
        pytest.param("runtime_version", "different-runtime", id="runtime_version"),
        pytest.param("max_attempts", 5, id="max_attempts"),
        pytest.param("payload", json.dumps({"extra": "different"}), id="payload_jsonb"),
    ],
)
async def test_idempotency_key_conflict_for_each_material_field(
    admin_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
    field: str,
    new_value: Any,
) -> None:
    """Reusing the same idempotency key with a changed material field returns 409."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    key = f"idem-{field}"

    base_args = [
        _context_token(user_id),
        account_id,
        transcript_id,
        None,
        "post-call",
        "1.0",
        "echo",
        "slice4",
        key,
        3,
        300,
        json.dumps({}),
        json.dumps({"transcript_id": str(transcript_id)}),
        json.dumps({"transcript_id": str(transcript_id)}),
    ]

    async with user_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM public.enqueue_job(
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12::jsonb, $13::jsonb, $14::jsonb
            )
            """,
            *base_args,
        )
    job_id = row["job_id"]

    # Build the conflicting call by mutating the chosen field.
    arg_names = [
        "context_token",
        "account_id",
        "transcript_id",
        "opportunity_id",
        "skill",
        "skill_version",
        "model",
        "runtime_version",
        "idempotency_key",
        "max_attempts",
        "timeout_seconds",
        "payload",
        "input_refs",
        "source_manifest",
    ]
    conflict_args = list(base_args)
    field_index = arg_names.index(field)
    conflict_args[field_index] = new_value
    if field == "payload":
        conflict_args[11] = new_value  # payload is at index 11

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.PostgresError):
            await conn.fetchrow(
                """
                SELECT * FROM public.enqueue_job(
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12::jsonb, $13::jsonb, $14::jsonb
                )
                """,
                *conflict_args,
            )

    # Original job should still exist and be the only one for the key.
    async with admin_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM public.jobs WHERE org_id = $1 AND idempotency_key = $2",
            org_id,
            key,
        )
    assert count == 1


@pytest.mark.hosted
async def test_idempotency_key_conflict_for_changed_ids_and_json(
    admin_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    app_client: TestClient,
) -> None:
    """Reusing the key with a changed account, transcript, input_refs, or source_manifest is a 409."""
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_ready_org(admin_pool)
    other_account = await _seed_account(admin_pool, org_id, user_id, name="Other Account")
    other_transcript = await _seed_transcript(admin_pool, org_id, other_account, None, user_id)
    key = "idem-ids-json"

    async def call(
        *,
        acc: uuid.UUID = account_id,
        trans: uuid.UUID = transcript_id,
        input_refs: dict | None = None,
        source_manifest: dict | None = None,
    ) -> dict:
        async with user_pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT * FROM public.enqueue_job(
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12::jsonb, $13::jsonb, $14::jsonb
                )
                """,
                _context_token(user_id),
                acc,
                trans,
                None,
                "post-call",
                "1.0",
                "echo",
                "slice4",
                key,
                3,
                300,
                json.dumps({}),
                json.dumps(input_refs or {"transcript_id": str(trans)}),
                json.dumps(source_manifest or {"transcript_id": str(trans)}),
            )

    row = await call()
    job_id = row["job_id"]

    with pytest.raises(asyncpg.exceptions.PostgresError):
        await call(acc=other_account)
    with pytest.raises(asyncpg.exceptions.PostgresError):
        await call(trans=other_transcript)
    with pytest.raises(asyncpg.exceptions.PostgresError):
        await call(input_refs={"transcript_id": str(transcript_id), "extra": True})
    with pytest.raises(asyncpg.exceptions.PostgresError):
        await call(source_manifest={"transcript_id": str(transcript_id), "extra": True})

    async with admin_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM public.jobs WHERE org_id = $1 AND idempotency_key = $2",
            org_id,
            key,
        )
    assert count == 1


# Helper functions


def _enqueue_via_api(
    app_client: TestClient,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    *,
    opportunity_id: uuid.UUID | None = None,
    max_attempts: int = 3,
    idempotency_key: str | None = None,
    payload: dict | None = None,
) -> str:
    body: dict = {
        "account_id": str(account_id),
        "transcript_id": str(transcript_id),
        "max_attempts": max_attempts,
    }
    if opportunity_id:
        body["opportunity_id"] = str(opportunity_id)
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    if payload:
        body["payload"] = payload

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json=body,
        headers=_auth_header(user_id, f"{user_id}@airbyte.io"),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _get_job(app_client: TestClient, user_id: uuid.UUID, job_id: str) -> dict:
    response = app_client.get(
        f"/api/hosted/jobs/{job_id}",
        headers=_auth_header(user_id, f"{user_id}@airbyte.io"),
    )
    assert response.status_code == 200
    return response.json()


def _enqueue_and_get(
    app_client: TestClient,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    *,
    payload: dict | None = None,
) -> dict:
    job_id = _enqueue_via_api(
        app_client,
        user_id,
        account_id,
        transcript_id,
        payload=payload,
    )
    return _get_job(app_client, user_id, job_id)


async def _claim(pool: asyncpg.Pool, worker_name: str, timeout_seconds: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)",
            worker_name,
            timeout_seconds,
        )
        if row is None:
            return None
        return {
            "job_id": row["job_id"],
            "attempt_number": row["attempt_number"],
            "lease_token": row["lease_token"],
            "org_id": row["org_id"],
            "account_id": row["account_id"],
            "transcript_id": row["transcript_id"],
        }


async def _heartbeat(
    pool: asyncpg.Pool,
    claim: dict,
    timeout_seconds: int,
) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT public.worker_heartbeat($1, $2, $3, $4)",
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            timeout_seconds,
        )


async def _complete_job(
    pool: asyncpg.Pool,
    claim: dict,
    *,
    output_id: uuid.UUID | None = None,
    validation_status: str = "unvalidated",
    token_usage: dict | None = None,
    cost: float = 0.0,
    runtime_version: str | None = "slice4-echo",
    model: str | None = "echo",
) -> None:
    async with pool.acquire() as conn:
        await conn.fetchval(
            """
            SELECT public.complete_job($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
            """,
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            output_id,
            validation_status,
            json.dumps(token_usage or {}),
            cost,
            runtime_version,
            model,
        )


async def _fail_job(
    pool: asyncpg.Pool,
    claim: dict,
    error_category: str,
    error: str,
    backoff_seconds: int | None = None,
    runtime_version: str | None = "slice4-echo",
    model: str | None = "echo",
    validation_status: str = "unvalidated",
) -> None:
    async with pool.acquire() as conn:
        await conn.fetchval(
            "SELECT public.fail_job($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            error_category,
            error,
            backoff_seconds,
            runtime_version,
            model,
            validation_status,
        )


async def _enqueue_job_direct(
    pool: asyncpg.Pool,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    opportunity_id: uuid.UUID | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            SELECT public.enqueue_job(
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12::jsonb, $13::jsonb, $14::jsonb
            )
            """,
            _context_token(user_id),
            account_id,
            transcript_id,
            opportunity_id,
            "post-call",
            "1.0",
            "echo",
            "slice4",
            None,
            3,
            300,
            json.dumps({}),
            json.dumps({"transcript_id": str(transcript_id)}),
            json.dumps({}),
        )
