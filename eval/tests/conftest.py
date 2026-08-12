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
                    CREATE ROLE authenticated WITH LOGIN;
                END IF;
            END $$;

            CREATE SCHEMA IF NOT EXISTS auth;

            GRANT CONNECT ON DATABASE test TO authenticated;
            GRANT USAGE ON SCHEMA public, storage, auth TO authenticated;
            ALTER ROLE authenticated SET search_path = 'auth, storage, public';

            CREATE OR REPLACE FUNCTION auth.uid()
            RETURNS UUID AS $$
                SELECT (NULLIF(current_setting('request.jwt.claims', true), '')::json->>'sub')::uuid;
            $$ LANGUAGE sql STABLE;
            GRANT EXECUTE ON FUNCTION auth.uid() TO authenticated;
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
        "HOSTED_CONTEXT_SECRET": "test-context-secret-32-bytes!!",
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


@pytest.fixture(scope="session")
def app_client(hosted_env: dict[str, str]) -> Any:
    """Return a TestClient for the hosted app with an in-memory storage backend."""
    # Use the same top-level `hosted` package that app.py imports so the
    # in-memory backend is visible to the transcript routes.
    import hosted
    from hosted import storage

    storage.set_backend(storage.MemoryStorageBackend())
    import webapp.app as app_module

    with TestClient(app_module.app) as client:
        yield client
    storage.clear_backend()
