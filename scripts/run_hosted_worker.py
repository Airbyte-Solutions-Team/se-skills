#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg>=0.30.0",
#   "pyjwt[crypto]>=2.10.0",
# ]
# ///
"""Standalone worker entry point for hosted SE Skills jobs.

Run from the repo root:
    HOSTED_MODE=1 DATABASE_WORKER_URL=postgresql://app_worker:... \
        uv run --script scripts/run_hosted_worker.py

Or for a single poll/claim/execute cycle:
    ... uv run --script scripts/run_hosted_worker.py --once
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Make the webapp package importable when running this script directly.
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "webapp"))

import config  # noqa: E402
from hosted import config as hosted_config  # noqa: E402
from hosted.worker import Worker, create_pool  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("run_hosted_worker")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hosted SE Skills worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll/claim/execute cycle and exit",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()

    if not hosted_config.is_hosted():
        logger.error("HOSTED_MODE is not enabled")
        return 1

    pool = await create_pool()
    worker = Worker(pool)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)

    try:
        if args.once:
            processed = await worker.run_once()
            logger.info("Processed job in single-run mode: %s", processed)
        else:
            await worker.run()
    finally:
        await pool.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
