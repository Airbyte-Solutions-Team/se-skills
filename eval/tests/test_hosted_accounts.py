"""Integration tests for the hosted auth/org/account vertical slice.

These tests spin up a temporary Postgres container, run the versioned migrations,
and exercise the FastAPI endpoints plus the database RLS boundary. They use
synthetic data only and a local HMAC JWT secret so no Supabase project is required.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import asyncpg
import jwt
import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container() -> Any:
    container = PostgresContainer("postgres:15", password="test", dbname="test")
    with container as postgres:
        yield postgres


@pytest.fixture(scope="session")
def db_urls(postgres_container: Any) -> dict[str, str]:
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    # testcontainers/postgres creates a superuser named 'test' with password 'test'.
    migrate = f"postgresql://test:test@{host}:{port}/test"
    return {
        "MIGRATE_DATABASE_URL": migrate,
        "DATABASE_URL": f"postgresql://app_user:app_user_password@{host}:{port}/test",
        "DATABASE_ADMIN_URL": f"postgresql://app_admin:app_admin_password@{host}:{port}/test",
    }


@pytest.fixture(scope="session")
async def hosted_env(db_urls: dict[str, str]) -> dict[str, str]:
    """Set hosted environment variables and run migrations once."""
    env = {
        "HOSTED_MODE": "1",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key",
        "HOSTED_JWT_ALGORITHM": "HS256",
        "HOSTED_JWT_SECRET": "super-secret-32-byte-test-jwt-key!",
        "BETA_ALLOWED_EMAILS": "test@airbyte.io,other@airbyte.io",
        **db_urls,
    }
    for key, value in env.items():
        os.environ[key] = value

    # Drop cached hosted modules so they re-import with the new config.
    modules_to_drop = [
        name
        for name in list(sys.modules)
        if name in ("app", "webapp.app")
        or name.startswith("webapp.hosted")
        or name.startswith("hosted")
    ]
    for name in modules_to_drop:
        del sys.modules[name]

    from webapp.hosted import config, migrations

    await migrations.migrate(
        env["MIGRATE_DATABASE_URL"],
        config.MIGRATIONS_DIR,
        app_user_password="app_user_password",
        app_admin_password="app_admin_password",
    )
    return env


@pytest.fixture(scope="session")
def app_client(hosted_env: dict[str, str]) -> Any:
    import webapp.app as app_module

    # `hosted_env` already reloaded `webapp.hosted.config` with the test
    # container URLs, so importing `webapp.app` here picks up hosted mode.
    with TestClient(app_module.app) as client:
        yield client


@pytest.fixture
async def admin_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    pool = await asyncpg.create_pool(hosted_env["DATABASE_ADMIN_URL"], min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def user_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    pool = await asyncpg.create_pool(hosted_env["DATABASE_URL"], min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_member(
    admin_pool: asyncpg.Pool,
    email: str,
    *,
    org_name: str = "Airbyte",
    org_slug: str = "airbyte",
    active: bool = True,
    role: str = "member",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with admin_pool.acquire() as conn:
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.users (id, email) VALUES ($1, $2)",
            user_id,
            email,
        )
        await conn.execute(
            "INSERT INTO public.organizations (id, name, slug) VALUES ($1, $2, $3)",
            org_id,
            org_name,
            org_slug,
        )
        membership_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO public.memberships (id, org_id, user_id, role, active)
            VALUES ($1, $2, $3, $4, $5)
            """,
            membership_id,
            org_id,
            user_id,
            role,
            active,
        )
        return user_id, org_id, membership_id


async def _seed_user_and_membership(
    admin_pool: asyncpg.Pool,
    email: str,
    org_id: uuid.UUID,
    active: bool = True,
) -> uuid.UUID:
    """Create a user and membership in an existing org."""
    async with admin_pool.acquire() as conn:
        user_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO public.users (id, email) VALUES ($1, $2)",
            user_id,
            email,
        )
        await conn.execute(
            """
            INSERT INTO public.memberships (id, org_id, user_id, role, active)
            VALUES ($1, $2, $3, $4, $5)
            """,
            uuid.uuid4(),
            org_id,
            user_id,
            "member",
            active,
        )
        return user_id


def _token(user_id: uuid.UUID, email: str) -> str:
    now = datetime.now(timezone.utc).timestamp()
    return jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "aud": "authenticated",
            "iat": now,
            "exp": now + 3600,
        },
        os.environ["HOSTED_JWT_SECRET"],
        algorithm="HS256",
    )


def _auth_header(user_id: uuid.UUID, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user_id, email)}"}


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
                    "SELECT set_config('app.current_org_id', $1, true)",
                    str(org_b),
                )
                await conn.execute(
                    "SELECT set_config('app.current_user_id', $1, true)",
                    str(user_b),
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
                    "SELECT set_config('app.current_org_id', $1, true)",
                    str(org_b),
                )
                await conn.execute(
                    "SELECT set_config('app.current_user_id', $1, true)",
                    str(user_a),
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
                    "SELECT set_config('app.current_org_id', $1, true)",
                    str(org_b),
                )
                await conn.execute(
                    "SELECT set_config('app.current_user_id', $1, true)",
                    str(user_a),
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
                    "SELECT set_config('app.current_org_id', $1, true)",
                    str(org_b),
                )
                await conn.execute(
                    "SELECT set_config('app.current_user_id', $1, true)",
                    str(user_b),
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
        )

    with pytest.raises(ValueError):
        await migrations.migrate(
            hosted_env["MIGRATE_DATABASE_URL"],
            config.MIGRATIONS_DIR,
            app_user_password="app_user_password",
            app_admin_password="",
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
        # Restore the global app_user password for the shared container.
        restore_conn = await asyncpg.connect(hosted_env["MIGRATE_DATABASE_URL"])
        try:
            await restore_conn.execute(
                f"ALTER ROLE app_user WITH PASSWORD 'app_user_password'"
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
