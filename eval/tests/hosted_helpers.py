"""Shared helpers for hosted-mode integration tests."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from datetime import datetime, timezone

import asyncpg
import jwt


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


def _context_token(user_id: uuid.UUID) -> str:
    """Return the signed context token the application would set for a user."""
    secret = os.environ["HOSTED_CONTEXT_SECRET"].encode("utf-8")
    user_text = str(user_id).encode("utf-8")
    return f"{user_id}:{hmac.new(secret, user_text, hashlib.sha256).hexdigest()}"


async def _seed_member(
    admin_pool: asyncpg.Pool,
    email: str,
    *,
    org_name: str | None = None,
    org_slug: str | None = None,
    active: bool = True,
    role: str = "member",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    # Use a unique suffix for the default org to avoid slug collisions when
    # many tests share the same Postgres container.
    suffix = uuid.uuid4().hex[:8]
    name = org_name or f"Airbyte {suffix}"
    slug = org_slug or f"airbyte-{suffix}"
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
            name,
            slug,
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


def _unique_slug(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return f"{base[:60] or 'test'}-{uuid.uuid4().hex[:8]}"


async def _seed_account(
    admin_pool: asyncpg.Pool,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str = "Test Account",
) -> uuid.UUID:
    async with admin_pool.acquire() as conn:
        account_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO public.accounts (id, org_id, name, slug, created_by)
            VALUES ($1, $2, $3, $4, $5)
            """,
            account_id,
            org_id,
            name,
            _unique_slug(name),
            created_by,
        )
        return account_id


async def _seed_opportunity(
    admin_pool: asyncpg.Pool,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str = "Test Opportunity",
) -> uuid.UUID:
    async with admin_pool.acquire() as conn:
        opportunity_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO public.opportunities (id, org_id, account_id, name, slug, created_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            opportunity_id,
            org_id,
            account_id,
            name,
            _unique_slug(name),
            created_by,
        )
        return opportunity_id


async def _seed_transcript(
    admin_pool: asyncpg.Pool,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None,
    uploaded_by: uuid.UUID,
    filename: str = "test-transcript.txt",
    size_bytes: int = 100,
    mime_type: str = "text/plain",
) -> uuid.UUID:
    """Insert a transcript metadata row directly for job lifecycle tests."""
    async with admin_pool.acquire() as conn:
        transcript_id = uuid.uuid4()
        storage_path = f"{org_id}/{account_id}/{transcript_id}-{filename}"
        await conn.execute(
            """
            INSERT INTO public.transcripts (
                id, org_id, account_id, opportunity_id, storage_path,
                original_filename, size_bytes, mime_type, uploaded_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            transcript_id,
            org_id,
            account_id,
            opportunity_id,
            storage_path,
            filename,
            size_bytes,
            mime_type,
            uploaded_by,
        )
        return transcript_id
