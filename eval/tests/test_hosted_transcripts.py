"""Integration tests for the hosted transcript upload/storage vertical slice."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
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
async def test_storage_backend_enforces_org_isolation(
    admin_pool: asyncpg.Pool,
) -> None:
    from hosted import storage

    backend = storage.MemoryStorageBackend(admin_pool)
    user_a, org_a, _ = await _seed_member(admin_pool, "store-a@airbyte.io")
    user_b, org_b, _ = await _seed_member(admin_pool, "store-b@airbyte.io")

    token_a = _token(user_a, "store-a@airbyte.io")
    token_b = _token(user_b, "store-b@airbyte.io")
    path_a = f"{org_a}/{uuid.uuid4()}/transcripts/{uuid.uuid4()}"
    path_b = f"{org_b}/{uuid.uuid4()}/transcripts/{uuid.uuid4()}"

    await backend.upload(token_a, path_a, b"A", "text/plain")

    # User B cannot read, list, or delete A's object.
    with pytest.raises(storage.StorageAuthError):
        await backend.download(token_b, path_a)
    with pytest.raises(storage.StorageAuthError):
        await backend.delete(token_b, path_a)
    with pytest.raises(storage.StorageAuthError):
        await backend.list_prefix(token_b, f"{org_a}/")

    # Anonymous access fails.
    with pytest.raises(storage.StorageAuthError):
        await backend.download("", path_a)

    # User B can operate within their own org.
    await backend.upload(token_b, path_b, b"B", "text/plain")
    data = await backend.download(token_b, path_b)
    content = b"".join([chunk async for chunk in data])
    assert content == b"B"


@pytest.mark.hosted
@pytest.mark.slow
async def test_storage_objects_rls_enforced_for_authenticated(
    superuser_pool: asyncpg.Pool,
) -> None:
    """The actual migration 002 Storage RLS policies restrict the
    `authenticated` role to objects under an org where the JWT user has an active
    membership.
    """
    user_a, org_a, _ = await _seed_member(superuser_pool, "rls-a@airbyte.io")
    user_b, org_b, _ = await _seed_member(superuser_pool, "rls-b@airbyte.io")
    inactive_user = await _seed_user_and_membership(
        superuser_pool, "rls-inactive@airbyte.io", org_a, active=False
    )
    account_a = await _seed_account(superuser_pool, org_a, user_a)

    path_a = f"{org_a}/{account_a}/transcripts/{uuid.uuid4()}"

    # Active org A member can insert and read.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_a)}),
            )
            await conn.execute(
                "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                path_a,
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name = $1", path_a
            )
            assert len(rows) == 1

    # User B (org B) cannot see or write into org A's prefix.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_b)}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_a,
                )

    # After the failed insert rolls back, verify user B sees nothing in org A.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_b)}),
            )
            rows = await conn.fetch(
                "SELECT name FROM storage.objects WHERE name = $1", path_a
            )
            assert rows == []

    # Anonymous JWT (no sub) cannot write.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_a,
                )

    # Inactive member cannot write.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(inactive_user)}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', $1)",
                    path_a,
                )

    # Malformed prefix (non-UUID first segment) cannot be inserted.
    async with superuser_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            await conn.execute("SET LOCAL search_path = 'auth, storage, public'")
            await conn.execute(
                "SELECT set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": str(user_a)}),
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await conn.execute(
                    "INSERT INTO storage.objects (bucket_id, name) VALUES ('transcripts', 'badpath')"
                )


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
async def test_supabase_storage_backend_uses_user_token_and_anon_key(
    app_client: TestClient,
) -> None:
    """The production backend sends the user's bearer token and public anon key;
    it never includes a service-role key.
    """
    from hosted import storage

    backend = storage.SupabaseStorageBackend()
    assert storage.BUCKET == "transcripts"
    headers = backend._headers("user-jwt")
    assert headers["Authorization"] == "Bearer user-jwt"
    assert headers["apikey"] == "anon-key"
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
    assert app_client.get("/api/run").status_code == 404
