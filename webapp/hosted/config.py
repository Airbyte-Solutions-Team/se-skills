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

BETA_ALLOWED_EMAILS = {e.strip().lower() for e in (os.environ.get("BETA_ALLOWED_EMAILS") or "").split(",") if e.strip()}
BETA_ALLOW_AIRBYTE_DOMAIN = (os.environ.get("BETA_ALLOW_AIRBYTE_DOMAIN") or "").lower() in ("1", "true", "yes")

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


def jwks_url() -> str:
    base = SUPABASE_URL.rstrip("/")
    return f"{base}/.well-known/jwks.json"


def require_hosted() -> None:
    if not is_hosted():
        raise RuntimeError("Hosted mode is not enabled")
