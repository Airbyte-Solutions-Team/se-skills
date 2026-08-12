#!/usr/bin/env python3
"""Run versioned Postgres migrations for the hosted SE Skills data layer.

Usage:
    HOSTED_MODE=1 MIGRATE_DATABASE_URL=postgresql://... \
        APP_USER_PASSWORD=... APP_ADMIN_PASSWORD=... HOSTED_CONTEXT_SECRET=... \
        uv run scripts/migrate.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.hosted import config, migrations


async def main() -> int:
    if not config.MIGRATE_DATABASE_URL:
        print("MIGRATE_DATABASE_URL is required", file=sys.stderr)
        return 1
    if not config.APP_USER_PASSWORD:
        print("APP_USER_PASSWORD is required and must be non-empty", file=sys.stderr)
        return 1
    if not config.APP_ADMIN_PASSWORD:
        print("APP_ADMIN_PASSWORD is required and must be non-empty", file=sys.stderr)
        return 1
    if not config.HOSTED_CONTEXT_SECRET:
        print("HOSTED_CONTEXT_SECRET is required and must be non-empty", file=sys.stderr)
        return 1
    ran = await migrations.migrate(
        config.MIGRATE_DATABASE_URL,
        config.MIGRATIONS_DIR,
        app_user_password=config.APP_USER_PASSWORD,
        app_admin_password=config.APP_ADMIN_PASSWORD,
        context_secret=config.HOSTED_CONTEXT_SECRET,
    )
    for version in ran:
        print(f"Applied migration {version}")
    print(f"Schema is up to date ({len(ran)} migrations applied)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
