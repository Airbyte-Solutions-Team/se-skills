"""Shared session fixtures for hosted integration tests."""
from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator
from typing import Any

import asyncpg
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
    migrate = f"postgresql://test:test@{host}:{port}/test"
    return {
        "MIGRATE_DATABASE_URL": migrate,
        "DATABASE_URL": f"postgresql://app_user:app_user_password@{host}:{port}/test",
        "DATABASE_ADMIN_URL": f"postgresql://app_admin:app_admin_password@{host}:{port}/test",
        "DATABASE_WORKER_URL": f"postgresql://app_worker:app_worker_password@{host}:{port}/test",
        "AUTHENTICATED_DATABASE_URL": f"postgresql://authenticated:authenticated_password@{host}:{port}/test",
        "AUTHENTICATOR_DATABASE_URL": f"postgresql://authenticator:authenticator_password@{host}:{port}/test",
    }


async def _ensure_storage_schema(admin_dsn: str) -> None:
    """Create a minimal Supabase Storage-compatible schema for migration 002.

    This lets the guarded storage SQL in migration 002 run in a plain
    testcontainers Postgres instance, so the Storage RLS policies can be
    exercised as the `authenticated` role in tests.
    """
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            """
            -- pgcrypto is also created by migration 001; make it available here
            -- for the default uuid values below.
            CREATE EXTENSION IF NOT EXISTS pgcrypto;

            CREATE SCHEMA IF NOT EXISTS storage;
            CREATE TABLE IF NOT EXISTS storage.buckets (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE,
                public BOOLEAN,
                owner UUID
            );
            CREATE TABLE IF NOT EXISTS storage.objects (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                bucket_id TEXT,
                name TEXT,
                owner UUID,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_objects_bucket ON storage.objects(bucket_id);

            CREATE OR REPLACE FUNCTION storage.foldername(name TEXT)
            RETURNS TEXT[] AS $$
                SELECT string_to_array(name, '/');
            $$ LANGUAGE sql IMMUTABLE;

            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                    CREATE ROLE authenticated WITH LOGIN NOINHERIT PASSWORD 'authenticated_password';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN
                    CREATE ROLE authenticator WITH LOGIN NOINHERIT PASSWORD 'authenticator_password';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_storage') THEN
                    CREATE ROLE app_storage NOLOGIN NOBYPASSRLS NOINHERIT;
                END IF;
                -- Supabase's authenticator role must be able to switch to app_storage
                -- based on the JWT role claim, even in the test shim.
                GRANT app_storage TO authenticator;
            END $$;

            CREATE SCHEMA IF NOT EXISTS auth;

            GRANT CONNECT ON DATABASE test TO authenticated, app_storage, authenticator;
            GRANT USAGE ON SCHEMA public, storage, auth TO authenticated, app_storage, authenticator;
            ALTER ROLE authenticated SET search_path = 'auth, storage, public';
            ALTER ROLE authenticator SET search_path = 'auth, storage, public';

            CREATE OR REPLACE FUNCTION auth.uid()
            RETURNS UUID AS $$
                SELECT (NULLIF(current_setting('request.jwt.claims', true), '')::json->>'sub')::uuid;
            $$ LANGUAGE sql STABLE;
            GRANT EXECUTE ON FUNCTION auth.uid() TO authenticated, app_storage;
            """
        )
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def hosted_env(db_urls: dict[str, str]) -> dict[str, str]:
    """Set hosted environment variables and run migrations once."""
    env = {
        "HOSTED_MODE": "1",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key",
        "HOSTED_JWT_ALGORITHM": "HS256",
        "HOSTED_JWT_SECRET": "super-secret-32-byte-test-jwt-key!",
        "SUPABASE_JWT_SECRET": "super-secret-32-byte-test-storage-jwt-key!",
        "HOSTED_CONTEXT_SECRET": "test-context-secret-32-bytes!!",
        "APP_WORKER_PASSWORD": "app_worker_password",
        "BETA_ALLOWED_EMAILS": "test@airbyte.io,other@airbyte.io",
        **db_urls,
    }
    for key, value in env.items():
        os.environ[key] = value

    modules_to_drop = [
        name
        for name in list(sys.modules)
        if name in ("app", "webapp.app")
        or name.startswith("webapp.hosted")
        or name.startswith("hosted")
    ]
    for name in modules_to_drop:
        del sys.modules[name]

    import hosted
    import hosted.migrations as migrations

    # Create the storage/auth schema before migrations so the guarded Supabase
    # Storage SQL in migration 002 is exercised in testcontainers.
    await _ensure_storage_schema(env["MIGRATE_DATABASE_URL"])

    await migrations.migrate(
        env["MIGRATE_DATABASE_URL"],
        hosted.config.MIGRATIONS_DIR,
        app_user_password="app_user_password",
        app_admin_password="app_admin_password",
        app_worker_password="app_worker_password",
        context_secret=env["HOSTED_CONTEXT_SECRET"],
    )
    return env


@pytest.fixture
async def admin_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    """Function-scoped admin pool (separate loop from the app under TestClient)."""
    pool = await asyncpg.create_pool(hosted_env["DATABASE_ADMIN_URL"], min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def user_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    """Function-scoped app_user pool (separate loop from the app under TestClient)."""
    pool = await asyncpg.create_pool(hosted_env["DATABASE_URL"], min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def superuser_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    """Function-scoped superuser pool for role-impersonation tests."""
    pool = await asyncpg.create_pool(hosted_env["MIGRATE_DATABASE_URL"], min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def authenticator_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    """Function-scoped pool connected as the Supabase authenticator role.

    This role is the entry point for JWT-driven role switching; it must be able
    to `SET ROLE app_storage` but not privileged roles such as `app_admin`.
    """
    pool = await asyncpg.create_pool(
        hosted_env["AUTHENTICATOR_DATABASE_URL"], min_size=1, max_size=2
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def authenticated_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    """Function-scoped pool connected as the browser-visible authenticated role.

    This role must have no direct Storage object privileges and must not be able
    to switch to app_storage.
    """
    pool = await asyncpg.create_pool(
        hosted_env["AUTHENTICATED_DATABASE_URL"], min_size=1, max_size=2
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def worker_pool(hosted_env: dict[str, str]) -> AsyncGenerator[asyncpg.Pool, None]:
    """Function-scoped pool connected as the least-privilege worker role."""
    pool = await asyncpg.create_pool(
        hosted_env["DATABASE_WORKER_URL"], min_size=1, max_size=2
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(autouse=True)
def _hosted_app_mode(request: pytest.FixtureRequest) -> None:
    """Ensure `webapp.app` is imported for the correct mode before any test.

    Hosted tests use a freshly imported `webapp.app` with `HOSTED_MODE=1` so the
    session-scoped app fixture and the tests patch the same module objects. Non-hosted
    tests import a local-mode `webapp.app` so filesystem-backed routes are present.

    `hosted_env` (and the Docker/Postgres testcontainer it spins up) is only resolved
    for tests actually marked `hosted` — resolving it unconditionally here would force
    every test in this directory, including plain unit tests, to require Docker.
    """
    hosted_marker = request.node.get_closest_marker("hosted")
    if hosted_marker:
        request.getfixturevalue("hosted_env")
        os.environ["HOSTED_MODE"] = "1"
        modules_to_drop = [
            name
            for name in list(sys.modules)
            if name in ("app", "webapp.app")
            or name.startswith("hosted")
            or name.startswith("webapp.hosted")
        ]
    else:
        os.environ.pop("HOSTED_MODE", None)
        modules_to_drop = [
            name
            for name in list(sys.modules)
            if name in ("app", "webapp.app")
            or name.startswith("hosted")
            or name.startswith("webapp.hosted")
        ]
    for name in modules_to_drop:
        if name in sys.modules:
            del sys.modules[name]
    if hosted_marker:
        # Leave the actual import to `app_client` so it can install the in-memory
        # Storage backend before `webapp.app` registers hosted routers.
        return
    # For non-hosted tests, preload the local app so `test_ask_routes` etc. can
    # import `webapp.app` without seeing a stale hosted module.
    import webapp.app  # noqa: F401


@pytest.fixture
def app_client(hosted_env: dict[str, str], _hosted_app_mode: None) -> Any:
    """Return a function-scoped TestClient for the hosted app.

    The in-memory Storage backend is installed before `webapp.app` is imported so
    the hosted transcript routes see the test backend. The backend is cleared after
    the test.
    """
    import hosted
    from hosted import storage

    storage.set_backend(storage.MemoryStorageBackend())
    import webapp.app as app_module

    with TestClient(app_module.app) as client:
        yield client
    storage.clear_backend()
