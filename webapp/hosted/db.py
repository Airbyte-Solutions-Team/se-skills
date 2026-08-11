"""Database pools and JWKS client for the hosted app."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

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


async def create_pools() -> tuple[asyncpg.Pool, asyncpg.Pool]:
    """Create admin and app_user connection pools."""
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    if not config.DATABASE_ADMIN_URL:
        raise RuntimeError("DATABASE_ADMIN_URL is not configured")

    user_pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    admin_pool = await asyncpg.create_pool(
        config.DATABASE_ADMIN_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    return admin_pool, user_pool
