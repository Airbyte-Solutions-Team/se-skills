"""Hosted-mode configuration for the SE Skills webapp.

These settings are only read when `HOSTED_MODE` is enabled. The local-only
workflow keeps running when `HOSTED_MODE` is unset, so existing users are not
forced onto Supabase or a database.
"""
from __future__ import annotations

import os
from pathlib import Path


_HOSTED_MODE = (os.environ.get("HOSTED_MODE") or "").lower() in ("1", "true", "yes")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATABASE_ADMIN_URL = os.environ.get("DATABASE_ADMIN_URL", "")
MIGRATE_DATABASE_URL = os.environ.get("MIGRATE_DATABASE_URL", "")

APP_USER_PASSWORD = os.environ.get("APP_USER_PASSWORD", "")
APP_ADMIN_PASSWORD = os.environ.get("APP_ADMIN_PASSWORD", "")

HOSTED_JWT_ALGORITHM = os.environ.get("HOSTED_JWT_ALGORITHM", "RS256")
HOSTED_JWT_SECRET = os.environ.get("HOSTED_JWT_SECRET", "")

# Supabase JWT secret used to sign short-lived Storage JWTs. The backend signs
# a token for a dedicated `app_storage` Postgres role; the user's browser token
# is never authorized for direct Storage object operations. This secret is only
# used server-side and is never sent to the browser.
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")

# Shared secret used to sign the tenant context token that the application
# passes to the database. The DB stores the same secret in app_private and
# verifies the token inside SECURITY DEFINER functions so the app_user role
# cannot forge a different user's tenant context.
HOSTED_CONTEXT_SECRET = os.environ.get("HOSTED_CONTEXT_SECRET", "")

BETA_ALLOWED_EMAILS = {e.strip().lower() for e in (os.environ.get("BETA_ALLOWED_EMAILS") or "").split(",") if e.strip()}
BETA_ALLOW_AIRBYTE_DOMAIN = (os.environ.get("BETA_ALLOW_AIRBYTE_DOMAIN") or "").lower() in ("1", "true", "yes")

SUPABASE_STORAGE_ENDPOINT = os.environ.get("SUPABASE_STORAGE_ENDPOINT", "")

_TRANSCRIPT_MAX_BYTES_STR = os.environ.get("TRANSCRIPT_MAX_BYTES", "10485760")
TRANSCRIPT_MAX_BYTES = int(_TRANSCRIPT_MAX_BYTES_STR) if _TRANSCRIPT_MAX_BYTES_STR.isdigit() else 10485760

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def is_hosted() -> bool:
    return _HOSTED_MODE


def hosted_public_config() -> dict:
    """Configuration the SPA needs to initialize Supabase Auth."""
    return {
        "hosted": is_hosted(),
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }


def supabase_issuer() -> str:
    """Return the expected issuer for Supabase Auth access tokens."""
    if not SUPABASE_URL:
        return ""
    return f"{SUPABASE_URL.rstrip('/')}/auth/v1"


def jwks_url() -> str:
    """Return the Supabase Auth JWKS endpoint for the project."""
    base = SUPABASE_URL.rstrip("/")
    return f"{base}/auth/v1/.well-known/jwks.json"


def require_hosted() -> None:
    if not is_hosted():
        raise RuntimeError("Hosted mode is not enabled")
