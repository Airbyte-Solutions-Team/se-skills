#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg>=0.30.0",
#   "pyjwt[crypto]>=2.10.0",
#   "pydantic>=2.0",
#   "httpx>=0.25",
#   "fastapi>=0.100",
#   "uvicorn[standard]>=0.30.0",
# ]
# ///
"""Standalone worker entry point for hosted SE Skills jobs.

Run from the repo root:
    HOSTED_MODE=1 DATABASE_WORKER_URL=postgresql://app_worker:... \
        uv run --script scripts/run_hosted_worker.py --runtime echo

To run a real post-call attempt inside a gVisor `runsc` sandbox:
    HOSTED_MODE=1 DATABASE_WORKER_URL=... ANTHROPIC_API_KEY=... \
        MODEL_PROXY_SECRET=... RUNSC_ROOTFS=... \
        uv run --script scripts/run_hosted_worker.py --runtime post-call-runsc

Or for a single poll/claim/execute cycle:
    ... uv run --script scripts/run_hosted_worker.py --once --runtime post-call-runsc
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

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
    parser.add_argument(
        "--runtime",
        choices=["echo", "post-call-runsc"],
        default="echo",
        help="Executor to use for claimed jobs",
    )
    return parser.parse_args()


def _build_executor(runtime: str, pool: asyncpg.Pool) -> Any:
    """Build the executor selected by the operator.

    The `post-call-runsc` mode fails closed if the model proxy or sandbox
    prerequisites are not configured. It never silently falls back to an
    in-process runtime.
    """
    if runtime == "echo":
        from hosted.executor import EchoExecutor

        return EchoExecutor()

    if runtime == "post-call-runsc":
        from hosted.post_call_orchestrator import PostCallExecutor
        from hosted.runsc_executor import create_runsc_runtime

        missing: list[str] = []
        if not hosted_config.ANTHROPIC_API_KEY:
            missing.append("ANTHROPIC_API_KEY")
        if not hosted_config.MODEL_PROXY_SECRET:
            missing.append("MODEL_PROXY_SECRET")
        if not hosted_config.RUNSC_ROOTFS:
            missing.append("RUNSC_ROOTFS")
        if not shutil.which(hosted_config.RUNSC_BINARY):
            missing.append(f"runsc binary ({hosted_config.RUNSC_BINARY})")
        if missing:
            raise RuntimeError(
                f"post-call-runsc runtime is missing prerequisites: {', '.join(missing)}"
            )

        runtime_impl = create_runsc_runtime()
        return PostCallExecutor(runtime=runtime_impl, db_pool=pool)

    raise RuntimeError(f"Unknown runtime: {runtime}")


async def main() -> int:
    args = _parse_args()

    if not hosted_config.is_hosted():
        logger.error("HOSTED_MODE is not enabled")
        return 1

    pool = await create_pool()
    try:
        executor = _build_executor(args.runtime, pool)
    except Exception as exc:
        logger.error("Failed to build executor: %s", exc)
        await pool.close()
        return 1

    worker = Worker(pool, executor=executor)

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
