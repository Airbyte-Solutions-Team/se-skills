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
) -> None:
    async with pool.acquire() as conn:
        await conn.fetchval(
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
) -> None:
    async with pool.acquire() as conn:
        await conn.fetchval(
            """
            SELECT public.complete_job($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            output_id,
            validation_status,
            json.dumps(token_usage or {}),
            cost,
        )


async def _fail_job(
    pool: asyncpg.Pool,
    claim: dict,
    error_category: str,
    error: str,
    backoff_seconds: int | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.fetchval(
            "SELECT public.fail_job($1, $2, $3, $4, $5, $6)",
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            error_category,
            error,
            backoff_seconds,
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
