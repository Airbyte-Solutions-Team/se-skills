"""Database pool and JWKS client for the hosted app.

The web process only needs the least-privileged `app_user` pool. The migration
role (`app_admin`) is used by `scripts/migrate.py` and is not kept in the
running application.
"""
from __future__ import annotations

import logging

import asyncpg
import jwt

from . import config

logger = logging.getLogger(__name__)

_jwks_client: jwt.PyJWKClient | None = None


def jwks_client() -> jwt.PyJWKClient:
    """Lazy singleton PyJWKClient for Supabase RS256 JWT verification."""
    global _jwks_client
    if _jwks_client is None:
        if not config.SUPABASE_URL:
            raise RuntimeError("SUPABASE_URL is not configured")
        _jwks_client = jwt.PyJWKClient(config.jwks_url(), cache_keys=True)
    return _jwks_client


def clear_jwks_client() -> None:
    global _jwks_client
    _jwks_client = None


async def create_pool() -> asyncpg.Pool:
    """Create the `app_user` connection pool used by normal requests."""
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
