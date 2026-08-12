"""JWT verification and FastAPI auth/org dependencies for the hosted app."""
from __future__ import annotations

import hmac
import hashlib
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

    if not config.SUPABASE_URL:
        raise AuthError("SUPABASE_URL is required for RS256 verification", status.HTTP_500_INTERNAL_SERVER_ERROR)

    return db.jwks_client().get_signing_key_from_jwt(token)


def _decode(token: str) -> dict[str, Any]:
    key = _signing_key(token)
    decode_kwargs: dict[str, Any] = {
        "algorithms": [config.HOSTED_JWT_ALGORITHM],
        "audience": "authenticated",
        "options": {"verify_exp": True, "verify_aud": True},
    }
    if config.HOSTED_JWT_ALGORITHM.upper() == "RS256":
        issuer = config.supabase_issuer()
        if issuer:
            decode_kwargs["issuer"] = issuer
    try:
        return jwt.decode(token, key, **decode_kwargs)
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("Invalid token issuer") from exc
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


def get_token_from_request(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith(_BEARER_PREFIX):
        return auth[len(_BEARER_PREFIX):].strip()
    return (request.cookies.get("se-hosted-token") or "").strip()


def _tenant_context_token(user_id: uuid.UUID) -> str:
    """Return a signed context token for the verified user.

    The token is `user_id:hmac(user_id, secret)`. The database stores the same
    secret in an app_private table and verifies the signature inside SECURITY
    DEFINER functions, so the runtime `app_user` role cannot forge another
    user's tenant context.
    """
    if not config.HOSTED_CONTEXT_SECRET:
        raise AuthError("Hosted context secret not configured", status.HTTP_500_INTERNAL_SERVER_ERROR)
    secret = config.HOSTED_CONTEXT_SECRET.encode("utf-8")
    user_text = str(user_id).encode("utf-8")
    mac = hmac.new(secret, user_text, hashlib.sha256).hexdigest()
    return f"{user_id}:{mac}"


async def require_user(request: Request) -> User:
    """FastAPI dependency that validates the bearer token."""
    token = get_token_from_request(request)
    try:
        verified = verify_token(token)
    except AuthError:
        raise
    except Exception as exc:
        logger.warning("Unexpected token verification error: %s", exc)
        raise AuthError("Could not validate credentials") from exc
    return User(id=verified.user_id, email=verified.email)


async def _resolve_org(pool: asyncpg.Pool, user: User) -> OrgContext | None:
    """Look up the user's active membership using the `app_user` pool.

    Membership resolution is delegated to a narrow `SECURITY DEFINER` function
    owned by the migration role. The function verifies a signed context token so
    the web process never relies on caller-settable GUCs for identity.
    """
    context_token = _tenant_context_token(user.id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT membership_id, org_id, role FROM public.resolve_active_membership($1)",
            context_token,
        )
        if row is None:
            return None
        return OrgContext(
            user=user,
            org_id=row["org_id"],
            membership_id=row["membership_id"],
            role=row["role"],
            context_token=context_token,
        )


async def require_org(
    request: Request,
    user: User = Depends(require_user),
) -> OrgContext:
    """FastAPI dependency that resolves the user's active organization."""
    pool = request.app.state.hosted_user_pool
    org = await _resolve_org(pool, user)
    if org is None or not org.role or not org.membership_id:
        raise AuthError("No active organization membership", status.HTTP_403_FORBIDDEN)
    return org


@asynccontextmanager
async def tenant_connection(request: Request, org: OrgContext):
    """Yield an `app_user` connection with the verified tenant context set.

    The signed context token is set as a transaction-local GUC
    (`app.context_token`) and is verified by `SECURITY DEFINER` functions for
    every RLS policy check. It is automatically cleared on commit or rollback,
    preventing one request from leaking context to another request that reuses
    the same pool connection.
    """
    pool: asyncpg.Pool = request.app.state.hosted_user_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)",
                org.context_token,
            )
            yield conn
