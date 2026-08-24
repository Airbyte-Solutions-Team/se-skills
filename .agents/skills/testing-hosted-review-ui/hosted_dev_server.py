"""Local hosted-mode dev server for review/export browser acceptance testing.

Boot against a Docker Postgres on 127.0.0.1:55432, apply the hosted migrations,
install the in-memory Storage backend, seed one synthetic post-call output with
hostile Markdown for XSS checks, then serve the app on 127.0.0.1:8787.

Synthetic data only. Never point this helper at a production database.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

# .agents/skills/testing-hosted-review-ui/hosted_dev_server.py -> repo root
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "webapp"))
sys.path.insert(0, str(REPO))

HOST = "127.0.0.1"
PORT = 55432
EMAIL = "reviewer@airbyte.io"

ENV = {
    "HOSTED_MODE": "1",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon-key",
    "HOSTED_JWT_ALGORITHM": "HS256",
    "HOSTED_JWT_SECRET": "super-secret-32-byte-test-jwt-key!",
    "SUPABASE_JWT_SECRET": "super-secret-32-byte-test-storage-jwt-key!",
    "HOSTED_CONTEXT_SECRET": "test-context-secret-32-bytes!!",
    "APP_WORKER_PASSWORD": "app_worker_password",
    "BETA_ALLOWED_EMAILS": EMAIL,
    "MIGRATE_DATABASE_URL": f"postgresql://test:test@{HOST}:{PORT}/test",
    "DATABASE_URL": f"postgresql://app_user:app_user_password@{HOST}:{PORT}/test",
    "DATABASE_ADMIN_URL": f"postgresql://app_admin:app_admin_password@{HOST}:{PORT}/test",
    "DATABASE_WORKER_URL": f"postgresql://app_worker:app_worker_password@{HOST}:{PORT}/test",
}

os.environ.update(ENV)

# Keep this equivalent to eval/tests/conftest.py::_ensure_storage_schema. The
# helper exists because migration 002 expects Supabase's auth/storage objects to
# exist, while local acceptance uses a plain Postgres container.
STORAGE_SHIM = """
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

HOSTILE = "\n".join(
    [
        "",
        "<script>alert('xss-from-document')</script>",
        '<img src=x onerror="alert(\'xss-img\')">',
        "- [click me](javascript:alert('xss-link'))",
        "- Unicode holds: café ✓ Привет",
    ]
)


def markdown_seed() -> str:
    fixture = REPO / "eval/fixtures/outputs/post-call-full.md"
    text = fixture.read_text(encoding="utf-8")
    return text.replace("## Key Takeaways", "## Key Takeaways" + HOSTILE, 1)


async def _stream(data: bytes):
    yield data


async def prepare() -> dict[str, str]:
    conn = await asyncpg.connect(ENV["MIGRATE_DATABASE_URL"])
    try:
        await conn.execute(STORAGE_SHIM)
    finally:
        await conn.close()

    import hosted
    import hosted.migrations as migrations

    await migrations.migrate(
        ENV["MIGRATE_DATABASE_URL"],
        hosted.config.MIGRATIONS_DIR,
        app_user_password="app_user_password",
        app_admin_password="app_admin_password",
        app_worker_password="app_worker_password",
        context_secret=ENV["HOSTED_CONTEXT_SECRET"],
    )

    from hosted import storage

    storage.set_backend(storage.MemoryStorageBackend())
    backend = storage.get_backend()

    pool = await asyncpg.create_pool(ENV["DATABASE_ADMIN_URL"], min_size=1, max_size=2)
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    account_id, transcript_id, output_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO public.users (id, email) VALUES ($1, $2)",
                user_id,
                EMAIL,
            )
            await conn.execute(
                "INSERT INTO public.organizations (id, name, slug) "
                "VALUES ($1, 'Airbyte Test', 'airbyte-test')",
                org_id,
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
                "VALUES ($1, $2, 'Acme Synthetic', 'acme-synthetic', $3)",
                account_id,
                org_id,
                user_id,
            )

            tpath = f"{org_id}/{account_id}/{transcript_id}-test-transcript.txt"
            await conn.execute(
                "INSERT INTO public.transcripts "
                "(id, org_id, account_id, opportunity_id, storage_path, original_filename, "
                "size_bytes, mime_type, uploaded_by) "
                "VALUES ($1, $2, $3, NULL, $4, 'test-transcript.txt', 100, "
                "'text/plain', $5)",
                transcript_id,
                org_id,
                account_id,
                tpath,
                user_id,
            )

            job_id = await conn.fetchval(
                "INSERT INTO public.jobs "
                "(org_id, account_id, transcript_id, requester_id, skill, skill_version, "
                "status, max_attempts) "
                "VALUES ($1, $2, $3, $4, 'post-call', '1.0', 'success', 1) "
                "RETURNING id",
                org_id,
                account_id,
                transcript_id,
                user_id,
            )

            opath = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"
            await conn.execute(
                "INSERT INTO public.outputs "
                "(id, org_id, job_id, account_id, transcript_id, requester_id, "
                "content_storage_path, title, sidecar, skill, skill_version, model, "
                "validation_status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, 'Acme post-call brief', "
                "$8::jsonb, 'post-call', '1.0', 'claude-test', 'valid')",
                output_id,
                org_id,
                job_id,
                account_id,
                transcript_id,
                user_id,
                opath,
                json.dumps(
                    {
                        "skill": "post-call",
                        "mode": "full",
                        "validation_status": "valid",
                    }
                ),
            )
    finally:
        await pool.close()

    await backend.upload(
        user_id,
        tpath,
        _stream(b"Synthetic transcript text."),
        "text/plain; charset=utf-8",
        bucket=storage.DEFAULT_BUCKET,
    )
    await backend.upload(
        user_id,
        opath,
        _stream(markdown_seed().encode("utf-8")),
        "text/plain; charset=utf-8",
        bucket=storage.OUTPUTS_BUCKET,
    )

    import jwt as pyjwt

    now = datetime.now(timezone.utc).timestamp()
    token = pyjwt.encode(
        {
            "sub": str(user_id),
            "email": EMAIL,
            "aud": "authenticated",
            "iat": now,
            "exp": now + 36000,
        },
        ENV["HOSTED_JWT_SECRET"],
        algorithm="HS256",
    )

    return {
        "token": token,
        "account_id": str(account_id),
        "output_id": str(output_id),
    }


def main() -> None:
    info = asyncio.run(prepare())
    sign_in_url = (
        f"http://127.0.0.1:8787/#access_token={info['token']}"
        "&token_type=bearer"
    )
    review_url = (
        f"http://127.0.0.1:8787/#/hosted/accounts/{info['account_id']}"
        f"/outputs/{info['output_id']}"
    )
    Path("/tmp/hosted_dev_urls.txt").write_text(
        f"{sign_in_url}\n{review_url}\n",
        encoding="utf-8",
    )
    print("SIGNIN_URL:", sign_in_url, flush=True)
    print("REVIEW_URL:", review_url, flush=True)

    import uvicorn
    import webapp.app as app_module

    uvicorn.run(app_module.app, host="127.0.0.1", port=8787, log_level="info")


if __name__ == "__main__":
    main()
