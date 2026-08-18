#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
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
from typing import TYPE_CHECKING
from pathlib import Path

# Make the webapp package importable when running this script directly.
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "webapp"))
sys.path.insert(0, str(repo_root))

import config  # noqa: E402
from hosted import config as hosted_config  # noqa: E402
from hosted.executor import Executor  # noqa: E402
from hosted.preflight import LocalHostProbe  # noqa: E402
from hosted.production_preflight import (  # noqa: E402
    ProductionPreflightSettings,
    run_production_preflight,
)
from hosted.worker import Worker, create_pool  # noqa: E402

if TYPE_CHECKING:
    import asyncpg

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


def _build_executor(runtime: str, pool: "asyncpg.Pool") -> Executor:
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


def _run_production_preflight() -> bool:
    names = (
        "DATABASE_WORKER_URL",
        "ANTHROPIC_API_KEY",
        "MODEL_PROXY_SECRET",
        "RUNSC_ROOTFS",
        "SANDBOX_IMAGE_DIGEST",
        "SANDBOX_MANIFEST_PATH",
    )
    present = frozenset(
        name
        for name in names
        if os.environ.get(name)
        or name == "SANDBOX_MANIFEST_PATH"
    )
    report = run_production_preflight(
        ProductionPreflightSettings(
            runsc_path=hosted_config.RUNSC_BINARY,
            rootfs_path=hosted_config.RUNSC_ROOTFS,
            manifest_path=hosted_config.SANDBOX_MANIFEST_PATH,
            hosted_env=hosted_config.HOSTED_ENV,
            runtime="post-call-runsc",
            worker_user="se-worker",
            worker_group="se-worker",
            worker_uid=hosted_config.HOSTED_WORKER_UID,
            non_worker_uid=hosted_config.HOSTED_NON_WORKER_UID,
            present_config_names=present,
            model_proxy_secret=hosted_config.MODEL_PROXY_SECRET,
            anthropic_api_url=hosted_config.ANTHROPIC_API_URL,
            database_url=hosted_config.DATABASE_WORKER_URL,
            storage_url=hosted_config.SUPABASE_STORAGE_ENDPOINT,
            sandbox_image_digest=hosted_config.SANDBOX_IMAGE_DIGEST,
            approved_https_destinations=hosted_config.APPROVED_HTTPS_DESTINATIONS,
        ),
        LocalHostProbe(),
    )
    if report.ok:
        return True
    for check in report.failed_required():
        logger.error("Hosted preflight failed: %s — %s", check.check_id, check.detail)
    return False


async def main() -> int:
    args = _parse_args()

    if not hosted_config.is_hosted():
        logger.error("HOSTED_MODE is not enabled")
        return 1
    if hosted_config.HOSTED_ENV == "production":
        if args.runtime == "echo":
            logger.error("EchoExecutor is not allowed in production")
            return 1
        if not _run_production_preflight():
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
