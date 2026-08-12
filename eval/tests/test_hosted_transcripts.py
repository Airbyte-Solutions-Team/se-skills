"""Integration tests for the hosted transcript upload/storage vertical slice."""
from __future__ import annotations

import uuid
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
    data, _ = await backend.download(token_b, path_b)
    assert data == b"B"


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
async def test_hosted_mode_does_not_expose_local_routes(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(admin_pool, "routes@airbyte.io")
    assert app_client.get("/api/skills", headers=_auth_header(user_id, "routes@airbyte.io")).status_code == 404
    assert app_client.get("/api/transcribe/start").status_code == 404
    assert app_client.get("/api/run").status_code == 404
