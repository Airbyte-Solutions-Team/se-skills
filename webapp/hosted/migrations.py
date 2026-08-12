"""Simple versioned SQL migration runner for the hosted database."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_MIGRATION_NAME_RE = re.compile(r"^(\d{3})_.*\.sql$")

# GUC names used to pass role passwords and the shared context secret into
# migrations without string substitution. The migration runner sets these inside
# each transaction with `set_config`, and migrations read them with
# `current_setting` inside `format` (which safely quotes literals) or direct GUC
# access for the context secret.
_APP_USER_PASSWORD_GUC = "migration.app_user_password"
_APP_ADMIN_PASSWORD_GUC = "migration.app_admin_password"
_APP_WORKER_PASSWORD_GUC = "migration.app_worker_password"
_CONTEXT_SECRET_GUC = "migration.context_secret"


async def ensure_migrations_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def list_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    files = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        match = _MIGRATION_NAME_RE.match(path.name)
        if match:
            files.append((match.group(1), path))
    return files


async def applied_versions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT version FROM public.schema_migrations")
    return {r["version"] for r in rows}


def _validate_passwords(app_user_password: str, app_admin_password: str) -> None:
    if not app_user_password:
        raise ValueError("app_user_password must be a non-empty string")
    if not app_admin_password:
        raise ValueError("app_admin_password must be a non-empty string")


def _validate_context_secret(context_secret: str) -> None:
    if not context_secret:
        raise ValueError("context_secret must be a non-empty string")


def _validate_worker_password(app_worker_password: str) -> None:
    if not app_worker_password:
        raise ValueError("app_worker_password must be a non-empty string")


async def migrate(
    dsn: str,
    migrations_dir: Path,
    *,
    app_user_password: str,
    app_admin_password: str,
    app_worker_password: str,
    context_secret: str,
) -> list[str]:
    """Apply all unapplied migrations under a single admin connection.

    Role passwords and the shared tenant-context signing secret are passed as
    transaction-local GUCs. The SQL migration reads the passwords through
    `current_setting` inside `format(... %L, current_setting(...))` and the
    secret directly, so nothing is interpolated into migration text.
    """
    _validate_passwords(app_user_password, app_admin_password)
    _validate_worker_password(app_worker_password)
    _validate_context_secret(context_secret)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            await ensure_migrations_table(conn)
            applied = await applied_versions(conn)
            ran: list[str] = []
            for version, path in list_migrations(migrations_dir):
                if version in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config($1, $2, true)",
                        _APP_USER_PASSWORD_GUC,
                        app_user_password,
                    )
                    await conn.execute(
                        "SELECT set_config($1, $2, true)",
                        _APP_ADMIN_PASSWORD_GUC,
                        app_admin_password,
                    )
                    await conn.execute(
                        "SELECT set_config($1, $2, true)",
                        _CONTEXT_SECRET_GUC,
                        context_secret,
                    )
                    await conn.execute(
                        "SELECT set_config($1, $2, true)",
                        _APP_WORKER_PASSWORD_GUC,
                        app_worker_password,
                    )
                    sql = path.read_text()
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO public.schema_migrations (version) VALUES ($1)",
                        version,
                    )
                ran.append(version)
                logger.info("Applied migration %s (%s)", version, path.name)
            return ran
    finally:
        await pool.close()


async def latest_applied(dsn: str) -> list[str]:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT version FROM public.schema_migrations ORDER BY version"
            )
            return [r["version"] for r in rows]
    finally:
        await pool.close()
