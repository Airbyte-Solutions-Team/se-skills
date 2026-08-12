"""Integration tests for the hosted transcript upload/storage vertical slice."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from hosted.models import OrgContext

from .hosted_helpers import (
    _auth_header,
    _context_token,
    _seed_account,
    _seed_member,
    _seed_opportunity,
    _seed_user_and_membership,
    _token,
)


@pytest.mark.hosted
@pytest.mark.slow
async def test_member_can_upload_list_download_delete_transcript(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "upload@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    auth = _auth_header(user_id, "upload@airbyte.io")

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("call.txt", b"Hello transcript", "text/plain")},
        headers=auth,
    )
    assert response.status_code == 201
    transcript = response.json()
    assert transcript["account_id"] == str(account_id)
    assert transcript["original_filename"] == "call.txt"
    assert transcript["size_bytes"] == 16

    response = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts", headers=auth
    )
    assert response.status_code == 200
    assert len(response.json()["transcripts"]) == 1

    download = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts/{transcript['id']}/download",
        headers=auth,
    )
    assert download.status_code == 200
    assert download.content == b"Hello transcript"
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers.get("x-content-type-options") == "nosniff"

    delete = app_client.delete(
        f"/api/hosted/accounts/{account_id}/transcripts/{transcript['id']}",
        headers=auth,
    )
    assert delete.status_code == 204

    response = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts", headers=auth
    )
    assert response.json()["transcripts"] == []


@pytest.mark.hosted
@pytest.mark.slow
async def test_transcript_upload_for_opportunity_requires_same_account_and_org(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "opp-upload@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    opportunity_id = await _seed_opportunity(admin_pool, org_id, account_id, user_id)
    auth = _auth_header(user_id, "opp-upload@airbyte.io")

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/opportunities/{opportunity_id}/transcripts",
        files={"file": ("opp.md", b"# Notes", "text/markdown")},
        headers=auth,
    )
    assert response.status_code == 201
    transcript = response.json()
    assert transcript["opportunity_id"] == str(opportunity_id)

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/opportunities/{opportunity_id}/transcripts",
        headers=auth,
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()["transcripts"]) == 1

    other_account = await _seed_account(admin_pool, org_id, user_id, name="Other")
    wrong = app_client.post(
        f"/api/hosted/accounts/{other_account}/opportunities/{opportunity_id}/transcripts",
        files={"file": ("wrong.txt", b"x", "text/plain")},
        headers=auth,
    )
    assert wrong.status_code == 404


@pytest.mark.hosted
@pytest.mark.slow
async def test_unauthenticated_inactive_and_non_member_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "member@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)

    response = app_client.get(f"/api/hosted/accounts/{account_id}/transcripts")
    assert response.status_code == 401

    inactive_user = await _seed_user_and_membership(
        admin_pool, "inactive@airbyte.io", org_id, active=False
    )
    response = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts",
        headers=_auth_header(inactive_user, "inactive@airbyte.io"),
    )
    assert response.status_code == 403

    other_user, other_org, _ = await _seed_member(admin_pool, "other@airbyte.io")
    response = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts",
        headers=_auth_header(other_user, "other@airbyte.io"),
    )
    assert response.status_code == 200
    assert response.json()["transcripts"] == []


@pytest.mark.hosted
@pytest.mark.slow
async def test_cross_org_transcript_access_returns_404(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_a, org_a, _ = await _seed_member(admin_pool, "a@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)
    auth_a = _auth_header(user_a, "a@airbyte.io")

    user_b, org_b, _ = await _seed_member(admin_pool, "b@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)
    auth_b = _auth_header(user_b, "b@airbyte.io")

    # Create a transcript in org A.
    upload = app_client.post(
        f"/api/hosted/accounts/{account_a}/transcripts",
        files={"file": ("a.txt", b"A content", "text/plain")},
        headers=auth_a,
    )
    assert upload.status_code == 201
    transcript_id = upload.json()["id"]

    # User B tries to download using their own org context and a transcript id
    # from org A. The API must not leak existence or content.
    download = app_client.get(
        f"/api/hosted/accounts/{account_b}/transcripts/{transcript_id}/download",
        headers=auth_b,
    )
    assert download.status_code == 404

    list_b = app_client.get(
        f"/api/hosted/accounts/{account_b}/transcripts", headers=auth_b
    )
    assert list_b.json()["transcripts"] == []


@pytest.mark.hosted
@pytest.mark.slow
async def test_browser_supplied_ids_cannot_bypass_authorization(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_a, org_a, _ = await _seed_member(admin_pool, "a2@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)
    opportunity_a = await _seed_opportunity(admin_pool, org_a, account_a, user_a)

    user_b, org_b, _ = await _seed_member(admin_pool, "b2@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)
    opportunity_b = await _seed_opportunity(admin_pool, org_b, account_b, user_b)

    # User A uploads a transcript for an opportunity in org A.
    upload = app_client.post(
        f"/api/hosted/accounts/{account_a}/opportunities/{opportunity_a}/transcripts",
        files={"file": ("a.txt", b"A", "text/plain")},
        headers=_auth_header(user_a, "a2@airbyte.io"),
    )
    assert upload.status_code == 201
    transcript_id = upload.json()["id"]

    # User B attempts to access it via an org B account/opportunity path.
    auth_b = _auth_header(user_b, "b2@airbyte.io")
    assert (
        app_client.get(
            f"/api/hosted/accounts/{account_b}/opportunities/{opportunity_b}/transcripts/{transcript_id}/download",
            headers=auth_b,
        ).status_code
        == 404
    )
    assert (
        app_client.get(
            f"/api/hosted/accounts/{account_b}/transcripts/{transcript_id}/download",
            headers=auth_b,
        ).status_code
        == 404
    )


@pytest.mark.hosted
@pytest.mark.slow
async def test_direct_app_user_cannot_read_or_mutate_other_org_transcripts(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    user_a, org_a, _ = await _seed_member(admin_pool, "app-a@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)
    user_b, org_b, _ = await _seed_member(admin_pool, "app-b@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_a),
            )
            # User A can insert a transcript for account A.
            transcript_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO public.transcripts
                    (id, org_id, account_id, storage_path, original_filename,
                     size_bytes, mime_type, uploaded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                transcript_id,
                org_a,
                account_a,
                f"{org_a}/{account_a}/transcripts/{transcript_id}",
                "a.txt",
                1,
                "text/plain",
                user_a,
            )

        # A fresh transaction: a member of org B tries to claim account A (which
        # belongs to org A) as part of org B. RLS passes because the caller is an
        # active member of org B, but the composite foreign key fails because
        # account A is not in org B.
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_b),
            )
            transcript_b_id = uuid.uuid4()
            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO public.transcripts
                        (id, org_id, account_id, storage_path, original_filename,
                         size_bytes, mime_type, uploaded_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    transcript_b_id,
                    org_b,
                    account_a,
                    f"{org_b}/{account_b}/transcripts/{transcript_b_id}",
                    "x.txt",
                    1,
                    "text/plain",
                    user_b,
                )


@pytest.mark.hosted
@pytest.mark.slow
async def test_direct_app_user_cannot_update_transcripts(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """app_user is granted only SELECT/INSERT/DELETE on transcripts; UPDATE and
    REFERENCES are not granted.
    """
    user_a, org_a, _ = await _seed_member(admin_pool, "update-a@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)

    transcript_id = uuid.uuid4()
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_a),
            )
            await conn.execute(
                """
                INSERT INTO public.transcripts
                    (id, org_id, account_id, storage_path, original_filename,
                     size_bytes, mime_type, uploaded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                transcript_id,
                org_a,
                account_a,
                f"{org_a}/{account_a}/transcripts/{transcript_id}",
                "a.txt",
                1,
                "text/plain",
                user_a,
            )

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_a),
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "UPDATE public.transcripts SET original_filename = 'b.txt' WHERE id = $1",
                    transcript_id,
                )


@pytest.mark.hosted
@pytest.mark.slow
async def test_transcripts_uploaded_by_requires_org_membership(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """The uploaded_by foreign key requires membership in the transcript's
    organization, not just any valid user id.
    """
    user_a, org_a, _ = await _seed_member(admin_pool, "uploader-a@airbyte.io")
    user_b, _, _ = await _seed_member(admin_pool, "uploader-b@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a)

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                _context_token(user_a),
            )
            transcript_id = uuid.uuid4()
            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO public.transcripts
                        (id, org_id, account_id, storage_path, original_filename,
                         size_bytes, mime_type, uploaded_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    transcript_id,
                    org_a,
                    account_a,
                    f"{org_a}/{account_a}/transcripts/{transcript_id}",
                    "a.txt",
                    1,
                    "text/plain",
                    user_b,
                )


@pytest.mark.hosted
@pytest.mark.slow
async def test_transcripts_opportunity_must_belong_to_account_and_org(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """The opportunity composite FK rejects an opportunity from the same org
    but a different account, and an opportunity from a different org.
    """
    user_a, org_a, _ = await _seed_member(admin_pool, "opp-fk-a@airbyte.io")
    account_a = await _seed_account(admin_pool, org_a, user_a, name="A")
    account_a2 = await _seed_account(admin_pool, org_a, user_a, name="A2")
    opportunity_a2 = await _seed_opportunity(admin_pool, org_a, account_a2, user_a)
    user_b, org_b, _ = await _seed_member(admin_pool, "opp-fk-b@airbyte.io")
    account_b = await _seed_account(admin_pool, org_b, user_b)
    opportunity_b = await _seed_opportunity(admin_pool, org_b, account_b, user_b)

    for label, opportunity_id in [
        ("same org, different account", opportunity_a2),
        ("different org", opportunity_b),
    ]:
        async with user_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    _context_token(user_a),
                )
                transcript_id = uuid.uuid4()
                with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                    await conn.execute(
                        """
                        INSERT INTO public.transcripts
                            (id, org_id, account_id, opportunity_id, storage_path,
                             original_filename, size_bytes, mime_type, uploaded_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        transcript_id,
                        org_a,
                        account_a,
                        opportunity_id,
                        f"{org_a}/{account_a}/transcripts/{transcript_id}",
                        f"{label}.txt",
                        1,
                        "text/plain",
                        user_a,
                    )


@pytest.mark.hosted
@pytest.mark.slow
async def test_storage_backend_enforces_org_isolation(
    admin_pool: asyncpg.Pool,
) -> None:
    from hosted import storage

    backend = storage.MemoryStorageBackend(admin_pool)
    user_a, org_a, _ = await _seed_member(admin_pool, "store-a@airbyte.io")
    user_b, org_b, _ = await _seed_member(admin_pool, "store-b@airbyte.io")

    path_a = f"{org_a}/{uuid.uuid4()}/transcripts/{uuid.uuid4()}"
    path_b = f"{org_b}/{uuid.uuid4()}/transcripts/{uuid.uuid4()}"

    await backend.upload(user_a, path_a, b"A", "text/plain")

    # User B cannot read, list, or delete A's object.
    with pytest.raises(storage.StorageAuthError):
        await backend.download(user_b, path_a)
    with pytest.raises(storage.StorageAuthError):
        await backend.delete(user_b, path_a)
    with pytest.raises(storage.StorageAuthError):
        await backend.list_prefix(user_b, f"{org_a}/")

    # Anonymous access fails.
    with pytest.raises(storage.StorageAuthError):
        await backend.download(None, path_a)

    # User B can operate within their own org.
    await backend.upload(user_b, path_b, b"B", "text/plain")
    data = await backend.download(user_b, path_b)
    content = b"".join([chunk async for chunk in data])
    assert content == b"B"


@pytest.mark.hosted
@pytest.mark.slow
async def test_storage_objects_rls_enforced_for_app_storage(
    superuser_pool: asyncpg.Pool,
) -> None:
    """The migration 002 Storage RLS policies apply to the backend's dedicated
    `app_storage` role. They allow an active member to list, insert, update, and
    delete objects under their org path, and deny cross-org, anonymous, and
    inactive-member operations.
    """
    user_a, org_a, _ = await _seed_member(superuser_pool, "rls-a@airbyte.io")
    user_b, org_b, _ = await _seed_member(superuser_pool, "rls-b@airbyte.io")
    inactive_user = await _seed_user_and_membership(
        superuser_pool, "rls-inactive@airbyte.io", org_a, active=False
    )
    account_a = await _seed_account(superuser_pool, org_a, user_a)
    account_b = await _seed_account(superuser_pool, org_b, user_b)

    path_a = f"{org_a}/{account_a}/transcripts/{uuid.uuid4()}"
    path_b = f"{org_b}/{account_b}/transcripts/{uuid.uuid4()}"

    def _jwt_claims(user_id: uuid.UUID) -> str:
        return json.dumps({"sub": str(user_id)})

    # Active org A member can insert, select, update, and delete.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_a),
            )
            await conn.execute(
                "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                path_a,
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name = $1", path_a
            )
            assert len(rows) == 1
            await conn.execute(
                "UPDATE storage.objects SET metadata = '{\"x\":1}'::jsonb WHERE name = $1",
                path_a,
            )
            await conn.execute("DELETE FROM storage.objects WHERE name = $1", path_a)

    # Active org A member can list objects under their org prefix.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_a),
            )
            await conn.execute(
                "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                path_a,
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name LIKE $1", f"{org_a}/%"
            )
            assert len(rows) == 1

    # User B cannot insert into org A; the INSERT fails WITH CHECK.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_b),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_a,
                )

    # Cross-org SELECT/LIST return empty because RLS hides the rows.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_b),
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name = $1", path_a
            )
            assert rows == []
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name LIKE $1", f"{org_a}/%"
            )
            assert rows == []

    # Cross-org UPDATE/DELETE silently affect no rows, leaving the object.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_b),
            )
            await conn.execute(
                "UPDATE storage.objects SET metadata = '{\"x\":2}'::jsonb WHERE name = $1",
                path_a,
            )
            await conn.execute("DELETE FROM storage.objects WHERE name = $1", path_a)

    # Verify the object still exists for user A.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_a),
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name = $1", path_a
            )
            assert len(rows) == 1

    # Anonymous JWT (no sub) cannot write.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_b,
                )

    # Inactive member cannot write.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(inactive_user),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_a,
                )

    # Malformed prefix (non-UUID first segment) cannot be inserted.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                _jwt_claims(user_a),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', 'badpath')"
                )


@pytest.mark.hosted
@pytest.mark.slow
async def test_authenticated_role_cannot_access_storage_objects(
    superuser_pool: asyncpg.Pool,
) -> None:
    """The browser-visible authenticated role has no storage object privileges.
    This is the SQL boundary proof that a browser using the anon key + user JWT
    cannot bypass FastAPI to perform transcript object operations.
    """
    user_a, org_a, _ = await _seed_member(superuser_pool, "authz-a@airbyte.io")
    account_a = await _seed_account(superuser_pool, org_a, user_a)
    path_a = f"{org_a}/{account_a}/transcripts/{uuid.uuid4()}"

    # Pre-create an object as app_storage so the bucket is not empty.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_a)}),
            )
            await conn.execute(
                "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                path_a,
            )

    # authenticated role cannot read or write even with a valid user JWT.
    for operation, args in [
        (
            "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
            (f"{org_a}/{account_a}/transcripts/{uuid.uuid4()}",),
        ),
        ("SELECT name FROM storage.objects WHERE name = $1", (path_a,)),
        ("UPDATE storage.objects SET metadata = '{\"x\":1}'::jsonb WHERE name = $1", (path_a,)),
        ("DELETE FROM storage.objects WHERE name = $1", (path_a,)),
    ]:
        async with superuser_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE authenticated")
                await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
                await conn.execute(
                    "SELECT set_config('request.jwt.claims', $1, true)",
                    json.dumps({"sub": str(user_a)}),
                )
                with pytest.raises(asyncpg.exceptions.PostgresError):
                    await conn.execute(operation, *args)


@pytest.mark.hosted
@pytest.mark.slow
async def test_authenticator_can_assume_app_storage_for_storage_operations(
    superuser_pool: asyncpg.Pool,
    authenticator_pool: asyncpg.Pool,
) -> None:
    """Supabase's authenticator role must be able to switch into app_storage
    based on the JWT role claim and perform allowed same-org operations, but it
    cannot access storage.objects directly without switching roles.
    """
    user_a, org_a, _ = await _seed_member(superuser_pool, "authn-a@airbyte.io")
    account_a = await _seed_account(superuser_pool, org_a, user_a)
    path_a = f"{org_a}/{account_a}/transcripts/{uuid.uuid4()}"

    def _jwt_claims(user_id: uuid.UUID) -> str:
        return json.dumps({"sub": str(user_id), "role": "app_storage"})

    # Without SET ROLE, the authenticator role has no storage.objects privileges
    # even though it is a member of app_storage (membership is NOINHERIT).
    async with authenticator_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)", _jwt_claims(user_a)
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_a,
                )

    # After SET ROLE app_storage, an active member can insert, select, update, and delete.
    async with authenticator_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE app_storage")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)", _jwt_claims(user_a)
            )
            await conn.execute(
                "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                path_a,
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name = $1", path_a
            )
            assert len(rows) == 1
            await conn.execute(
                "UPDATE storage.objects SET metadata = '{\"x\":1}'::jsonb WHERE name = $1",
                path_a,
            )
            await conn.execute("DELETE FROM storage.objects WHERE name = $1", path_a)


@pytest.mark.hosted
@pytest.mark.slow
async def test_authenticator_cannot_assume_privileged_roles(
    superuser_pool: asyncpg.Pool,
    authenticator_pool: asyncpg.Pool,
) -> None:
    """The authenticator role must not be able to switch into privileged
    migration/admin roles, only app_storage.
    """
    user_a, org_a, _ = await _seed_member(superuser_pool, "authn-priv@airbyte.io")

    async with authenticator_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_a), "role": "app_admin"}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute("SET LOCAL ROLE app_admin")


@pytest.mark.hosted
@pytest.mark.slow
async def test_authenticated_role_cannot_assume_app_storage(
    superuser_pool: asyncpg.Pool,
    authenticated_pool: asyncpg.Pool,
) -> None:
    """The browser-visible authenticated role has no membership in app_storage,
    so a real connection as authenticated cannot switch into the Storage role
    even with a JWT claim of role=app_storage.
    """
    user_a, org_a, _ = await _seed_member(superuser_pool, "authz-role@airbyte.io")

    async with authenticated_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_a), "role": "app_storage"}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute("SET LOCAL ROLE app_storage")


@pytest.mark.hosted
@pytest.mark.slow
async def test_storage_bucket_is_private_after_migration(
    superuser_pool: asyncpg.Pool,
) -> None:
    async with superuser_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT public FROM storage.buckets WHERE name = 'transcripts'"
        )
    assert row is not None
    assert row["public"] is False


@pytest.mark.hosted
@pytest.mark.slow
async def test_storage_bucket_public_is_forced_private_by_migration(
    superuser_pool: asyncpg.Pool,
    hosted_env: dict[str, str],
) -> None:
    """If a pre-existing transcripts bucket is public, migration 002 forces it
    private and fails loudly if it cannot."""
    import hosted.migrations as migrations

    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE storage.buckets SET public = true WHERE name = 'transcripts'"
            )

    # Re-run migration 002; it must force the bucket private.
    async with superuser_pool.acquire() as conn:
        await conn.execute("DELETE FROM public.schema_migrations WHERE version = '002'")

    import hosted.config

    await migrations.migrate(
        hosted_env["MIGRATE_DATABASE_URL"],
        hosted.config.MIGRATIONS_DIR,
        app_user_password="app_user_password",
        app_admin_password="app_admin_password",
        app_worker_password="app_worker_password",
        context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
    )

    async with superuser_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT public FROM storage.buckets WHERE name = 'transcripts'"
        )
    assert row is not None
    assert row["public"] is False


@pytest.mark.hosted
@pytest.mark.slow
async def test_supabase_storage_backend_uses_server_signed_app_storage_token(
    app_client: TestClient,
) -> None:
    """The production backend signs a short-lived JWT for the dedicated
    `app_storage` Postgres role using the server-side Supabase JWT secret. The
    public anon key is only used as an `apikey` project identifier; a service
    role or the user's browser token is never sent to Storage.
    """
    import os

    import jwt as pyjwt
    from hosted import storage

    user_id = uuid.uuid4()
    backend = storage.SupabaseStorageBackend()
    assert storage.BUCKET == "transcripts"
    headers = backend._headers(user_id)
    assert headers["apikey"] == "anon-key"
    auth_header = headers["Authorization"]
    assert auth_header.startswith("Bearer ")
    token = auth_header.split(" ", 1)[1]
    decoded = pyjwt.decode(token, os.environ["SUPABASE_JWT_SECRET"], algorithms=["HS256"])
    assert decoded["sub"] == str(user_id)
    assert decoded["role"] == "app_storage"
    assert "service_role" not in headers
    assert "service-role" not in headers


@pytest.mark.hosted
@pytest.mark.slow
@pytest.mark.parametrize(
    "filename,content,expected_status",
    [
        pytest.param("call.txt", b"valid", 201, id="valid_txt"),
        pytest.param("notes.md", b"# notes", 201, id="valid_md"),
        pytest.param("sub.vtt", b"WEBVTT", 201, id="valid_vtt"),
        pytest.param("sub.srt", b"1\n00:00:00 --> 00:00:01\nHi", 201, id="valid_srt"),
        pytest.param("mal.exe", b"binary", 400, id="bad_extension"),
        pytest.param("empty.txt", b"", 400, id="empty_file"),
        pytest.param("nul.txt", b"a\x00b", 400, id="nul_byte"),
        pytest.param("utf16.txt", "é".encode("utf-16"), 400, id="invalid_utf8"),
        pytest.param(
            "page.txt",
            b"<!DOCTYPE html><html><body>Not a transcript</body></html>",
            400,
            id="html_renamed_txt",
        ),
        pytest.param(
            "notes.md",
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>",
            400,
            id="pdf_renamed_md",
        ),
        pytest.param(
            "archive.txt",
            b"PK\x03\x04\x14\x00\x00\x00\x00\x00",
            400,
            id="zip_renamed_txt",
        ),
        pytest.param(
            "script.txt",
            b"<?php echo 'not a transcript'; ?>",
            400,
            id="php_renamed_txt",
        ),
        pytest.param(
            "exec.txt",
            b"MZ\x90\x00\x03\x00\x00\x00",
            400,
            id="exe_renamed_txt",
        ),
        pytest.param(
            "chat.txt",
            b"User: <script>alert(1)</script> but this is text",
            201,
            id="html_tag_in_body_allowed",
        ),
    ],
)
async def test_upload_validation_rejects_unsafe_files(
    filename: str,
    content: bytes,
    expected_status: int,
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "validate@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": (filename, content, "application/octet-stream")},
        headers=_auth_header(user_id, "validate@airbyte.io"),
    )
    assert response.status_code == expected_status


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_rejects_oversized_file(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "oversize@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    monkeypatch.setattr("hosted.config.TRANSCRIPT_MAX_BYTES", 10)
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("big.txt", b"x" * 11, "text/plain")},
        headers=_auth_header(user_id, "oversize@airbyte.io"),
    )
    assert response.status_code == 400


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_validates_account_before_storage(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """Uploading to a non-existent or cross-org account fails before Storage
    receives any bytes, leaving no orphan object."""
    user_id, org_id, _ = await _seed_member(admin_pool, "validate-account@airbyte.io")

    response = app_client.post(
        f"/api/hosted/accounts/{uuid.uuid4()}/transcripts",
        files={"file": ("orphan.txt", b"orphan", "text/plain")},
        headers=_auth_header(user_id, "validate-account@airbyte.io"),
    )
    assert response.status_code == 404
    assert not app_client.app.state.storage_backend.objects


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_validates_opportunity_before_storage(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """Uploading to an opportunity that does not belong to the selected
    account and org fails before Storage receives any bytes."""
    user_id, org_id, _ = await _seed_member(admin_pool, "validate-opp@airbyte.io")
    account_a = await _seed_account(admin_pool, org_id, user_id, name="Account A")
    account_b = await _seed_account(admin_pool, org_id, user_id, name="Account B")
    opportunity_a = await _seed_opportunity(admin_pool, org_id, account_a, user_id)

    response = app_client.post(
        f"/api/hosted/accounts/{account_b}/opportunities/{opportunity_a}/transcripts",
        files={"file": ("opp-mismatch.txt", b"mismatch", "text/plain")},
        headers=_auth_header(user_id, "validate-opp@airbyte.io"),
    )
    assert response.status_code == 404
    assert not app_client.app.state.storage_backend.objects


@pytest.mark.hosted
@pytest.mark.slow
@pytest.mark.parametrize(
    "filename,content,expected_detail",
    [
        pytest.param(
            "late_nul.txt",
            b"a" * 8192 + b"\x00",
            "File contains NUL bytes",
            id="nul_after_first_chunk",
        ),
        pytest.param(
            "late_invalid_utf8.txt",
            b"a" * 8192 + b"\xc3\x28",
            "File is not valid UTF-8",
            id="invalid_utf8_after_first_chunk",
        ),
    ],
)
async def test_upload_rejects_invalid_content_after_first_chunk(
    filename: str,
    content: bytes,
    expected_detail: str,
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
) -> None:
    """Validation errors discovered after streaming begins return 400 and do
    not leave a listable transcript or orphaned object."""
    user_id, org_id, _ = await _seed_member(admin_pool, "late-bad@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": (filename, content, "text/plain")},
        headers=_auth_header(user_id, "late-bad@airbyte.io"),
    )
    assert response.status_code == 400
    assert expected_detail in response.text
    assert not app_client.app.state.storage_backend.objects


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_rejects_oversized_after_first_chunk(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Size-overflow detected after the first chunk returns 400 and does not
    leave an orphan object."""
    user_id, org_id, _ = await _seed_member(admin_pool, "late-big@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    monkeypatch.setattr("hosted.config.TRANSCRIPT_MAX_BYTES", 9000)
    content = b"a" * 8192 + b"b" * 1000
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("late-big.txt", content, "text/plain")},
        headers=_auth_header(user_id, "late-big@airbyte.io"),
    )
    assert response.status_code == 400
    assert "File exceeds 9000 bytes" in response.text
    assert not app_client.app.state.storage_backend.objects


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_partial_cleanup_failure_returns_500(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If validation fails after streaming starts and the compensating delete
    also fails, the API returns 500 rather than success."""
    from hosted import storage as storage_module

    user_id, org_id, _ = await _seed_member(admin_pool, "cleanup-fail@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)

    async def fake_delete(self: Any, user_id: Any, path: str) -> None:
        raise storage_module.StorageError("simulated cleanup failure")

    monkeypatch.setattr(
        app_client.app.state.storage_backend, "delete", fake_delete
    )

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("late-nul.txt", b"a" * 8192 + b"\x00", "text/plain")},
        headers=_auth_header(user_id, "cleanup-fail@airbyte.io"),
    )
    assert response.status_code == 500


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_failure_does_not_create_metadata(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "cleanup@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)

    async def raise_error(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated upload failure")

    monkeypatch.setattr(app_client.app.state.storage_backend, "upload", raise_error)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("fail.txt", b"content", "text/plain")},
        headers=_auth_header(user_id, "cleanup@airbyte.io"),
    )
    assert response.status_code == 500

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts",
        headers=_auth_header(user_id, "cleanup@airbyte.io"),
    )
    assert list_resp.json()["transcripts"] == []


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_cleans_up_object_when_metadata_insert_fails(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asyncpg import Connection

    user_id, org_id, _ = await _seed_member(admin_pool, "cleanup-insert@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    auth = _auth_header(user_id, "cleanup-insert@airbyte.io")

    original_fetchrow = Connection.fetchrow

    async def fake_fetchrow(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(query, str) and "INSERT INTO public.transcripts" in query:
            raise RuntimeError("simulated metadata insert failure")
        return await original_fetchrow(self, query, *args, **kwargs)

    monkeypatch.setattr(Connection, "fetchrow", fake_fetchrow)

    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("fail.txt", b"content", "text/plain")},
        headers=auth,
    )
    assert response.status_code == 500

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts", headers=auth
    )
    assert list_resp.json()["transcripts"] == []


@pytest.mark.hosted
@pytest.mark.slow
async def test_upload_cleans_up_object_when_transaction_finalization_fails(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hosted.transcripts as transcripts_module

    user_id, org_id, _ = await _seed_member(admin_pool, "cleanup-final@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    auth = _auth_header(user_id, "cleanup-final@airbyte.io")

    original_tenant_connection = transcripts_module.tenant_connection

    @asynccontextmanager
    async def failing_tenant_connection(request: Request, org: Any) -> Any:
        pool = request.app.state.hosted_user_pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    org.context_token,
                )
                yield conn
                raise RuntimeError("simulated transaction finalization failure")

    monkeypatch.setattr(transcripts_module, "tenant_connection", failing_tenant_connection)

    content = b"transaction-finalization-test-content"
    response = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("final.txt", content, "text/plain")},
        headers=auth,
    )
    assert response.status_code == 500

    # Restore the real resolver so the subsequent list request can verify cleanup.
    monkeypatch.setattr(transcripts_module, "tenant_connection", original_tenant_connection)

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts", headers=auth
    )
    assert list_resp.json()["transcripts"] == []
    assert not any(
        v == content for v in app_client.app.state.storage_backend.objects.values()
    )


@pytest.mark.hosted
@pytest.mark.slow
async def test_delete_metadata_failure_leaves_transcript_listable(
    app_client: TestClient, admin_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asyncpg import Connection

    user_id, org_id, _ = await _seed_member(admin_pool, "delete-meta@airbyte.io")
    account_id = await _seed_account(admin_pool, org_id, user_id)
    auth = _auth_header(user_id, "delete-meta@airbyte.io")

    upload = app_client.post(
        f"/api/hosted/accounts/{account_id}/transcripts",
        files={"file": ("keep.txt", b"keep", "text/plain")},
        headers=auth,
    )
    assert upload.status_code == 201
    transcript_id = upload.json()["id"]

    original_execute = Connection.execute

    async def fake_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(query, str) and "DELETE FROM public.transcripts" in query:
            raise RuntimeError("simulated metadata delete failure")
        return await original_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", fake_execute)

    delete = app_client.delete(
        f"/api/hosted/accounts/{account_id}/transcripts/{transcript_id}",
        headers=auth,
    )
    assert delete.status_code == 500

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/transcripts", headers=auth
    )
    assert len(list_resp.json()["transcripts"]) == 1
    assert list_resp.json()["transcripts"][0]["id"] == transcript_id
    assert not any(
        v == b"keep" for v in app_client.app.state.storage_backend.objects.values()
    )


@pytest.mark.hosted
@pytest.mark.slow
async def test_hosted_mode_does_not_expose_local_routes(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "routes@airbyte.io")
    assert app_client.get("/api/skills", headers=_auth_header(user_id, "routes@airbyte.io")).status_code == 404
    assert app_client.get("/api/transcribe/start").status_code == 404


@pytest.mark.hosted
@pytest.mark.slow
async def test_storage_migration_fails_when_authenticator_missing(
    superuser_pool: asyncpg.Pool,
    hosted_env: dict[str, str],
) -> None:
    """When the storage schema is present but the Supabase authenticator role is
    absent, migration 002 must raise a clear exception rather than silently
    omitting the required role handoff.
    """
    from hosted import migrations

    migrations_dir = Path(__file__).parent.parent.parent / "webapp" / "hosted" / "migrations"
    admin_dsn = hosted_env["MIGRATE_DATABASE_URL"]

    try:
        async with superuser_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("REVOKE app_storage FROM authenticator")
                await conn.execute("REVOKE ALL ON DATABASE test FROM authenticator")
                await conn.execute("REVOKE ALL ON SCHEMA public, storage, auth FROM authenticator")
                await conn.execute("DROP ROLE IF EXISTS authenticator")
                await conn.execute("DELETE FROM public.schema_migrations WHERE version = '002'")

        with pytest.raises(asyncpg.exceptions.PostgresError):
            await migrations.migrate(
                admin_dsn,
                migrations_dir,
                app_user_password="app_user_password",
                app_admin_password="app_admin_password",
                app_worker_password="app_worker_password",
                context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
            )
    finally:
        async with superuser_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN "
                    "CREATE ROLE authenticator WITH LOGIN NOINHERIT PASSWORD 'authenticator_password'; "
                    "END IF; END $$"
                )
                await conn.execute("GRANT CONNECT ON DATABASE test TO authenticator")
                await conn.execute("GRANT USAGE ON SCHEMA public, storage, auth TO authenticator")
                await conn.execute("ALTER ROLE authenticator SET search_path = 'auth, storage, public'")
                await conn.execute("GRANT app_storage TO authenticator")
                await conn.execute("DELETE FROM public.schema_migrations WHERE version = '002'")

        await migrations.migrate(
            admin_dsn,
            migrations_dir,
            app_user_password="app_user_password",
            app_admin_password="app_admin_password",
            app_worker_password="app_worker_password",
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )
