"""Audit completeness for hosted user actions (Slice 6B2A).

Covers the four actions added by migration 012 — `transcript_upload`,
`transcript_delete`, `job_run_requested`, `job_cancel_requested` — including the
cases where no evidence may exist: validation failures, Storage failures,
unauthorized or cross-organization requests, idempotent replays, lost
cancellation races, and worker lifecycle activity.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
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
    _seed_user_and_membership,
)
from webapp.hosted.worker import Worker


pytestmark = [pytest.mark.asyncio, pytest.mark.hosted]

_TRANSCRIPT_ACTIONS = ("transcript_upload", "transcript_delete")
_JOB_ACTIONS = ("job_run_requested", "job_cancel_requested")

# Strings that must never appear anywhere in audit metadata.
_LEAK_MARKERS = (
    "Acme Confidential Corp",
    "quarterly-forecast-call.txt",
    "transcripts/",
    "SECRET-TRANSCRIPT-BODY",
    "idem-key-",
)


@pytest.fixture(autouse=True)
async def _clean_audit_state(admin_pool: asyncpg.Pool) -> None:
    """Start every test with an empty audit log and job ledger."""
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE public.audit_events, public.job_attempts, public.jobs CASCADE"
        )


async def _events(
    admin_pool: asyncpg.Pool, org_id: uuid.UUID, action: str | None = None
) -> list[asyncpg.Record]:
    async with admin_pool.acquire() as conn:
        if action is None:
            return list(
                await conn.fetch(
                    "SELECT * FROM public.audit_events WHERE org_id = $1 ORDER BY created_at",
                    org_id,
                )
            )
        return list(
            await conn.fetch(
                "SELECT * FROM public.audit_events WHERE org_id = $1 AND action = $2 "
                "ORDER BY created_at",
                org_id,
                action,
            )
        )


async def _count_all(admin_pool: asyncpg.Pool, action: str | None = None) -> int:
    async with admin_pool.acquire() as conn:
        if action is None:
            return int(await conn.fetchval("SELECT count(*) FROM public.audit_events"))
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM public.audit_events WHERE action = $1", action
            )
        )


def _metadata(record: asyncpg.Record) -> dict[str, Any]:
    raw = record["metadata"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _assert_no_customer_data(record: asyncpg.Record) -> None:
    blob = str(dict(record))
    for marker in _LEAK_MARKERS:
        assert marker not in blob, f"audit metadata leaked {marker!r}: {blob}"


async def _seed_upload_ready_org(
    admin_pool: asyncpg.Pool, email: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, org_id, _ = await _seed_member(admin_pool, email)
    account_id = await _seed_account(
        admin_pool, org_id, user_id, name="Acme Confidential Corp"
    )
    return user_id, org_id, account_id


def _upload(
    app_client: TestClient,
    user_id: uuid.UUID,
    email: str,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None = None,
    filename: str = "quarterly-forecast-call.txt",
    content: bytes = b"SECRET-TRANSCRIPT-BODY line one\n",
) -> Any:
    base = f"/api/hosted/accounts/{account_id}"
    if opportunity_id is not None:
        base = f"{base}/opportunities/{opportunity_id}"
    return app_client.post(
        f"{base}/transcripts",
        files={"file": (filename, content, "text/plain")},
        headers=_auth_header(user_id, email),
    )


# ---------------------------------------------------------------------------
# transcript_upload
# ---------------------------------------------------------------------------


async def test_account_upload_records_exactly_one_safe_audit_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-upload@airbyte.io"
    )

    response = _upload(app_client, user_id, "audit-upload@airbyte.io", account_id)
    assert response.status_code == 201
    transcript_id = uuid.UUID(response.json()["id"])

    events = await _events(admin_pool, org_id, "transcript_upload")
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == user_id
    assert event["entity_type"] == "transcripts"
    assert event["entity_id"] == transcript_id
    assert event["request_id"] is None
    assert _metadata(event) == {
        "transcript_id": str(transcript_id),
        "account_id": str(account_id),
        "opportunity_id": None,
    }
    _assert_no_customer_data(event)


async def test_opportunity_upload_records_one_event_with_opportunity(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-upload-opp@airbyte.io"
    )
    opportunity_id = await _seed_opportunity(admin_pool, org_id, account_id, user_id)

    response = _upload(
        app_client, user_id, "audit-upload-opp@airbyte.io", account_id, opportunity_id
    )
    assert response.status_code == 201

    events = await _events(admin_pool, org_id, "transcript_upload")
    assert len(events) == 1
    assert _metadata(events[0])["opportunity_id"] == str(opportunity_id)
    _assert_no_customer_data(events[0])


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("call.exe", b"MZ binary"),
        ("call.txt", b"<html><body>hi</body></html>"),
        ("call.txt", b"\xff\xfe\x00invalid utf8"),
        ("call.txt", b"nul\x00byte"),
    ],
)
async def test_rejected_upload_records_no_event(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    filename: str,
    content: bytes,
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, f"audit-bad-{uuid.uuid4().hex[:6]}@airbyte.io"
    )

    response = _upload(
        app_client,
        user_id,
        "audit-bad@airbyte.io",
        account_id,
        filename=filename,
        content=content,
    )
    assert response.status_code == 400
    assert await _events(admin_pool, org_id) == []


async def test_oversize_upload_records_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-oversize@airbyte.io"
    )
    monkeypatch.setattr("hosted.config.TRANSCRIPT_MAX_BYTES", 10)

    response = _upload(
        app_client,
        user_id,
        "audit-oversize@airbyte.io",
        account_id,
        content=b"far too many bytes for the limit",
    )
    assert response.status_code == 400
    assert await _events(admin_pool, org_id) == []


async def test_storage_upload_failure_records_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-storage-fail@airbyte.io"
    )

    async def raise_error(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(app_client.app.state.storage_backend, "upload", raise_error)

    response = _upload(app_client, user_id, "audit-storage-fail@airbyte.io", account_id)
    assert response.status_code == 500
    assert await _events(admin_pool, org_id) == []
    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.transcripts WHERE org_id = $1", org_id
            )
            == 0
        )


async def test_metadata_failure_after_storage_success_records_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata row and its audit event roll back together, and the
    already-uploaded Storage object is compensated away.
    """
    from asyncpg import Connection

    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-db-fail@airbyte.io"
    )
    original_fetchrow = Connection.fetchrow

    async def fake_fetchrow(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(query, str) and "INSERT INTO public.transcripts" in query:
            raise RuntimeError("simulated metadata insert failure")
        return await original_fetchrow(self, query, *args, **kwargs)

    monkeypatch.setattr(Connection, "fetchrow", fake_fetchrow)

    response = _upload(app_client, user_id, "audit-db-fail@airbyte.io", account_id)
    assert response.status_code == 500
    monkeypatch.setattr(Connection, "fetchrow", original_fetchrow)

    assert await _events(admin_pool, org_id) == []
    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.transcripts WHERE org_id = $1", org_id
            )
            == 0
        )
    # Compensation removed the object that had already been written.
    assert not app_client.app.state.storage_backend.objects


async def test_upload_to_foreign_account_records_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_a, org_a, account_a = await _seed_upload_ready_org(
        admin_pool, "audit-xorg-a@airbyte.io"
    )
    user_b, org_b, _ = await _seed_member(admin_pool, "audit-xorg-b@airbyte.io")

    response = _upload(app_client, user_b, "audit-xorg-b@airbyte.io", account_a)
    assert response.status_code == 404
    assert await _events(admin_pool, org_a) == []
    assert await _events(admin_pool, org_b) == []


async def test_inactive_member_and_anonymous_uploads_record_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-inactive-owner@airbyte.io"
    )
    inactive = await _seed_user_and_membership(
        admin_pool, "audit-inactive@airbyte.io", org_id, active=False
    )

    assert (
        _upload(app_client, inactive, "audit-inactive@airbyte.io", account_id).status_code
        == 403
    )
    anonymous = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("a.txt", b"body", "text/plain")},
    )
    assert anonymous.status_code == 401
    assert await _events(admin_pool, org_id) == []


async def test_audit_actor_is_the_authenticated_user_not_a_request_field(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """A forged actor/org in the request cannot influence audit identity."""
    owner, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-actor-owner@airbyte.io"
    )
    other = await _seed_user_and_membership(
        admin_pool, "audit-actor-other@airbyte.io", org_id
    )
    foreign_org = uuid.uuid4()

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("a.txt", b"body", "text/plain")},
        data={"user_id": str(owner), "org_id": str(foreign_org)},
        headers={
            **_auth_header(other, "audit-actor-other@airbyte.io"),
            "X-User-Id": str(owner),
            "X-Org-Id": str(foreign_org),
        },
    )
    assert response.status_code == 201

    events = await _events(admin_pool, org_id, "transcript_upload")
    assert len(events) == 1
    assert events[0]["user_id"] == other
    assert events[0]["org_id"] == org_id


# ---------------------------------------------------------------------------
# transcript_delete
# ---------------------------------------------------------------------------


async def test_delete_records_one_event_at_tombstone_time(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-delete@airbyte.io"
    )
    upload = _upload(app_client, user_id, "audit-delete@airbyte.io", account_id)
    assert upload.status_code == 201
    transcript_id = uuid.UUID(upload.json()["id"])

    response = app_client.delete(
        f"/api/hosted/accounts/{account_id}/transcripts/{transcript_id}",
        headers=_auth_header(user_id, "audit-delete@airbyte.io"),
    )
    assert response.status_code == 202

    events = await _events(admin_pool, org_id, "transcript_delete")
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == user_id
    assert event["entity_id"] == transcript_id
    assert _metadata(event) == {
        "transcript_id": str(transcript_id),
        "account_id": str(account_id),
        "opportunity_id": None,
    }
    _assert_no_customer_data(event)

    async with admin_pool.acquire() as conn:
        # The row survives as the tombstoned provenance anchor, and the event
        # describes the accepted request, not a completed Storage purge.
        row = await conn.fetchrow(
            "SELECT tombstoned_at, cleanup_state, delete_requested_by"
            " FROM public.transcripts WHERE id = $1",
            transcript_id,
        )
        assert row["tombstoned_at"] is not None
        assert row["cleanup_state"] == "pending"
        assert row["delete_requested_by"] == user_id
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.audit_events WHERE entity_id = $1",
                transcript_id,
            )
            == 2
        )


async def test_tombstone_failure_records_no_delete_event(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion is now one database transaction, so its failure is total: the
    transcript stays visible, no Storage call was ever made, and no delete
    evidence exists.
    """
    from asyncpg.connection import Connection

    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-delete-dbfail@airbyte.io"
    )
    upload = _upload(app_client, user_id, "audit-delete-dbfail@airbyte.io", account_id)
    transcript_id = uuid.UUID(upload.json()["id"])
    original_fetchval = Connection.fetchval

    async def fake_fetchval(self: Any, query: str, *args: Any, **kwargs: Any) -> Any:
        if "request_transcript_deletion" in query:
            raise asyncpg.exceptions.DeadlockDetectedError("tombstone failed")
        return await original_fetchval(self, query, *args, **kwargs)

    monkeypatch.setattr(Connection, "fetchval", fake_fetchval)
    response = app_client.delete(
        f"/api/hosted/accounts/{account_id}/transcripts/{transcript_id}",
        headers=_auth_header(user_id, "audit-delete-dbfail@airbyte.io"),
    )
    monkeypatch.setattr(Connection, "fetchval", original_fetchval)

    assert response.status_code == 500
    assert await _events(admin_pool, org_id, "transcript_delete") == []
    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT tombstoned_at FROM public.transcripts WHERE id = $1",
                transcript_id,
            )
            is None
        )


async def test_repeated_and_foreign_deletes_record_no_extra_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id = await _seed_upload_ready_org(
        admin_pool, "audit-delete-repeat@airbyte.io"
    )
    user_b, org_b, _ = await _seed_member(admin_pool, "audit-delete-foreign@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)

    upload = _upload(app_client, user_id, "audit-delete-repeat@airbyte.io", account_id)
    transcript_id = uuid.UUID(upload.json()["id"])
    url = f"/api/hosted/accounts/{account_id}/transcripts/{transcript_id}"
    headers = _auth_header(user_id, "audit-delete-repeat@airbyte.io")

    assert app_client.delete(url, headers=headers).status_code == 202
    replay = app_client.delete(url, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["status"] == "already_deleted"
    assert len(await _events(admin_pool, org_id, "transcript_delete")) == 1

    # A member of another org cannot delete it (already gone) or a live one.
    other_upload = _upload(app_client, user_id, "audit-delete-repeat@airbyte.io", account_id)
    other_id = uuid.UUID(other_upload.json()["id"])
    foreign = app_client.delete(
        f"/api/hosted/accounts/{account_b}/transcripts/{other_id}",
        headers=_auth_header(user_b, "audit-delete-foreign@airbyte.io"),
    )
    assert foreign.status_code == 404
    assert await _events(admin_pool, org_b) == []
    assert len(await _events(admin_pool, org_id, "transcript_delete")) == 1


# ---------------------------------------------------------------------------
# Direct database boundaries
# ---------------------------------------------------------------------------


async def test_app_user_cannot_forge_update_or_delete_audit_events(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "audit-forge@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    transcript_id = await _seed_transcript(
        admin_pool, org_id, account_id, None, user_id
    )

    forged: tuple[tuple[str, tuple[Any, ...]], ...] = (
        (
            "INSERT INTO public.audit_events (org_id, user_id, action, entity_type, entity_id) "
            "VALUES ($1, $2, 'transcript_upload', 'transcripts', $3)",
            (org_id, user_id, transcript_id),
        ),
        (
            "UPDATE public.audit_events SET action = 'transcript_delete' WHERE org_id = $1",
            (org_id,),
        ),
        ("DELETE FROM public.audit_events WHERE org_id = $1", (org_id,)),
    )
    async with user_pool.acquire() as conn:
        for query, args in forged:
            # A denied statement aborts its transaction, so each attempt runs in
            # its own.
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    _context_token(user_id),
                )
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await conn.execute(query, *args)


async def test_app_worker_cannot_write_audit_events(
    worker_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "audit-worker@airbyte.io")

    async with worker_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO public.audit_events (org_id, user_id, action, entity_type) "
                "VALUES ($1, $2, 'transcript_upload', 'transcripts')",
                org_id,
                user_id,
            )


async def test_direct_app_user_transcript_dml_cannot_bypass_the_audit(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """Direct metadata DML is audited too, and DML without a signed tenant
    context is refused by row-level security, so there is no unaudited path.
    Deletion is no longer expressible as direct DML at all: `app_user` lost the
    DELETE privilege, so the tombstone function is the only way to delete.
    """
    user_id, org_id, _ = await _seed_member(admin_pool, "audit-direct@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    transcript_id = uuid.uuid4()
    insert = """
        INSERT INTO public.transcripts
            (id, org_id, account_id, storage_path, original_filename,
             size_bytes, mime_type, uploaded_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """
    args = (
        transcript_id,
        org_id,
        account_id,
        f"{org_id}/{account_id}/transcripts/{transcript_id}",
        "quarterly-forecast-call.txt",
        1,
        "text/plain",
        user_id,
    )

    async with user_pool.acquire() as conn:
        # No tenant context: RLS refuses the write, so nothing to audit.
        async with conn.transaction():
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(insert, *args)

        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_id),
            )
            await conn.execute(insert, *args)

        # A direct hard delete cannot skip the tombstone, its audit event, or the
        # Storage cleanup it schedules: `app_user` lost the DELETE privilege, and
        # the reconciliation guard rejects the statement even where the privilege
        # survives, so the row is still there afterwards either way.
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_id),
            )
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "DELETE FROM public.transcripts WHERE id = $1", transcript_id
                )

    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.transcripts WHERE id = $1", transcript_id
            )
            == 1
        )

    events = await _events(admin_pool, org_id)
    assert [e["action"] for e in events] == ["transcript_upload"]
    for event in events:
        assert event["user_id"] == user_id
        _assert_no_customer_data(event)


async def test_forged_context_token_cannot_produce_audit_evidence(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "audit-forged-ctx@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    transcript_id = uuid.uuid4()

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                f"{user_id}:deadbeef",
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    """
                    INSERT INTO public.transcripts
                        (id, org_id, account_id, storage_path, original_filename,
                         size_bytes, mime_type, uploaded_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    transcript_id,
                    org_id,
                    account_id,
                    f"{org_id}/{account_id}/transcripts/{transcript_id}",
                    "a.txt",
                    1,
                    "text/plain",
                    user_id,
                )

    assert await _events(admin_pool, org_id) == []


# ---------------------------------------------------------------------------
# job_run_requested
# ---------------------------------------------------------------------------


async def _seed_job_org(
    admin_pool: asyncpg.Pool, email: str, *, with_opportunity: bool = False
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    user_id, org_id, _ = await _seed_member(admin_pool, email)
    account_id = await _seed_account(
        admin_pool, org_id, user_id, name="Acme Confidential Corp"
    )
    opportunity_id = (
        await _seed_opportunity(admin_pool, org_id, account_id, user_id)
        if with_opportunity
        else None
    )
    transcript_id = await _seed_transcript(
        admin_pool,
        org_id,
        account_id,
        opportunity_id,
        user_id,
        filename="quarterly-forecast-call.txt",
    )
    return user_id, org_id, account_id, transcript_id, opportunity_id


def _job_body(
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    opportunity_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    skill: str = "post-call",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_id": str(account_id),
        "transcript_id": str(transcript_id),
        "skill": skill,
    }
    if opportunity_id is not None:
        body["opportunity_id"] = str(opportunity_id)
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return body


async def test_run_request_records_one_event_tied_to_the_job(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-run@airbyte.io"
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json=_job_body(account_id, transcript_id),
        headers=_auth_header(user_id, "audit-run@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = uuid.UUID(response.json()["id"])

    events = await _events(admin_pool, org_id, "job_run_requested")
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == user_id
    assert event["entity_type"] == "jobs"
    assert event["entity_id"] == job_id
    assert event["request_id"] is None
    assert _metadata(event) == {
        "job_id": str(job_id),
        "account_id": str(account_id),
        "transcript_id": str(transcript_id),
        "opportunity_id": None,
    }
    _assert_no_customer_data(event)


async def test_hostile_skill_value_cannot_reach_audit_metadata(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """`JobCreate.skill` is unconstrained client text, so it is excluded from
    audit metadata: a customer-data marker or oversized value sent in that field
    reaches `public.jobs` but never `public.audit_events`.
    """
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-run-hostile-skill@airbyte.io"
    )
    hostile = "Acme Confidential Corp — renewal at risk " + ("A" * 4096)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json=_job_body(account_id, transcript_id, skill=hostile),
        headers=_auth_header(user_id, "audit-run-hostile-skill@airbyte.io"),
    )
    assert response.status_code == 201
    job_id = uuid.UUID(response.json()["id"])

    events = await _events(admin_pool, org_id, "job_run_requested")
    assert len(events) == 1
    metadata = _metadata(events[0])
    assert "skill" not in metadata
    assert set(metadata) == {"job_id", "account_id", "transcript_id", "opportunity_id"}
    assert all(value is None or _is_uuid(value) for value in metadata.values())
    _assert_no_customer_data(events[0])

    headers = _auth_header(user_id, "audit-run-hostile-skill@airbyte.io")
    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel", headers=headers).status_code
        == 204
    )
    cancels = await _events(admin_pool, org_id, "job_cancel_requested")
    assert len(cancels) == 1
    assert "skill" not in _metadata(cancels[0])
    _assert_no_customer_data(cancels[0])


async def test_idempotent_replay_records_no_second_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-run-replay@airbyte.io"
    )
    body = _job_body(account_id, transcript_id, idempotency_key="idem-key-replay")
    headers = _auth_header(user_id, "audit-run-replay@airbyte.io")
    url = f"/api/hosted/accounts/{account_id}/jobs"

    first = app_client.post(url, json=body, headers=headers)
    second = app_client.post(url, json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]

    events = await _events(admin_pool, org_id, "job_run_requested")
    assert len(events) == 1
    _assert_no_customer_data(events[0])


async def test_concurrent_identical_run_requests_record_one_event(
    admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool
) -> None:
    """Two concurrent enqueues sharing an idempotency key create one job and one
    audit event.
    """
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-run-race@airbyte.io"
    )

    async def enqueue() -> None:
        async with user_pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow(
                    """
                    SELECT * FROM public.enqueue_job(
                        $1, $2, $3, NULL, 'post-call', '1.0', 'echo', 'slice4',
                        'idem-key-race', 3, 900, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
                    )
                    """,
                    _context_token(user_id),
                    account_id,
                    transcript_id,
                )

    results = await asyncio.gather(enqueue(), enqueue(), return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results), results

    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.jobs WHERE org_id = $1", org_id
            )
            == 1
        )
    assert len(await _events(admin_pool, org_id, "job_run_requested")) == 1


async def test_conflicting_idempotency_reuse_records_no_extra_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-run-conflict@airbyte.io"
    )
    other_transcript = await _seed_transcript(
        admin_pool, org_id, account_id, None, user_id
    )
    headers = _auth_header(user_id, "audit-run-conflict@airbyte.io")
    url = f"/api/hosted/accounts/{account_id}/jobs"

    first = app_client.post(
        url,
        json=_job_body(account_id, transcript_id, idempotency_key="idem-key-conflict"),
        headers=headers,
    )
    assert first.status_code == 201
    conflict = app_client.post(
        url,
        json=_job_body(
            account_id, other_transcript, idempotency_key="idem-key-conflict"
        ),
        headers=headers,
    )
    assert conflict.status_code == 409

    events = await _events(admin_pool, org_id, "job_run_requested")
    assert len(events) == 1
    assert _metadata(events[0])["transcript_id"] == str(transcript_id)


async def test_invalid_linkage_and_unauthorized_run_requests_record_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-run-invalid@airbyte.io"
    )
    user_b, org_b, _ = await _seed_member(admin_pool, "audit-run-foreign@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)
    headers = _auth_header(user_id, "audit-run-invalid@airbyte.io")

    # Transcript that does not exist.
    assert (
        app_client.post(
            f"/api/hosted/accounts/{account_id}/jobs",
            json=_job_body(account_id, uuid.uuid4()),
            headers=headers,
        ).status_code
        == 404
    )
    # Cross-org transcript.
    foreign_transcript = await _seed_transcript(
        admin_pool, org_b, account_b, None, user_b
    )
    assert (
        app_client.post(
            f"/api/hosted/accounts/{account_id}/jobs",
            json=_job_body(account_id, foreign_transcript),
            headers=headers,
        ).status_code
        == 404
    )
    # Unauthenticated.
    assert (
        app_client.post(
            f"/api/hosted/accounts/{account_id}/jobs",
            json=_job_body(account_id, transcript_id),
        ).status_code
        == 401
    )

    assert await _events(admin_pool, org_id) == []
    assert await _events(admin_pool, org_b) == []


# ---------------------------------------------------------------------------
# job_cancel_requested
# ---------------------------------------------------------------------------


async def _create_job(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    email: str,
    *,
    with_opportunity: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    user_id, org_id, account_id, transcript_id, opportunity_id = await _seed_job_org(
        admin_pool, email, with_opportunity=with_opportunity
    )
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/jobs",
        json=_job_body(account_id, transcript_id, opportunity_id),
        headers=_auth_header(user_id, email),
    )
    assert response.status_code == 201
    return (
        user_id,
        org_id,
        uuid.UUID(response.json()["id"]),
        transcript_id,
        opportunity_id,
    )


async def test_cancelling_queued_job_records_one_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, job_id, transcript_id, _ = await _create_job(
        app_client, admin_pool, "audit-cancel-queued@airbyte.io"
    )
    headers = _auth_header(user_id, "audit-cancel-queued@airbyte.io")

    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel", headers=headers).status_code
        == 204
    )
    # A retried cancellation is idempotent for an already-cancelled job and adds
    # no second event.
    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel", headers=headers).status_code
        == 204
    )

    events = await _events(admin_pool, org_id, "job_cancel_requested")
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == user_id
    assert event["entity_type"] == "jobs"
    assert event["entity_id"] == job_id
    assert _metadata(event)["transcript_id"] == str(transcript_id)
    assert "skill" not in _metadata(event)
    _assert_no_customer_data(event)


async def test_cancelling_running_job_records_one_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, job_id, _, _ = await _create_job(
        app_client, admin_pool, "audit-cancel-running@airbyte.io"
    )
    headers = _auth_header(user_id, "audit-cancel-running@airbyte.io")

    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.jobs SET status = 'running', started_at = now() WHERE id = $1",
            job_id,
        )

    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel", headers=headers).status_code
        == 204
    )
    # The second request finds the cancellation already recorded and is rejected
    # by the database rather than duplicating evidence.
    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel", headers=headers).status_code
        == 400
    )

    events = await _events(admin_pool, org_id, "job_cancel_requested")
    assert len(events) == 1
    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, cancel_requested_at, cancelled_by FROM public.jobs WHERE id = $1",
            job_id,
        )
    assert row["status"] == "running"
    assert row["cancel_requested_at"] is not None
    assert row["cancelled_by"] == user_id


async def test_terminal_missing_and_foreign_cancellations_record_no_event(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, job_id, _, _ = await _create_job(
        app_client, admin_pool, "audit-cancel-terminal@airbyte.io"
    )
    headers = _auth_header(user_id, "audit-cancel-terminal@airbyte.io")
    user_b, org_b, _ = await _seed_member(admin_pool, "audit-cancel-foreign@airbyte.io")

    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.jobs SET status = 'success', finished_at = now() WHERE id = $1",
            job_id,
        )

    # Terminal, missing, and cross-organization cancellations are all refused by
    # the database boundary and surface as a customer-safe 400.
    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel", headers=headers).status_code
        == 400
    )
    assert (
        app_client.post(
            f"/api/hosted/jobs/{uuid.uuid4()}/cancel", headers=headers
        ).status_code
        == 400
    )
    assert (
        app_client.post(
            f"/api/hosted/jobs/{job_id}/cancel",
            headers=_auth_header(user_b, "audit-cancel-foreign@airbyte.io"),
        ).status_code
        == 400
    )
    assert (
        app_client.post(f"/api/hosted/jobs/{job_id}/cancel").status_code == 401
    )

    assert await _events(admin_pool, org_id, "job_cancel_requested") == []
    assert await _events(admin_pool, org_b) == []


async def test_cancellation_racing_a_terminal_transition_records_no_stale_event(
    admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool
) -> None:
    """When the job reaches a terminal state first, the cancellation request is
    rejected by the database and leaves no evidence.
    """
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-cancel-race@airbyte.io"
    )
    async with user_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM public.enqueue_job(
                $1, $2, $3, NULL, 'post-call', '1.0', 'echo', 'slice4',
                NULL, 3, 900, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
            )
            """,
            _context_token(user_id),
            account_id,
            transcript_id,
        )
    job_id = row["job_id"]

    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.jobs SET status = 'failure', finished_at = now() WHERE id = $1",
            job_id,
        )

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.PostgresError):
            await conn.fetchval(
                "SELECT public.request_job_cancellation($1, $2)",
                _context_token(user_id),
                job_id,
            )

    assert await _events(admin_pool, org_id, "job_cancel_requested") == []


async def test_concurrent_cancellations_record_one_event(
    admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool
) -> None:
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-cancel-concurrent@airbyte.io"
    )
    other = await _seed_user_and_membership(
        admin_pool, "audit-cancel-concurrent-2@airbyte.io", org_id
    )
    async with user_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM public.enqueue_job(
                $1, $2, $3, NULL, 'post-call', '1.0', 'echo', 'slice4',
                NULL, 3, 900, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
            )
            """,
            _context_token(user_id),
            account_id,
            transcript_id,
        )
    job_id = row["job_id"]

    async def cancel(actor: uuid.UUID) -> Any:
        async with user_pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT public.request_job_cancellation($1, $2)",
                _context_token(actor),
                job_id,
            )

    results = await asyncio.gather(
        cancel(user_id), cancel(other), return_exceptions=True
    )
    assert any(r is True for r in results), results

    events = await _events(admin_pool, org_id, "job_cancel_requested")
    assert len(events) == 1
    assert events[0]["user_id"] in (user_id, other)


async def test_worker_lifecycle_records_no_audit_events(
    admin_pool: asyncpg.Pool, worker_pool: asyncpg.Pool, hosted_env: dict[str, str]
) -> None:
    """`jobs`/`job_attempts` stay the lifecycle ledger: claim, heartbeat, and
    completion add no audit evidence.
    """
    user_id, org_id, account_id, transcript_id, _ = await _seed_job_org(
        admin_pool, "audit-worker-lifecycle@airbyte.io"
    )
    async with admin_pool.acquire() as conn:
        job_id = await conn.fetchval(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id, skill,
                status, payload, input_refs, source_manifest
            ) VALUES ($1, $2, $3, $4, 'post-call', 'queued',
                      '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)
            RETURNING id
            """,
            org_id,
            account_id,
            transcript_id,
            user_id,
        )

    async with worker_pool.acquire() as conn:
        claim = await conn.fetchrow(
            "SELECT * FROM public.claim_next_job($1, $2)", "worker-audit-test", 30
        )
        assert claim is not None
        # The heartbeat return value reports whether cancellation was requested.
        assert not await conn.fetchval(
            "SELECT public.worker_heartbeat($1, $2, $3, $4)",
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            30,
        )
        await conn.fetchval(
            "SELECT public.complete_job($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)",
            claim["job_id"],
            claim["attempt_number"],
            claim["lease_token"],
            None,
            "unvalidated",
            "{}",
            0.0,
            "slice4-echo",
            "echo",
        )

    assert await _events(admin_pool, org_id) == []
    assert await _count_all(admin_pool) == 0
    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM public.jobs WHERE id = $1", job_id)
            == "success"
        )


# ---------------------------------------------------------------------------
# Migration behavior
# ---------------------------------------------------------------------------


async def test_fresh_install_exposes_the_full_action_allowlist(
    admin_pool: asyncpg.Pool,
) -> None:
    async with admin_pool.acquire() as conn:
        definition = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'audit_events' AND c.conname = 'audit_events_action_check'
            """
        )
    for action in (
        "output_comment",
        "output_correction",
        "output_approval",
        "output_export",
        *_TRANSCRIPT_ACTIONS,
        *_JOB_ACTIONS,
    ):
        assert action in definition

    async with admin_pool.acquire() as conn:
        org_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
            org_id,
            "Constraint Org",
            f"constraint-org-{uuid.uuid4().hex[:8]}",
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO public.audit_events (org_id, action, entity_type) "
                "VALUES ($1, 'worker_heartbeat', 'jobs')",
                org_id,
            )


@pytest.mark.slow
async def test_upgrade_from_011_preserves_existing_audit_evidence(
    hosted_env: dict[str, str], tmp_path: Path
) -> None:
    """Migrating an 011 database with synthetic evidence to 012 keeps prior rows
    and enables the new actions.
    """
    import hosted
    import hosted.migrations as migrations

    host_port = hosted_env["MIGRATE_DATABASE_URL"].rsplit("/", 1)[0]
    test_db = f"upgrade_012_{uuid.uuid4().hex[:8]}"
    partial_dir = tmp_path / "migrations_011"
    partial_dir.mkdir()
    for version, path in migrations.list_migrations(hosted.config.MIGRATIONS_DIR):
        if version <= "011":
            shutil.copyfile(path, partial_dir / path.name)

    admin_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
    try:
        await admin_conn.execute(f'CREATE DATABASE "{test_db}"')
    finally:
        await admin_conn.close()

    test_dsn = f"{host_port}/{test_db}"
    conn = None
    try:
        await migrations.migrate(
            test_dsn,
            partial_dir,
            app_user_password="app_user_password",
            app_admin_password="app_admin_password",
            app_worker_password="app_worker_password",
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )

        conn = await asyncpg.connect(test_dsn)
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        transcript_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.users (id, email) VALUES ($1, $2)",
            user_id,
            "upgrade@airbyte.io",
        )
        await conn.execute(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
            org_id,
            "Upgrade Org",
            f"upgrade-org-{uuid.uuid4().hex[:8]}",
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
            f"upgrade-account-{uuid.uuid4().hex[:8]}",
            user_id,
        )
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
            f"{org_id}/{account_id}/transcripts/{transcript_id}",
            user_id,
        )
        job_id = await conn.fetchval(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id, skill,
                status, payload, input_refs, source_manifest
            ) VALUES ($1, $2, $3, $4, 'post-call', 'queued',
                      '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)
            RETURNING id
            """,
            org_id,
            account_id,
            transcript_id,
            user_id,
        )
        for action, entity_type in (
            ("output_comment", "outputs"),
            ("output_correction", "outputs"),
            ("output_approval", "outputs"),
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
        legacy_count = await conn.fetchval("SELECT count(*) FROM public.audit_events")
        assert legacy_count == 4
        await conn.close()
        conn = None

        await migrations.migrate(
            test_dsn,
            hosted.config.MIGRATIONS_DIR,
            app_user_password="app_user_password",
            app_admin_password="app_admin_password",
            app_worker_password="app_worker_password",
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )

        conn = await asyncpg.connect(test_dsn)
        assert await conn.fetchval("SELECT count(*) FROM public.audit_events") == 4
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.transcripts WHERE id = $1", transcript_id
            )
            == 1
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM public.jobs WHERE id = $1", job_id)
            == 1
        )
        # The new actions are accepted after the upgrade.
        await conn.execute(
            "INSERT INTO public.audit_events (org_id, user_id, action, entity_type, entity_id) "
            "VALUES ($1, $2, 'transcript_upload', 'transcripts', $3)",
            org_id,
            user_id,
            transcript_id,
        )
        # And the lifecycle backstop rejects duplicate evidence.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO public.audit_events (org_id, user_id, action, entity_type, entity_id) "
                "VALUES ($1, $2, 'transcript_upload', 'transcripts', $3)",
                org_id,
                user_id,
                transcript_id,
            )
    finally:
        if conn is not None:
            await conn.close()
        drop_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
        try:
            await drop_conn.execute(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)')
        finally:
            await drop_conn.close()
