"""Slice 6B2A1 tests: transcript deletion tombstone + Storage reconciliation.

These run against a real Postgres container with the versioned migrations
applied, so the boundary exercised is the production one:

    authenticated FastAPI -> tenant-scoped `app_user` connection
    -> `public.request_transcript_deletion` (tombstone + audit, one transaction)
    -> worker claim -> exact-target Storage credential -> `cleanup_state`

All data is synthetic. The transcript row survives deletion as the provenance
anchor terminal jobs, outputs, and audit events reference; only the private
object is removed, and only after the tombstone is committed.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import shutil
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from .hosted_helpers import (
    _auth_header,
    _context_token,
    _seed_account,
    _seed_member,
    _seed_opportunity,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.hosted, pytest.mark.slow]

TERMINAL_STATUSES = ("success", "failure", "timeout", "cancelled")


def _hs() -> Any:
    """Return the live `hosted.storage` module.

    The hosted fixtures drop `hosted*` from `sys.modules` before the app is
    built, so a module-level import here would capture stale classes and the
    `StorageError` raised by a test double would not be the class the
    orchestrator catches.
    """
    import hosted.storage

    return hosted.storage


def _orchestrator_module() -> Any:
    """Return the live `hosted.post_call_orchestrator` module."""
    import hosted.post_call_orchestrator

    return hosted.post_call_orchestrator


class _IdleRuntime:
    """Runtime that must never be invoked by a cleanup-only orchestrator."""

    async def execute(self, job: Any, cancellation: Any) -> Any:
        raise AssertionError("runtime must not be invoked for transcript cleanup")


def _worker_backend(
    admin_pool: asyncpg.Pool, app_backend: Any, *, fail_times: int = 0
) -> Any:
    """Return a worker-side memory backend over the app's object store.

    It shares `objects` with the backend the app uploaded through, so a
    maintenance delete performed by the worker is observable from the request
    side. `fail_times` injects transient Storage failures.
    """
    hs = _hs()

    class WorkerBackend(hs.MemoryStorageBackend):  # type: ignore[misc, name-defined]
        def __init__(self) -> None:
            super().__init__(admin_pool=admin_pool)
            self.objects = app_backend.objects
            self.maintenance_calls: list[tuple[uuid.UUID, str, str]] = []
            self.failures_left = fail_times

        async def delete_for_maintenance(
            self, org_id: uuid.UUID, path: str, bucket: str = hs.OUTPUTS_BUCKET
        ) -> None:
            self.maintenance_calls.append((org_id, path, bucket))
            if self.failures_left > 0:
                self.failures_left -= 1
                raise hs.StorageError("injected maintenance delete failure")
            return await super().delete_for_maintenance(org_id, path, bucket=bucket)

    return WorkerBackend()


@pytest.fixture
def backend(app_client: TestClient) -> Any:
    """Return the in-memory Storage backend installed by `app_client`."""
    return _hs().get_backend()


@pytest.fixture(autouse=True)
async def _clean_tables(admin_pool: asyncpg.Pool) -> None:
    """Reset transcript, job, output, and audit state between tests.

    Transcripts are included so a tombstone left pending by one test cannot be
    the row a later worker-claim test picks up. TRUNCATE does not fire the
    physical-delete guard, which is a row trigger.
    """
    async with admin_pool.acquire() as conn:
        await conn.execute("TRUNCATE public.audit_events")
        await conn.execute(
            "TRUNCATE public.reviews, public.output_versions, public.outputs, "
            "public.job_attempts, public.jobs, public.transcripts CASCADE"
        )


async def _upload(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    *,
    email: str | None = None,
    with_opportunity: bool = False,
) -> dict[str, Any]:
    """Seed an org/account and upload one transcript through the hosted API."""
    email = email or f"tomb-{uuid.uuid4().hex[:8]}@airbyte.io"
    user_id, org_id, _ = await _seed_member(admin_pool, email)
    account_id = await _seed_account(admin_pool, org_id, user_id)
    opportunity_id = (
        await _seed_opportunity(admin_pool, org_id, account_id, user_id)
        if with_opportunity
        else None
    )
    headers = _auth_header(user_id, email)
    prefix = f"/api/hosted/accounts/{account_id}"
    if opportunity_id is not None:
        prefix = f"{prefix}/opportunities/{opportunity_id}"
    response = app_client.post(
        f"{prefix}/transcripts",
        files={"file": ("call.txt", b"Discovery call with Acme.", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201
    transcript_id = uuid.UUID(response.json()["id"])
    async with admin_pool.acquire() as conn:
        storage_path = await conn.fetchval(
            "SELECT storage_path FROM public.transcripts WHERE id = $1", transcript_id
        )
    return {
        "user_id": user_id,
        "email": email,
        "org_id": org_id,
        "account_id": account_id,
        "opportunity_id": opportunity_id,
        "transcript_id": transcript_id,
        "headers": headers,
        "prefix": prefix,
        "storage_path": storage_path,
    }


def _delete(app_client: TestClient, fx: dict[str, Any]) -> Any:
    return app_client.delete(
        f"{fx['prefix']}/transcripts/{fx['transcript_id']}", headers=fx["headers"]
    )


async def _row(admin_pool: asyncpg.Pool, transcript_id: uuid.UUID) -> asyncpg.Record:
    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM public.transcripts WHERE id = $1", transcript_id
        )
    assert row is not None, "the transcript row must survive as provenance"
    return row


async def _delete_events(
    admin_pool: asyncpg.Pool, org_id: uuid.UUID
) -> list[asyncpg.Record]:
    async with admin_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM public.audit_events "
            "WHERE org_id = $1 AND action = 'transcript_delete' ORDER BY created_at",
            org_id,
        )


async def _seed_job(admin_pool: asyncpg.Pool, fx: dict[str, Any], status: str) -> uuid.UUID:
    async with admin_pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO public.jobs (
                org_id, account_id, opportunity_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, $5, 'post-call', '1.0', $6, 3)
            RETURNING id
            """,
            fx["org_id"],
            fx["account_id"],
            fx["opportunity_id"],
            fx["transcript_id"],
            fx["user_id"],
            status,
        )


def _orchestrator(worker_pool: asyncpg.Pool, backend: Any) -> Any:
    return _orchestrator_module().PostCallOrchestrator(
        _IdleRuntime(), db_pool=worker_pool, storage_backend=backend
    )


def _object_exists(backend: Any, storage_path: str) -> bool:
    return f"{_hs().DEFAULT_BUCKET}:{storage_path}" in backend.objects


# ---------------------------------------------------------------------------
# Logical deletion: authorize, hide, audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_opportunity", [False, True], ids=["account", "opportunity"])
async def test_delete_hides_the_transcript_everywhere_and_keeps_the_object(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    user_pool: asyncpg.Pool,
    backend: Any,
    with_opportunity: bool,
) -> None:
    """A committed tombstone hides the transcript from every read path at once,
    while the private object is still present: cleanup is asynchronous, and the
    `202` response says exactly that.
    """
    fx = await _upload(app_client, admin_pool, with_opportunity=with_opportunity)
    assert _object_exists(backend, fx["storage_path"])

    response = _delete(app_client, fx)
    assert response.status_code == 202
    assert response.json() == {
        "transcript_id": str(fx["transcript_id"]),
        "status": "accepted",
        "cleanup_state": "pending",
    }

    # Every user-facing read path fails closed immediately.
    assert (
        app_client.get(f"{fx['prefix']}/transcripts", headers=fx["headers"]).json()[
            "transcripts"
        ]
        == []
    )
    assert (
        app_client.get(
            f"/api/hosted/accounts/{fx['account_id']}/transcripts", headers=fx["headers"]
        ).json()["transcripts"]
        == []
    )
    assert (
        app_client.get(
            f"{fx['prefix']}/transcripts/{fx['transcript_id']}/download",
            headers=fx["headers"],
        ).status_code
        == 404
    )
    assert _delete(app_client, fx).json()["status"] == "already_deleted"

    # RLS hides the row from a direct `app_user` SELECT, so no route has to
    # remember to filter, while the row itself survives for provenance.
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(fx["user_id"]),
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM public.transcripts WHERE id = $1",
                    fx["transcript_id"],
                )
                == 0
            )

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["tombstoned_at"] is not None
    assert row["delete_requested_by"] == fx["user_id"]
    assert row["cleanup_state"] == "pending"
    assert row["cleanup_completed_at"] is None
    assert row["storage_path"] == fx["storage_path"]

    # The bytes are still there: `transcript_delete` records an accepted request,
    # not a completed purge.
    assert _object_exists(backend, fx["storage_path"])
    events = await _delete_events(admin_pool, fx["org_id"])
    assert len(events) == 1
    assert events[0]["user_id"] == fx["user_id"]
    assert events[0]["entity_id"] == fx["transcript_id"]


async def test_cross_org_and_missing_deletions_are_indistinguishable(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """A neighbour's transcript id and a random id fail the same way and leave
    no tombstone and no audit event behind.
    """
    victim = await _upload(app_client, admin_pool, email="victim@airbyte.io")
    attacker = await _upload(app_client, admin_pool, email="attacker@airbyte.io")

    for target in (victim["transcript_id"], uuid.uuid4()):
        response = app_client.delete(
            f"/api/hosted/accounts/{attacker['account_id']}/transcripts/{target}",
            headers=attacker["headers"],
        )
        assert response.status_code == 404

    # The victim's own account id with the attacker's credentials fails too, and
    # in the same way.
    assert (
        app_client.delete(
            f"/api/hosted/accounts/{victim['account_id']}/transcripts/{victim['transcript_id']}",
            headers=attacker["headers"],
        ).status_code
        == 404
    )

    row = await _row(admin_pool, victim["transcript_id"])
    assert row["tombstoned_at"] is None
    assert row["cleanup_state"] == "none"
    assert await _delete_events(admin_pool, victim["org_id"]) == []
    assert await _delete_events(admin_pool, attacker["org_id"]) == []


async def test_wrong_opportunity_scope_cannot_delete_a_transcript(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """The opportunity relationship is validated against the trusted row, so an
    account-scoped path cannot delete an opportunity transcript or vice versa.
    """
    opp_fx = await _upload(app_client, admin_pool, with_opportunity=True)
    account_fx = await _upload(app_client, admin_pool)

    assert (
        app_client.delete(
            f"/api/hosted/accounts/{opp_fx['account_id']}/transcripts/{opp_fx['transcript_id']}",
            headers=opp_fx["headers"],
        ).status_code
        == 404
    )
    other_opportunity = await _seed_opportunity(
        admin_pool, account_fx["org_id"], account_fx["account_id"], account_fx["user_id"]
    )
    assert (
        app_client.delete(
            f"/api/hosted/accounts/{account_fx['account_id']}/opportunities/"
            f"{other_opportunity}/transcripts/{account_fx['transcript_id']}",
            headers=account_fx["headers"],
        ).status_code
        == 404
    )
    assert (await _row(admin_pool, opp_fx["transcript_id"]))["tombstoned_at"] is None
    assert (await _row(admin_pool, account_fx["transcript_id"]))["tombstoned_at"] is None


async def test_inactive_member_cannot_request_deletion(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """Deactivation revokes the deletion capability like every other action."""
    fx = await _upload(app_client, admin_pool)
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.memberships SET active = false WHERE user_id = $1",
            fx["user_id"],
        )

    assert _delete(app_client, fx).status_code == 403
    assert (await _row(admin_pool, fx["transcript_id"]))["tombstoned_at"] is None
    assert await _delete_events(admin_pool, fx["org_id"]) == []


# ---------------------------------------------------------------------------
# Concurrency with jobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_status", ["queued", "running"])
async def test_active_jobs_block_deletion_without_cancelling_them(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any, job_status: str
) -> None:
    """Deleting the input of live work is a conflict, not an implicit cancel."""
    fx = await _upload(app_client, admin_pool)
    job_id = await _seed_job(admin_pool, fx, job_status)

    assert _delete(app_client, fx).status_code == 409

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["tombstoned_at"] is None
    assert row["cleanup_state"] == "none"
    assert _object_exists(backend, fx["storage_path"])
    assert await _delete_events(admin_pool, fx["org_id"]) == []
    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM public.jobs WHERE id = $1", job_id)
            == job_status
        )

    # The transcript is still fully usable while the conflict stands.
    assert (
        len(
            app_client.get(f"{fx['prefix']}/transcripts", headers=fx["headers"]).json()[
                "transcripts"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("job_status", TERMINAL_STATUSES)
async def test_terminal_jobs_do_not_block_deletion_and_keep_their_reference(
    app_client: TestClient, admin_pool: asyncpg.Pool, job_status: str
) -> None:
    """Terminal provenance survives: the job keeps pointing at the tombstone."""
    fx = await _upload(app_client, admin_pool)
    job_id = await _seed_job(admin_pool, fx, job_status)

    assert _delete(app_client, fx).status_code == 202

    async with admin_pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM public.jobs WHERE id = $1", job_id)
    assert job["status"] == job_status
    assert job["transcript_id"] == fx["transcript_id"]


async def test_cross_org_active_job_does_not_block_deletion(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """The active-job check is organization-scoped, so another tenant's queued
    job can neither deny a deletion nor reveal that it exists.
    """
    fx = await _upload(app_client, admin_pool)
    other = await _upload(app_client, admin_pool)
    await _seed_job(admin_pool, other, "queued")

    assert _delete(app_client, fx).status_code == 202


async def test_enqueue_after_tombstone_is_rejected_by_the_database(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool
) -> None:
    """The API hides the transcript (404), and `enqueue_job` independently
    refuses it (SE021) so no caller of the function can bypass the tombstone.
    """
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202

    response = app_client.post(
        f"/api/hosted/accounts/{fx['account_id']}/jobs",
        json={
            "account_id": str(fx["account_id"]),
            "transcript_id": str(fx["transcript_id"]),
            "skill": "post-call",
        },
        headers=fx["headers"],
    )
    assert response.status_code == 404

    with pytest.raises(asyncpg.PostgresError) as exc_info:
        async with user_pool.acquire() as conn:
            await conn.fetch(
                """
                SELECT * FROM public.enqueue_job(
                    $1, $2, $3, NULL, 'post-call', '1.0', 'claude-test', '1.0',
                    NULL, 3, 900, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
                )
                """,
                _context_token(fx["user_id"]),
                fx["account_id"],
                fx["transcript_id"],
            )
    assert exc_info.value.sqlstate == "SE021"

    async with admin_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM public.jobs") == 0


async def test_concurrent_delete_and_enqueue_have_exactly_one_winner(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """Either the run is queued and deletion loses with a conflict, or the
    tombstone lands first and the run is refused. Never both, never neither.
    """
    fx = await _upload(app_client, admin_pool)

    def _enqueue() -> Any:
        return app_client.post(
            f"/api/hosted/accounts/{fx['account_id']}/jobs",
            json={
                "account_id": str(fx["account_id"]),
                "transcript_id": str(fx["transcript_id"]),
                "skill": "post-call",
            },
            headers=fx["headers"],
        )

    delete_response, enqueue_response = await asyncio.gather(
        asyncio.to_thread(lambda: _delete(app_client, fx)),
        asyncio.to_thread(_enqueue),
    )

    row = await _row(admin_pool, fx["transcript_id"])
    async with admin_pool.acquire() as conn:
        jobs = await conn.fetch("SELECT * FROM public.jobs")

    if enqueue_response.status_code == 201:
        # The run won: the transcript is intact and the deletion was refused.
        assert delete_response.status_code == 409
        assert row["tombstoned_at"] is None
        assert len(jobs) == 1
        assert _object_exists(backend, fx["storage_path"])
        assert await _delete_events(admin_pool, fx["org_id"]) == []
    else:
        assert enqueue_response.status_code in (404, 409)
        assert delete_response.status_code == 202
        assert row["tombstoned_at"] is not None
        assert jobs == []
        assert len(await _delete_events(admin_pool, fx["org_id"])) == 1


async def test_concurrent_duplicate_deletions_produce_one_tombstone(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """Two simultaneous requests serialize on the row lock: one accepts, the
    other replays, and there is exactly one deletion event.
    """
    fx = await _upload(app_client, admin_pool)

    first, second = await asyncio.gather(
        asyncio.to_thread(lambda: _delete(app_client, fx)),
        asyncio.to_thread(lambda: _delete(app_client, fx)),
    )
    assert {first.status_code, second.status_code} == {202}
    assert sorted([first.json()["status"], second.json()["status"]]) == [
        "accepted",
        "already_deleted",
    ]
    assert len(await _delete_events(admin_pool, fx["org_id"])) == 1

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "pending"
    assert row["cleanup_attempts"] == 0


# ---------------------------------------------------------------------------
# Worker reconciliation
# ---------------------------------------------------------------------------


async def test_worker_deletes_the_exact_object_and_marks_cleanup_complete(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """The claim hands the worker only trusted identifiers, and the object is
    removed with an exact `{org, bucket, path}` maintenance credential.
    """
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202
    assert _object_exists(backend, fx["storage_path"])

    worker_backend = _worker_backend(admin_pool, backend)
    orchestrator = _orchestrator(worker_pool, worker_backend)
    assert await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-1") is True
    assert worker_backend.maintenance_calls == [
        (fx["org_id"], fx["storage_path"], _hs().DEFAULT_BUCKET)
    ]
    assert not _object_exists(backend, fx["storage_path"])

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "complete"
    assert row["cleanup_completed_at"] is not None
    assert row["cleanup_claimed_by"] is None
    assert row["cleanup_last_error"] is None
    assert row["storage_path"] == fx["storage_path"]

    # Reconciliation is not a user action: it records no second audit event, and
    # there is nothing left to claim.
    assert len(await _delete_events(admin_pool, fx["org_id"])) == 1
    assert await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-1") is False


async def test_missing_object_counts_as_reconciled(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """An already-absent object is the desired end state, not an error."""
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202
    del backend.objects[f"{_hs().DEFAULT_BUCKET}:{fx['storage_path']}"]

    orchestrator = _orchestrator(worker_pool, backend)
    assert await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-2") is True
    assert (await _row(admin_pool, fx["transcript_id"]))["cleanup_state"] == "complete"


async def test_storage_failure_keeps_the_tombstone_retryable(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """An ambiguous Storage delete never marks cleanup complete: the claim is
    released with a bounded error, the transcript stays hidden, and a later
    attempt from a restarted worker finishes the job.
    """
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202
    failing = _worker_backend(admin_pool, backend, fail_times=1)

    orchestrator = _orchestrator(worker_pool, failing)
    with pytest.raises(_orchestrator_module().CleanupError):
        await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-fail")

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "pending"
    assert row["cleanup_completed_at"] is None
    assert row["cleanup_claimed_by"] is None
    assert row["cleanup_attempts"] == 1
    assert row["cleanup_last_error"] == "StorageError"
    assert _object_exists(failing, fx["storage_path"])
    assert (
        app_client.get(f"{fx['prefix']}/transcripts", headers=fx["headers"]).json()[
            "transcripts"
        ]
        == []
    )

    # A fresh process picks the row back up because the claim was released.
    restarted = _orchestrator(worker_pool, failing)
    assert (
        await restarted.cleanup_next_transcript_tombstone("cleanup-worker-restarted") is True
    )
    assert not _object_exists(failing, fx["storage_path"])

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "complete"
    assert row["cleanup_attempts"] == 2
    assert row["cleanup_last_error"] is None
    assert len(await _delete_events(admin_pool, fx["org_id"])) == 1


async def test_repeated_failures_stop_being_claimed_after_max_attempts(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """Attempts are bounded so one poisoned row cannot spin the worker forever;
    the row stays `pending` with its last error for operator follow-up.
    """
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202
    failing = _worker_backend(admin_pool, backend, fail_times=10)

    orchestrator = _orchestrator(worker_pool, failing)
    for _ in range(3):
        with pytest.raises(_orchestrator_module().CleanupError):
            await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-bounded")

    async with worker_pool.acquire() as conn:
        claimed = await conn.fetch(
            "SELECT * FROM public.claim_next_transcript_cleanup($1, $2, $3)",
            "cleanup-worker-bounded",
            60,
            3,
        )
    assert claimed == []

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "pending"
    assert row["cleanup_attempts"] == 3
    assert row["cleanup_last_error"] == "StorageError"
    assert _object_exists(failing, fx["storage_path"])


async def test_live_claim_is_not_stolen_and_expired_claim_is_reclaimed(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool
) -> None:
    """Two workers cannot reconcile the same transcript concurrently, and a
    worker that died mid-cleanup does not strand the row.
    """
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202

    async with worker_pool.acquire() as conn:
        claimed = await conn.fetchrow(
            "SELECT * FROM public.claim_next_transcript_cleanup($1, $2)", "worker-a", 60
        )
        assert claimed["transcript_id"] == fx["transcript_id"]
        assert claimed["org_id"] == fx["org_id"]
        assert claimed["storage_path"] == fx["storage_path"]

        # A live lease is invisible to a second worker.
        assert (
            await conn.fetchrow(
                "SELECT * FROM public.claim_next_transcript_cleanup($1, $2)", "worker-b", 60
            )
            is None
        )

        # Only the claim holder may finalize or release.
        assert (
            await conn.fetchval(
                "SELECT public.finalize_transcript_cleanup($1, $2)",
                fx["transcript_id"],
                "worker-b",
            )
            is False
        )
        assert (
            await conn.fetchval(
                "SELECT public.release_transcript_cleanup($1, $2, $3)",
                fx["transcript_id"],
                "worker-b",
                "not mine",
            )
            is False
        )
        assert (await _row(admin_pool, fx["transcript_id"]))["cleanup_state"] == "pending"

        # Once the lease expires the row is claimable again, and the recovered
        # claim carries the same trusted target.
        await conn.execute("SELECT pg_sleep(1.1)")
        reclaimed = await conn.fetchrow(
            "SELECT * FROM public.claim_next_transcript_cleanup($1, $2)", "worker-b", 1
        )
    assert reclaimed is not None
    assert reclaimed["transcript_id"] == fx["transcript_id"]
    assert reclaimed["storage_path"] == fx["storage_path"]

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_claimed_by"] == "worker-b"
    assert row["cleanup_attempts"] == 2


async def test_concurrent_workers_reconcile_each_tombstone_once(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """`FOR UPDATE SKIP LOCKED` means N workers share the backlog instead of
    fighting over one row or deleting the same object twice.
    """
    fixtures = []
    for _ in range(3):
        fx = await _upload(app_client, admin_pool)
        assert _delete(app_client, fx).status_code == 202
        fixtures.append(fx)

    worker_backends = [_worker_backend(admin_pool, backend) for _ in range(3)]
    orchestrators = [_orchestrator(worker_pool, wb) for wb in worker_backends]
    results = await asyncio.gather(
        *(
            orchestrator.cleanup_next_transcript_tombstone(f"parallel-worker-{index}")
            for index, orchestrator in enumerate(orchestrators)
        )
    )
    assert results == [True, True, True]

    targets = [call[1] for wb in worker_backends for call in wb.maintenance_calls]
    assert sorted(targets) == sorted(fx["storage_path"] for fx in fixtures)
    for fx in fixtures:
        assert (await _row(admin_pool, fx["transcript_id"]))["cleanup_state"] == "complete"
        assert not _object_exists(backend, fx["storage_path"])


async def test_reconciliation_ignores_requester_deactivation(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """A deactivated requester cannot pin customer bytes in Storage: the
    maintenance credential consults no membership.
    """
    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.memberships SET active = false WHERE user_id = $1", fx["user_id"]
        )

    orchestrator = _orchestrator(worker_pool, _worker_backend(admin_pool, backend))
    assert await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-3") is True
    assert not _object_exists(backend, fx["storage_path"])

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "complete"
    assert row["delete_requested_by"] == fx["user_id"]


async def test_worker_loop_reconciles_tombstones_and_suppresses_failures(
    app_client: TestClient, admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, backend: Any
) -> None:
    """The generic worker poll cycle performs transcript cleanup, and a cleanup
    failure is suppressed so job processing is never blocked by it.
    """
    from hosted.post_call_orchestrator import PostCallExecutor
    from hosted.worker import Worker

    fx = await _upload(app_client, admin_pool)
    assert _delete(app_client, fx).status_code == 202
    failing = _worker_backend(admin_pool, backend, fail_times=1)

    executor = PostCallExecutor(_IdleRuntime(), db_pool=worker_pool, storage_backend=failing)
    worker = Worker(worker_pool, executor=executor, worker_name="tombstone-loop-worker")

    assert await worker.cleanup_next_transcript_tombstone() is False
    assert (await _row(admin_pool, fx["transcript_id"]))["cleanup_state"] == "pending"
    assert await worker.run_once() is False

    row = await _row(admin_pool, fx["transcript_id"])
    assert row["cleanup_state"] == "complete"
    assert not _object_exists(failing, fx["storage_path"])


async def test_worker_role_reaches_transcripts_only_through_the_functions(
    worker_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """`app_worker` keeps no direct privilege on the customer-data tables."""
    async with admin_pool.acquire() as conn:
        for table in ("transcripts", "memberships", "audit_events"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await conn.fetchval(
                    "SELECT has_table_privilege('app_worker', $1, $2)",
                    f"public.{table}",
                    privilege,
                ), (table, privilege)

    # The narrow functions are the only path, and they are executable.
    async with worker_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT has_function_privilege('app_worker', "
            "'public.claim_next_transcript_cleanup(text,integer,integer)', 'EXECUTE')"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetchval("SELECT count(*) FROM public.transcripts")


# ---------------------------------------------------------------------------
# Physical deletion guard
# ---------------------------------------------------------------------------


async def test_physical_delete_requires_a_reconciled_tombstone(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    backend: Any,
) -> None:
    """No privileged path can hard-delete a transcript that was never tombstoned
    or whose object has not been reconciled, so a future retention slice cannot
    silently skip the deletion audit event and the Storage cleanup.
    """
    fx = await _upload(app_client, admin_pool)

    async with admin_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await conn.execute(
                "DELETE FROM public.transcripts WHERE id = $1", fx["transcript_id"]
            )
    assert "reconciled" in str(exc_info.value)
    await _row(admin_pool, fx["transcript_id"])

    assert _delete(app_client, fx).status_code == 202
    async with admin_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "DELETE FROM public.transcripts WHERE id = $1", fx["transcript_id"]
            )
    await _row(admin_pool, fx["transcript_id"])

    orchestrator = _orchestrator(worker_pool, _worker_backend(admin_pool, backend))
    assert await orchestrator.cleanup_next_transcript_tombstone("cleanup-worker-4") is True

    # Only a reconciled tombstone may be physically removed, and doing so
    # records no user action.
    async with admin_pool.acquire() as conn:
        await conn.execute("DELETE FROM public.transcripts WHERE id = $1", fx["transcript_id"])
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.transcripts WHERE id = $1", fx["transcript_id"]
            )
            == 0
        )
    assert len(await _delete_events(admin_pool, fx["org_id"])) == 1


@pytest.mark.parametrize(
    "tombstoned_at,cleanup_state,cleanup_completed_at",
    [
        pytest.param("NULL", "pending", "NULL", id="pending_without_tombstone"),
        pytest.param("now()", "none", "NULL", id="tombstone_without_cleanup"),
        pytest.param("NULL", "none", "now()", id="completed_without_tombstone"),
    ],
)
async def test_tombstone_and_cleanup_state_cannot_drift(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    tombstoned_at: str,
    cleanup_state: str,
    cleanup_completed_at: str,
) -> None:
    """Hidden and being-reconciled are one invariant, enforced by a CHECK."""
    fx = await _upload(app_client, admin_pool)
    async with admin_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                f"""
                UPDATE public.transcripts
                SET tombstoned_at = {tombstoned_at},
                    cleanup_state = $1,
                    cleanup_completed_at = {cleanup_completed_at}
                WHERE id = $2
                """,
                cleanup_state,
                fx["transcript_id"],
            )


# ---------------------------------------------------------------------------
# Migration compatibility
# ---------------------------------------------------------------------------


async def test_clean_install_has_the_tombstone_boundary(admin_pool: asyncpg.Pool) -> None:
    """The container database (a clean install through 013) has the state,
    privileges, policies, and function ownership the boundary depends on.
    """
    async with admin_pool.acquire() as conn:
        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'transcripts'"
            )
        }
        assert {
            "tombstoned_at",
            "delete_requested_by",
            "cleanup_state",
            "cleanup_completed_at",
            "cleanup_attempts",
            "cleanup_claimed_by",
            "cleanup_claimed_at",
            "cleanup_last_error",
        } <= columns

        # `app_user` may read and insert, but no longer delete or update.
        privileges = {
            privilege: await conn.fetchval(
                "SELECT has_table_privilege('app_user', 'public.transcripts', $1)", privilege
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        assert privileges == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        }

        policies = {
            row["policyname"]: row["cmd"]
            for row in await conn.fetch(
                "SELECT policyname, cmd FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = 'transcripts'"
            )
        }
        assert policies == {
            "org_tenant_transcripts_select": "SELECT",
            "org_tenant_transcripts_insert": "INSERT",
        }

        # The deletion and cleanup functions are app_admin-owned SECURITY DEFINER
        # with a pinned search_path, and only the intended role may execute each.
        for name, allowed_role in (
            ("request_transcript_deletion", "app_user"),
            ("claim_next_transcript_cleanup", "app_worker"),
            ("finalize_transcript_cleanup", "app_worker"),
            ("release_transcript_cleanup", "app_worker"),
        ):
            row = await conn.fetchrow(
                "SELECT p.oid, pg_get_userbyid(p.proowner) AS owner, p.prosecdef, p.proconfig "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = $1",
                name,
            )
            assert row["owner"] == "app_admin", name
            assert row["prosecdef"] is True, name
            assert "search_path=" in "".join(row["proconfig"] or []), name
            assert await conn.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", allowed_role, row["oid"]
            ), (allowed_role, name)
            denied = "app_worker" if allowed_role == "app_user" else "app_user"
            assert not await conn.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", denied, row["oid"]
            ), (denied, name)

        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'audit_transcript_delete'"
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'transcripts_guard_physical_delete'"
            )
            == 1
        )


async def test_upgrade_from_012_preserves_live_transcripts_and_evidence(
    hosted_env: dict[str, str], tmp_path: Path
) -> None:
    """Upgrading a populated 012 database keeps historical rows, leaves existing
    transcripts live, and tombstones one of them through the new function.
    """
    import hosted
    import hosted.migrations as migrations

    host_port = hosted_env["MIGRATE_DATABASE_URL"].rsplit("/", 1)[0]
    test_db = f"upgrade_013_{uuid.uuid4().hex[:8]}"
    partial_dir = tmp_path / "migrations_012"
    partial_dir.mkdir()
    for version, path in migrations.list_migrations(hosted.config.MIGRATIONS_DIR):
        if version <= "012":
            shutil.copyfile(path, partial_dir / path.name)

    admin_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
    try:
        await admin_conn.execute(f'CREATE DATABASE "{test_db}"')
    finally:
        await admin_conn.close()

    test_dsn = f"{host_port}/{test_db}"
    passwords = {
        "app_user_password": "app_user_password",
        "app_admin_password": "app_admin_password",
        "app_worker_password": "app_worker_password",
        "context_secret": hosted_env["HOSTED_CONTEXT_SECRET"],
    }
    conn = None
    try:
        await migrations.migrate(test_dsn, partial_dir, **passwords)

        conn = await asyncpg.connect(test_dsn)
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        live_id = uuid.uuid4()
        historical_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.users (id, email) VALUES ($1, $2)",
            user_id,
            "upgrade-013@airbyte.io",
        )
        await conn.execute(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
            org_id,
            "Upgrade Org",
            f"upgrade-013-{uuid.uuid4().hex[:8]}",
        )
        await conn.execute(
            "INSERT INTO public.memberships (id, org_id, user_id, role, active) "
            "VALUES ($1, $2, $3, 'member', true)",
            uuid.uuid4(),
            org_id,
            user_id,
        )
        await conn.execute(
            "INSERT INTO public.accounts (id, org_id, name, slug, created_by) "
            "VALUES ($1, $2, $3, $4, $5)",
            account_id,
            org_id,
            "Upgrade Account",
            f"upgrade-013-account-{uuid.uuid4().hex[:8]}",
            user_id,
        )
        for transcript_id in (live_id, historical_id):
            await conn.execute(
                """
                INSERT INTO public.transcripts
                    (id, org_id, account_id, storage_path, original_filename,
                     size_bytes, mime_type, uploaded_by)
                VALUES ($1, $2, $3, $4, 'legacy.txt', 4, 'text/plain', $5)
                """,
                transcript_id,
                org_id,
                account_id,
                f"{org_id}/{account_id}/{transcript_id}-legacy.txt",
                user_id,
            )
        job_id = await conn.fetchval(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id, skill,
                skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, 'post-call', '1.0', 'success', 3)
            RETURNING id
            """,
            org_id,
            account_id,
            historical_id,
            user_id,
        )
        # Historical audit evidence, including a pre-upgrade delete event that
        # meant "row and object removed".
        for action, entity_type in (
            ("transcript_upload", "transcripts"),
            ("transcript_delete", "transcripts"),
            ("job_run_requested", "jobs"),
            ("output_export", "outputs"),
        ):
            await conn.execute(
                "INSERT INTO public.audit_events (org_id, user_id, action, entity_type) "
                "VALUES ($1, $2, $3, $4)",
                org_id,
                user_id,
                action,
                entity_type,
            )
        await conn.close()
        conn = None

        await migrations.migrate(test_dsn, hosted.config.MIGRATIONS_DIR, **passwords)

        conn = await asyncpg.connect(test_dsn)
        assert await conn.fetchval("SELECT count(*) FROM public.audit_events") == 4
        assert await conn.fetchval("SELECT count(*) FROM public.transcripts") == 2
        assert (
            await conn.fetchval("SELECT count(*) FROM public.jobs WHERE id = $1", job_id) == 1
        )
        # Existing rows come out of the upgrade live, not tombstoned.
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.transcripts "
                "WHERE tombstoned_at IS NULL AND cleanup_state = 'none'"
            )
            == 2
        )

        # The new deletion path works on a pre-existing row.
        secret = hosted_env["HOSTED_CONTEXT_SECRET"].encode("utf-8")
        digest = hmac.new(secret, str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()
        result = await conn.fetchval(
            "SELECT public.request_transcript_deletion($1, $2, NULL, $3)",
            f"{user_id}:{digest}",
            account_id,
            live_id,
        )
        assert '"accepted"' in result
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.audit_events WHERE action = 'transcript_delete'"
            )
            == 2
        )
        row = await conn.fetchrow(
            "SELECT tombstoned_at, cleanup_state FROM public.transcripts WHERE id = $1",
            live_id,
        )
        assert row["tombstoned_at"] is not None
        assert row["cleanup_state"] == "pending"

        # The historical terminal job still resolves its transcript reference.
        assert (
            await conn.fetchval("SELECT transcript_id FROM public.jobs WHERE id = $1", job_id)
            == historical_id
        )
    finally:
        if conn is not None:
            await conn.close()
        drop_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
        try:
            await drop_conn.execute(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)')
        finally:
            await drop_conn.close()
