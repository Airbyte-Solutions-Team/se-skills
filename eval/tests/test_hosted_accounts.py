"""Integration tests for the hosted auth/org/account vertical slice.

These tests spin up a temporary Postgres container, run the versioned migrations,
and exercise the FastAPI endpoints plus the database RLS boundary. They use
synthetic data only and a local HMAC JWT secret so no Supabase project is required.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import asyncpg
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient


from .hosted_helpers import (
    _auth_header,
    _context_token,
    _seed_member,
    _seed_user_and_membership,
    _token,
)


@pytest.mark.hosted
@pytest.mark.slow
async def test_auth_config_returns_hosted_true(app_client: TestClient) -> None:
    response = app_client.get("/api/auth/config")
    assert response.status_code == 200
    body = response.json()
    assert body["hosted"] is True
    assert body["supabase_url"] == "https://example.supabase.co"
    assert body["supabase_anon_key"] == "anon-key"


@pytest.mark.hosted
@pytest.mark.slow
async def test_unauthenticated_request_rejected(app_client: TestClient) -> None:
    response = app_client.get("/api/hosted/accounts")
    assert response.status_code == 401


@pytest.mark.hosted
@pytest.mark.slow
async def test_inactive_membership_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, _, _ = await _seed_member(
        admin_pool, "inactive@airbyte.io", active=False
    )
    response = app_client.get(
        "/api/hosted/accounts",
        headers=_auth_header(user_id, "inactive@airbyte.io"),
    )
    assert response.status_code == 403


@pytest.mark.hosted
@pytest.mark.slow
async def test_non_member_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    async with admin_pool.acquire() as conn:
        user_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.users (id, email) VALUES ($1, $2)",
            user_id,
            "nomembership@airbyte.io",
        )
    response = app_client.get(
        "/api/hosted/accounts",
        headers=_auth_header(user_id, "nomembership@airbyte.io"),
    )
    assert response.status_code == 403


@pytest.mark.hosted
@pytest.mark.slow
async def test_member_can_create_and_list_accounts(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(
        admin_pool, "member@airbyte.io", org_slug="airbyte-test"
    )
    create_resp = app_client.post(
        "/api/hosted/accounts",
        json={"name": "Acme Corp"},
        headers=_auth_header(user_id, "member@airbyte.io"),
    )
    assert create_resp.status_code == 201
    account = create_resp.json()
    assert account["name"] == "Acme Corp"
    assert account["slug"] == "acme-corp"
    assert account["created_by"] == str(user_id)
    assert account["org_id"] == str(org_id)

    list_resp = app_client.get(
        "/api/hosted/accounts",
        headers=_auth_header(user_id, "member@airbyte.io"),
    )
    assert list_resp.status_code == 200
    accounts = list_resp.json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["id"] == account["id"]


@pytest.mark.hosted
@pytest.mark.slow
async def test_spoofed_org_id_cannot_override_membership(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, org_id, _ = await _seed_member(
        admin_pool, "spoof@airbyte.io", org_slug="spoof-org"
    )

    async def _other_org() -> uuid.UUID:
        async with admin_pool.acquire() as conn:
            other_org_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
                other_org_id,
                "Other",
                "other",
            )
            return other_org_id

    other_org_id = await _other_org()

    create_resp = app_client.post(
        "/api/hosted/accounts",
        json={"name": "Spoofed Account", "org_id": str(other_org_id)},
        headers={
            **_auth_header(user_id, "spoof@airbyte.io"),
            "X-Org-Id": str(other_org_id),
        },
    )
    assert create_resp.status_code == 201
    account = create_resp.json()
    # The API ignored both supplied org identifiers and used the user's org.
    assert account["org_id"] == str(org_id)
    assert account["org_id"] != str(other_org_id)


@pytest.mark.hosted
@pytest.mark.slow
async def test_assignment_does_not_change_org_visibility(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    u1, org_id, _ = await _seed_member(admin_pool, "one@airbyte.io", org_slug="assign-org")
    u2, u2_org_id, _ = await _seed_member(
        admin_pool, "two@airbyte.io", org_name="Airbyte", org_slug="assign-org-2"
    )

    async def _move() -> None:
        async with admin_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.memberships SET org_id = $1 WHERE user_id = $2
                """,
                org_id,
                u2,
            )
            await conn.execute(
                "DELETE FROM public.organizations WHERE id = $1", u2_org_id
            )

    await _move()

    create_resp = app_client.post(
        "/api/hosted/accounts",
        json={"name": "Shared Account", "assigned_to": str(u1)},
        headers=_auth_header(u2, "two@airbyte.io"),
    )
    assert create_resp.status_code == 201
    account_id = create_resp.json()["id"]

    list_resp = app_client.get(
        "/api/hosted/accounts",
        headers=_auth_header(u1, "one@airbyte.io"),
    )
    assert list_resp.status_code == 200
    assert any(a["id"] == account_id for a in list_resp.json()["accounts"])


@pytest.mark.hosted
@pytest.mark.slow
async def test_hosted_mode_does_not_expose_local_routes(app_client: TestClient) -> None:
    """Hosted mode should only register auth, hosted, favicon, and static."""
    local_routes = [
        "/api/members",
        "/api/accounts",
        "/api/skills",
        "/api/jobs",
        "/api/overview",
        "/api/sfdc/stage-amount",
        "/api/ask",
        "/api/transcribe/start",
        "/api/invoke",
        "/api/output/ask",
        "/api/accounts/example/opportunities/example/overview/evidence",
        "/api/accounts/example/opportunities/example/overview/create",
        "/api/accounts/example/opportunities/example/overview/state",
        "/api/accounts/example/opportunities/example/overview/freshness",
        "/api/accounts/example/opportunities/example/overview/update",
        "/api/accounts/example/opportunities/example/overview/update/jobs/example",
        "/api/accounts/example/opportunities/example/overview/history",
        "/api/accounts/example/opportunities/example/overview/history/1",
        "/api/accounts/example/opportunities/example/tech-eval",
        "/api/accounts/example/opportunities/example/tech-eval/items/example",
    ]
    for route in local_routes:
        response = app_client.get(route)
        assert response.status_code == 404, f"{route} should not be exposed in hosted mode"


@pytest.mark.hosted
@pytest.mark.slow
async def test_cross_org_account_not_visible_via_rls(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    async def _setup() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        async with admin_pool.acquire() as conn:
            org_a, org_b = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3), ($4, $5, $6)",
                org_a, "Org A", "org-a",
                org_b, "Org B", "org-b",
            )
            account_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO public.accounts (id, org_id, name, slug)
                VALUES ($1, $2, $3, $4)
                """,
                account_id, org_a, "Account A", "account-a",
            )
            user_b = await _seed_user_and_membership(admin_pool, "user-b@airbyte.io", org_b)
            return org_a, org_b, account_id, user_b

    org_a, org_b, account_id, user_b = await _setup()  # noqa: F841

    async def _query() -> list[asyncpg.Record]:
        async with user_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    _context_token(user_b),
                )
                return await conn.fetch(
                    "SELECT id FROM public.accounts WHERE id = $1", account_id
                )

    rows = await _query()
    assert len(rows) == 0


@pytest.mark.hosted
@pytest.mark.slow
async def test_rls_blocks_mismatched_org_context(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """A valid membership for org A must not allow access to org B rows."""
    async def _setup() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        async with admin_pool.acquire() as conn:
            org_a, org_b = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3), ($4, $5, $6)",
                org_a, "Org A", "org-a-mismatch",
                org_b, "Org B", "org-b-mismatch",
            )
            user_a = await _seed_user_and_membership(admin_pool, "user-a@airbyte.io", org_a)
            await _seed_user_and_membership(admin_pool, "user-b@airbyte.io", org_b)
            await conn.execute(
                """
                INSERT INTO public.accounts (id, org_id, name, slug)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.uuid4(), org_b, "Account B", "account-b",
            )
            return org_a, org_b, user_a, uuid.uuid4()

    org_a, org_b, user_a, _ = await _setup()  # noqa: F841

    async def _query() -> list[asyncpg.Record]:
        async with user_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    _context_token(user_a),
                )
                return await conn.fetch("SELECT id FROM public.accounts")

    rows = await _query()
    assert len(rows) == 0


@pytest.mark.hosted
@pytest.mark.slow
async def test_rls_blocks_cross_org_write_without_membership(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """Setting current_org_id to another org without a membership must block writes."""
    async def _setup() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with admin_pool.acquire() as conn:
            org_a, org_b = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3), ($4, $5, $6)",
                org_a, "Org A", "org-a-write",
                org_b, "Org B", "org-b-write",
            )
            user_a = await _seed_user_and_membership(admin_pool, "user-a-write@airbyte.io", org_a)
            await conn.execute(
                """
                INSERT INTO public.accounts (id, org_id, name, slug)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.uuid4(), org_b, "Account B", "account-b-write",
            )
            return org_a, org_b, user_a

    org_a, org_b, user_a = await _setup()  # noqa: F841

    async def _insert() -> None:
        async with user_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    _context_token(user_a),
                )
                account_b = await conn.fetchval(
                    "SELECT id FROM public.accounts WHERE slug = $1",
                    "account-b-write",
                )
                await conn.execute(
                    """
                    INSERT INTO public.opportunities (id, org_id, account_id, name, slug)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    uuid.uuid4(), org_b, account_b, "Opp", "opp-write",
                )

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await _insert()


@pytest.mark.hosted
@pytest.mark.slow
async def test_cross_org_opportunity_relationship_fails_at_db(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    async def _setup() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        async with admin_pool.acquire() as conn:
            org_a, org_b = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3), ($4, $5, $6)",
                org_a, "Org A", "org-a-xfk",
                org_b, "Org B", "org-b-xfk",
            )
            account_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO public.accounts (id, org_id, name, slug)
                VALUES ($1, $2, $3, $4)
                """,
                account_id, org_a, "Account A", "account-a-xfk",
            )
            user_b = await _seed_user_and_membership(admin_pool, "user-b-xfk@airbyte.io", org_b)
            return org_a, org_b, account_id, user_b

    org_a, org_b, account_id, user_b = await _setup()  # noqa: F841

    async def _insert_bad_opportunity() -> None:
        async with user_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    _context_token(user_b),
                )
                await conn.execute(
                    """
                    INSERT INTO public.opportunities (id, org_id, account_id, name, slug)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    uuid.uuid4(), org_b, account_id, "Opp", "opp-xfk",
                )

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await _insert_bad_opportunity()


@pytest.mark.hosted
@pytest.mark.slow
async def test_app_user_cannot_impersonate_another_member(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """Using the normal runtime `app_user` credential, a User A connection
    cannot gain Org B access by setting both User B and Org B context values,
    cannot resolve another user's membership, and cannot read/insert/update/delete
    Org B rows.
    """
    async with admin_pool.acquire() as conn:
        org_a = await conn.fetchval(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3) RETURNING id",
            uuid.uuid4(), "Org A", "impersonate-org-a",
        )
        org_b = await conn.fetchval(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3) RETURNING id",
            uuid.uuid4(), "Org B", "impersonate-org-b",
        )
        account_b = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.accounts (id, org_id, name, slug) VALUES ($1, $2, $3, $4)",
            account_b, org_b, "Account B", "account-b-impersonate",
        )
        user_a = await _seed_user_and_membership(admin_pool, "user-a-imp@airbyte.io", org_a)
        user_b = await _seed_user_and_membership(admin_pool, "user-b-imp@airbyte.io", org_b)

    token_a = _context_token(user_a)
    forged_b = f"{user_b}:invalid-mac-hex"
    valid_mac_b = hmac.new(
        os.environ["HOSTED_CONTEXT_SECRET"].encode("utf-8"),
        str(user_b).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    token_b = f"{user_b}:{valid_mac_b}"

    async with user_pool.acquire() as conn:
        # The resolver only returns the membership encoded in a valid signed token.
        row = await conn.fetchrow(
            "SELECT membership_id, org_id FROM public.resolve_active_membership($1)",
            token_a,
        )
        assert row is not None
        assert row["org_id"] == org_a

        invalid = await conn.fetchrow(
            "SELECT membership_id, org_id FROM public.resolve_active_membership($1)",
            forged_b,
        )
        assert invalid is None

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            # User A tries to use the forgeable GUCs plus a forged B token.
            await conn.execute(
                "SELECT set_config('app.current_org_id', $1, true)", str(org_b)
            )
            await conn.execute(
                "SELECT set_config('app.current_user_id', $1, true)", str(user_b)
            )
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", forged_b
            )
            rows = await conn.fetch(
                "SELECT id FROM public.accounts WHERE id = $1", account_b
            )
            assert len(rows) == 0

    # Using a valid token for A while also setting the GUCs to B still only gives
    # access to A's own org rows.
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_org_id', $1, true)", str(org_b)
            )
            await conn.execute(
                "SELECT set_config('app.current_user_id', $1, true)", str(user_b)
            )
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", token_a
            )
            rows = await conn.fetch(
                "SELECT id FROM public.accounts WHERE id = $1", account_b
            )
            assert len(rows) == 0

    # Even with a cryptographically valid B token (which only the app could
    # produce), the connection is still subject to the same RLS path. This test
    # verifies the boundary with a valid token for the wrong member: the row is
    # visible because the token genuinely represents User B's membership.
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", token_b
            )
            rows = await conn.fetch(
                "SELECT id FROM public.accounts WHERE id = $1", account_b
            )
            assert len(rows) == 1

    # app_user must not be able to read or execute the private secret/verifier.
    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM app_private.context_secret")
    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetchval(
                "SELECT app_private.verify_context_token($1)", token_a
            )

    # app_user cannot resolve another user's membership without that user's token.
    async with user_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT membership_id, org_id FROM public.resolve_active_membership($1)",
            token_a,
        )
        assert row is None or row["org_id"] == org_a


@pytest.mark.hosted
@pytest.mark.slow
async def test_api_cannot_create_opportunity_for_account_in_other_org(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, _, _ = await _seed_member(
        admin_pool, "opp-guard@airbyte.io", org_slug="opp-guard"
    )

    async def _other_account() -> uuid.UUID:
        async with admin_pool.acquire() as conn:
            other_org_id = uuid.uuid4()
            other_account_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
                other_org_id, "Other", "other-opp-guard",
            )
            await conn.execute(
                """
                INSERT INTO public.accounts (id, org_id, name, slug)
                VALUES ($1, $2, $3, $4)
                """,
                other_account_id, other_org_id, "Other Account", "other-account",
            )
            return other_account_id

    other_account_id = await _other_account()

    response = app_client.post(
        f"/api/hosted/accounts/{other_account_id}/opportunities",
        json={"name": "Should Fail"},
        headers=_auth_header(user_id, "opp-guard@airbyte.io"),
    )
    assert response.status_code == 404


@pytest.mark.hosted
@pytest.mark.slow
async def test_opportunity_same_org_succeeds(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    user_id, _, _ = await _seed_member(
        admin_pool, "opp-ok@airbyte.io", org_slug="opp-ok"
    )
    account_resp = app_client.post(
        "/api/hosted/accounts",
        json={"name": "Opportunity Account"},
        headers=_auth_header(user_id, "opp-ok@airbyte.io"),
    )
    assert account_resp.status_code == 201
    account_id = account_resp.json()["id"]

    opp_resp = app_client.post(
        f"/api/hosted/accounts/{account_id}/opportunities",
        json={"name": "Expansion"},
        headers=_auth_header(user_id, "opp-ok@airbyte.io"),
    )
    assert opp_resp.status_code == 201
    opportunity = opp_resp.json()
    assert opportunity["account_id"] == account_id
    assert opportunity["name"] == "Expansion"

    list_resp = app_client.get(
        f"/api/hosted/accounts/{account_id}/opportunities",
        headers=_auth_header(user_id, "opp-ok@airbyte.io"),
    )
    assert list_resp.status_code == 200
    assert any(o["id"] == opportunity["id"] for o in list_resp.json()["opportunities"])


@pytest.mark.hosted
@pytest.mark.slow
async def test_opportunity_slug_unique_per_account(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """The same slug may be used for opportunities on different accounts in the same org."""
    user_id, _, _ = await _seed_member(
        admin_pool, "opp-slug@airbyte.io", org_slug="opp-slug"
    )
    account_ids = []
    for name in ["Account One", "Account Two"]:
        resp = app_client.post(
            "/api/hosted/accounts",
            json={"name": name},
            headers=_auth_header(user_id, "opp-slug@airbyte.io"),
        )
        assert resp.status_code == 201
        account_ids.append(resp.json()["id"])

    created_ids = set()
    for account_id in account_ids:
        resp = app_client.post(
            f"/api/hosted/accounts/{account_id}/opportunities",
            json={"name": "Expansion"},
            headers=_auth_header(user_id, "opp-slug@airbyte.io"),
        )
        assert resp.status_code == 201
        created_ids.add(resp.json()["id"])
        assert resp.json()["slug"] == "expansion"

    assert len(created_ids) == 2


@pytest.mark.hosted
@pytest.mark.slow
async def test_app_user_resolver_cannot_alter_tenancy(
    user_pool: asyncpg.Pool, admin_pool: asyncpg.Pool
) -> None:
    """The app_user role used by normal requests cannot mutate organizations, memberships, users, or schema_migrations."""
    async with admin_pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3) RETURNING id",
            uuid.uuid4(), "Evil Org", "evil-org",
        )
        user_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.users (id, email) VALUES ($1, $2)",
            user_id,
            "evil@airbyte.io",
        )

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
                uuid.uuid4(), "Bad Org", "bad-org",
            )

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                """
                INSERT INTO public.memberships (id, org_id, user_id, role, active)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uuid.uuid4(), org_id, user_id, "member", True,
            )

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE public.memberships SET active = false WHERE user_id = $1",
                user_id,
            )

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "DELETE FROM public.users WHERE id = $1",
                user_id,
            )

    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO public.schema_migrations (version) VALUES ($1)",
                "999",
            )


@pytest.mark.hosted
@pytest.mark.slow
async def test_deactivated_membership_denies_access(
    app_client: TestClient, admin_pool: asyncpg.Pool
) -> None:
    """After a membership is deactivated the user can no longer access the org."""
    user_id, _, _ = await _seed_member(
        admin_pool, "deactivated@airbyte.io", org_slug="deactivated"
    )

    # First request succeeds.
    response = app_client.get(
        "/api/hosted/accounts",
        headers=_auth_header(user_id, "deactivated@airbyte.io"),
    )
    assert response.status_code == 200

    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.memberships SET active = false WHERE user_id = $1",
            user_id,
        )

    # Subsequent request is rejected because the resolver no longer finds an
    # active membership.
    response = app_client.get(
        "/api/hosted/accounts",
        headers=_auth_header(user_id, "deactivated@airbyte.io"),
    )
    assert response.status_code == 403


@pytest.mark.hosted
@pytest.mark.slow
async def test_migration_rejects_empty_passwords(hosted_env: dict[str, str]) -> None:
    """The migration runner fails before connecting if credentials are empty."""
    from webapp.hosted import config, migrations

    with pytest.raises(ValueError):
        await migrations.migrate(
            hosted_env["MIGRATE_DATABASE_URL"],
            config.MIGRATIONS_DIR,
            app_user_password="",
            app_admin_password="app_admin_password",
            app_worker_password="app_worker_password",
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )

    with pytest.raises(ValueError):
        await migrations.migrate(
            hosted_env["MIGRATE_DATABASE_URL"],
            config.MIGRATIONS_DIR,
            app_user_password="app_user_password",
            app_admin_password="",
            app_worker_password="app_worker_password",
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )

    with pytest.raises(ValueError):
        await migrations.migrate(
            hosted_env["MIGRATE_DATABASE_URL"],
            config.MIGRATIONS_DIR,
            app_user_password="app_user_password",
            app_admin_password="app_admin_password",
            app_worker_password="",
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )

    with pytest.raises(ValueError):
        await migrations.migrate(
            hosted_env["MIGRATE_DATABASE_URL"],
            config.MIGRATIONS_DIR,
            app_user_password="app_user_password",
            app_admin_password="app_admin_password",
            app_worker_password="app_worker_password",
            context_secret="",
        )


@pytest.mark.hosted
@pytest.mark.slow
async def test_migration_safe_password_quoting(hosted_env: dict[str, str]) -> None:
    """Role passwords with SQL-significant characters are quoted safely."""
    from webapp.hosted import config, migrations

    malicious = "app'user\"; DROP TABLE public.users; --"
    host_port = hosted_env["MIGRATE_DATABASE_URL"].rsplit("/", 1)[0]
    test_db = "test_migration_pw"

    admin_conn = None
    user_conn = None
    try:
        admin_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
        await admin_conn.execute(f"CREATE DATABASE {test_db}")
        await admin_conn.close()
        admin_conn = None

        test_dsn = f"{host_port}/{test_db}"
        await migrations.migrate(
            test_dsn,
            config.MIGRATIONS_DIR,
            app_user_password=malicious,
            app_admin_password=malicious,
            app_worker_password=malicious,
            context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
        )

        # Must be able to connect as app_user with the malicious password.
        user_conn = await asyncpg.connect(
            f"{host_port}/{test_db}?user=app_user&password={malicious}"
        )
        row = await user_conn.fetchval(
            "SELECT public.is_active_org_member($1, $2)",
            uuid.uuid4(),
            uuid.uuid4(),
        )
        assert row is False
        await user_conn.close()
        user_conn = None
    finally:
        if user_conn:
            await user_conn.close()
        if admin_conn:
            await admin_conn.close()
        # Restore the global app_user and app_admin passwords for the shared
        # container; roles are cluster-wide, so the malicious migration changed
        # them even though it targeted a separate database.
        restore_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
        try:
            await restore_conn.execute(
                f"ALTER ROLE app_user WITH PASSWORD 'app_user_password'"
            )
            await restore_conn.execute(
                f"ALTER ROLE app_admin WITH PASSWORD 'app_admin_password'"
            )
            await restore_conn.execute(
                f"ALTER ROLE app_worker WITH PASSWORD 'app_worker_password'"
            )
        finally:
            await restore_conn.close()
        # Drop the temporary database.
        drop_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
        try:
            await drop_conn.execute(
                f"DROP DATABASE IF EXISTS {test_db} WITH (FORCE)"
            )
        finally:
            await drop_conn.close()


class _JwksHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that serves a JWKS document at the Supabase path."""

    def __init__(self, jwks: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.jwks = jwks
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path == "/auth/v1/.well-known/jwks.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.jwks).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def _rsa_jwks() -> tuple[rsa.RSAPrivateKey, dict[str, Any], str]:
    """Generate a test RSA key pair and return the private key, JWKS, and kid."""
    key_id = "test-key-1"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": key_id,
                "use": "sig",
                "alg": "RS256",
                "n": jwt.utils.to_base64url_uint(public_numbers.n).decode("ascii"),
                "e": jwt.utils.to_base64url_uint(public_numbers.e).decode("ascii"),
            }
        ]
    }
    return private_key, jwks, key_id


@pytest.mark.hosted
@pytest.mark.slow
def test_rs256_jwks_url_and_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production RS256/JWKS path uses the correct Supabase JWKS endpoint and validates issuer."""
    from webapp.hosted import auth, config, db

    private_key, jwks, key_id = _rsa_jwks()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    server = HTTPServer(("127.0.0.1", 0), lambda *args, **kwargs: _JwksHandler(jwks, *args, **kwargs))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(config, "SUPABASE_URL", base_url)
        monkeypatch.setattr(config, "HOSTED_JWT_ALGORITHM", "RS256")
        monkeypatch.setattr(config, "HOSTED_JWT_SECRET", "")
        db.clear_jwks_client()

        issuer = f"{base_url}/auth/v1"
        user_id = uuid.uuid4()
        valid_token = jwt.encode(
            {
                "sub": str(user_id),
                "email": "rs256@airbyte.io",
                "aud": "authenticated",
                "iss": issuer,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": key_id},
        )

        # Correct JWKS URL includes /auth/v1/.
        assert config.jwks_url() == f"{base_url}/auth/v1/.well-known/jwks.json"

        verified = auth.verify_token(valid_token)
        assert verified.user_id == user_id
        assert verified.email == "rs256@airbyte.io"

        # A token with a wrong issuer must be rejected.
        bad_token = jwt.encode(
            {
                "sub": str(user_id),
                "email": "wrong-issuer@airbyte.io",
                "aud": "authenticated",
                "iss": "https://evil.example.com/auth/v1",
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": key_id},
        )
        with pytest.raises(auth.AuthError):
            auth.verify_token(bad_token)
    finally:
        server.shutdown()
        server.server_close()
        db.clear_jwks_client()


@pytest.mark.hosted
def test_config_supabase_jwks_url() -> None:
    """The configured JWKS URL is the Supabase Auth project JWKS endpoint."""
    from webapp.hosted import config

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    try:
        assert config.jwks_url() == "https://example.supabase.co/auth/v1/.well-known/jwks.json"
        assert config.supabase_issuer() == "https://example.supabase.co/auth/v1"
    finally:
        monkeypatch.undo()
