"""Simple versioned SQL migration runner for the hosted database."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_MIGRATION_NAME_RE = re.compile(r"^(\d{3})_.*\.sql$")


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


def _substitute_placeholders(sql: str, values: dict[str, str]) -> str:
    """Replace `__NAME__` placeholders so real passwords never live in .sql files."""
    result = sql
    for key, value in values.items():
        result = result.replace(f"__{key}__", value)
    return result


async def migrate(
    dsn: str,
    migrations_dir: Path,
    *,
    app_user_password: str = "app_user_password",
    app_admin_password: str = "app_admin_password",
) -> list[str]:
    """Apply all unapplied migrations under a single admin connection.

    Placeholders replaced:
      - __APP_USER_PASSWORD__
      - __APP_ADMIN_PASSWORD__
      - __DATABASE_NAME__
    """
    values = {
        "APP_USER_PASSWORD": app_user_password,
        "APP_ADMIN_PASSWORD": app_admin_password,
        "DATABASE_NAME": "current_database()",  # only used inside EXECUTE format
    }

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            await ensure_migrations_table(conn)
            applied = await applied_versions(conn)
            ran: list[str] = []
            for version, path in list_migrations(migrations_dir):
                if version in applied:
                    continue
                sql = _substitute_placeholders(path.read_text(), values)
                async with conn.transaction():
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
