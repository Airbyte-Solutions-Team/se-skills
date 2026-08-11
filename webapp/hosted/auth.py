"""JWT verification and FastAPI auth/org dependencies for the hosted app."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request, status

from . import config, db
from .models import OrgContext, User

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "bearer "


class AuthError(HTTPException):
    def __init__(self, detail: str, status_code: int = status.HTTP_401_UNAUTHORIZED) -> None:
        super().__init__(status_code=status_code, detail=detail)


@dataclass(frozen=True)
class VerifiedToken:
    user_id: uuid.UUID
    email: str
    raw: dict[str, Any]


def _signing_key(token: str) -> Any:
    """Return the secret or JWK needed to verify a token."""
    if config.HOSTED_JWT_ALGORITHM.upper() == "HS256":
        if not config.HOSTED_JWT_SECRET:
            raise AuthError("JWT secret not configured", status.HTTP_500_INTERNAL_SERVER_ERROR)
        return config.HOSTED_JWT_SECRET

    if config.HOSTED_JWT_ALGORITHM.upper() != "RS256":
        raise AuthError("Unsupported JWT algorithm", status.HTTP_500_INTERNAL_SERVER_ERROR)

    return db.jwks_client().get_signing_key_from_jwt(token)


def _decode(token: str) -> dict[str, Any]:
    key = _signing_key(token)
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[config.HOSTED_JWT_ALGORITHM],
            audience="authenticated",
            options={"verify_exp": True, "verify_aud": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid token") from exc


def verify_token(token: str) -> VerifiedToken:
    """Verify a Supabase access token and return the claims we care about."""
    if not token:
        raise AuthError("Missing token")
    payload = _decode(token)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise AuthError("Token missing valid sub claim") from exc
    email = payload.get("email", "")
    if not isinstance(email, str):
        email = ""
    return VerifiedToken(user_id=user_id, email=email, raw=payload)


def _get_token_from_request(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith(_BEARER_PREFIX):
        return auth[len(_BEARER_PREFIX):].strip()
    return (request.cookies.get("se-hosted-token") or "").strip()


async def require_user(request: Request) -> User:
    """FastAPI dependency that validates the bearer token."""
    token = _get_token_from_request(request)
    try:
        verified = verify_token(token)
    except AuthError:
        raise
    except Exception as exc:
        logger.warning("Unexpected token verification error: %s", exc)
        raise AuthError("Could not validate credentials") from exc
    return User(id=verified.user_id, email=verified.email)


async def _resolve_org(admin_pool: asyncpg.Pool, user: User) -> OrgContext | None:
    """Look up the user's active membership and return the organization context.

    The beta is scoped to a single Airbyte organization; if a user somehow has
    multiple active memberships we resolve the first one and warn.
    """
    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT m.id AS membership_id, m.org_id, m.role, m.active,
                   (SELECT count(*) FROM public.memberships m2
                    WHERE m2.user_id = $1 AND m2.active = true) AS org_count
            FROM public.memberships m
            WHERE m.user_id = $1 AND m.active = true
            ORDER BY m.created_at
            LIMIT 1
            """,
            user.id,
        )
        if row is None:
            return None
        if row["org_count"] > 1:
            logger.warning(
                "User %s has %d active memberships; using %s",
                user.id,
                row["org_count"],
                row["org_id"],
            )
        return OrgContext(
            user=user,
            org_id=row["org_id"],
            membership_id=row["membership_id"],
            role=row["role"],
        )


async def require_org(
    request: Request,
    user: User = Depends(require_user),
) -> OrgContext:
    """FastAPI dependency that resolves the user's active organization."""
    pool = request.app.state.hosted_admin_pool
    org = await _resolve_org(pool, user)
    if org is None or not org.role or not org.membership_id:
        raise AuthError("No active organization membership", status.HTTP_403_FORBIDDEN)
    return org


@asynccontextmanager
async def tenant_connection(request: Request, org: OrgContext):
    """Yield an app_user connection with the tenant context set for RLS.

    `app.current_org_id` and `app.current_user_id` are set with `SET LOCAL`
    inside a transaction so the value is automatically cleared on commit or
    rollback. This prevents one request from leaking organization context to
    another request that reuses the same pool connection.
    """
    pool: asyncpg.Pool = request.app.state.hosted_user_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # `SET LOCAL` cannot be parameterized, so these values are supplied
            # as string literals. They are UUIDs from the verified token/membership
            # lookup, not user-controlled free text.
            await conn.execute(
                f"SET LOCAL app.current_org_id = '{str(org.org_id)}'"
            )
            await conn.execute(
                f"SET LOCAL app.current_user_id = '{str(org.user.id)}'"
            )
            yield conn
