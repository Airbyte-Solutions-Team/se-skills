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
DATABASE_WORKER_URL = os.environ.get("DATABASE_WORKER_URL", "")
MIGRATE_DATABASE_URL = os.environ.get("MIGRATE_DATABASE_URL", "")

APP_USER_PASSWORD = os.environ.get("APP_USER_PASSWORD", "")
APP_ADMIN_PASSWORD = os.environ.get("APP_ADMIN_PASSWORD", "")
APP_WORKER_PASSWORD = os.environ.get("APP_WORKER_PASSWORD", "")

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

# Model proxy configuration. These values live only in the trusted worker process.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com")
ANTHROPIC_API_VERSION = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
ANTHROPIC_EGRESS_PROXY_URL = os.environ.get("ANTHROPIC_EGRESS_PROXY_URL", "")
MODEL_PROXY_SECRET = os.environ.get("MODEL_PROXY_SECRET", "")

# gVisor/runsc executor configuration.
RUNSC_BINARY = os.environ.get("RUNSC_BINARY", "/usr/local/bin/runsc")
RUNSC_ROOTFS = os.environ.get("RUNSC_ROOTFS", "")
HOSTED_ENV = os.environ.get("HOSTED_ENV", "development").lower()
SANDBOX_IMAGE_DIGEST = os.environ.get("SANDBOX_IMAGE_DIGEST", "")
SANDBOX_MANIFEST_PATH = os.environ.get(
    "SANDBOX_MANIFEST_PATH", "/etc/se-skills/sandbox-manifest.json"
)
RUNSC_BUNDLE_DIR = os.environ.get("RUNSC_BUNDLE_DIR", "/var/lib/se-skills/bundles")
RUNSC_STATE_DIR = os.environ.get("RUNSC_STATE_DIR", "/var/lib/se-skills/runsc")
HOSTED_WORKER_UID = int(os.environ.get("HOSTED_WORKER_UID", "995"))
HOSTED_NON_WORKER_UID = int(os.environ.get("HOSTED_NON_WORKER_UID", "994"))
APPROVED_HTTPS_DESTINATIONS = frozenset(
    value
    for value in os.environ.get("HOSTED_APPROVED_HTTPS_DESTINATIONS", "").split(",")
    if value
)
OBSERVABILITY_ENDPOINT = os.environ.get("OBSERVABILITY_ENDPOINT", "")
ALLOW_LIVE_HOSTED_SMOKE = os.environ.get("ALLOW_LIVE_HOSTED_SMOKE", "")

# Worker tuning. The worker process uses short lease/heartbeat intervals to detect
# crashed workers; values are configurable for tests and small deployments.
_WORKER_POLL_INTERVAL_STR = os.environ.get("WORKER_POLL_INTERVAL", "1")
WORKER_POLL_INTERVAL = float(_WORKER_POLL_INTERVAL_STR) if _WORKER_POLL_INTERVAL_STR.replace(".", "", 1).isdigit() else 1.0

_WORKER_HEARTBEAT_INTERVAL_STR = os.environ.get("WORKER_HEARTBEAT_INTERVAL", "10")
WORKER_HEARTBEAT_INTERVAL = float(_WORKER_HEARTBEAT_INTERVAL_STR) if _WORKER_HEARTBEAT_INTERVAL_STR.replace(".", "", 1).isdigit() else 10.0

_WORKER_TIMEOUT_SECONDS_STR = os.environ.get("WORKER_TIMEOUT_SECONDS", "300")
WORKER_TIMEOUT_SECONDS = int(_WORKER_TIMEOUT_SECONDS_STR) if _WORKER_TIMEOUT_SECONDS_STR.isdigit() else 300

_WORKER_MAX_ATTEMPTS_STR = os.environ.get("WORKER_MAX_ATTEMPTS", "3")
WORKER_MAX_ATTEMPTS = int(_WORKER_MAX_ATTEMPTS_STR) if _WORKER_MAX_ATTEMPTS_STR.isdigit() else 3

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
