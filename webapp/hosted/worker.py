"""Postgres-backed worker for durable asynchronous jobs.

The worker runs as a separate process from FastAPI and connects with the
least-privilege `app_worker` Postgres role. All queue operations are performed
through narrowly scoped SECURITY DEFINER functions owned by the migration role.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from typing import Any

import asyncpg

from . import config
from .executor import EchoExecutor, Executor, ExecutorResult

logger = logging.getLogger(__name__)


def worker_id() -> str:
    """Return a stable identifier for this worker process."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


async def create_pool() -> asyncpg.Pool:
    """Create the `app_worker` connection pool used by the worker process."""
    if not config.DATABASE_WORKER_URL:
        raise RuntimeError("DATABASE_WORKER_URL is not configured")
    return await asyncpg.create_pool(
        config.DATABASE_WORKER_URL,
        min_size=1,
        max_size=2,
    )


class Worker:
    """Polls the Postgres job ledger, claims jobs atomically, and runs them.

    The worker is intentionally separate from the FastAPI process and uses a
    dedicated database identity. It does not read transcript bodies, call
    models, execute shell commands, or access the local filesystem by default.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        executor: Executor | None = None,
        *,
        worker_name: str | None = None,
        poll_interval: float | None = None,
        heartbeat_interval: float | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.pool = pool
        self.executor = executor or EchoExecutor()
        self.worker_name = worker_name or worker_id()
        self.poll_interval = poll_interval if poll_interval is not None else config.WORKER_POLL_INTERVAL
        self.heartbeat_interval = heartbeat_interval if heartbeat_interval is not None else config.WORKER_HEARTBEAT_INTERVAL
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else config.WORKER_TIMEOUT_SECONDS
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the worker to stop after the current attempt completes."""
        self._stop.set()

    async def run(self) -> None:
        """Poll until stopped, recovering expired leases and claiming work."""
        logger.info("Worker %s started", self.worker_name)
        while not self._stop.is_set():
            try:
                recovered = await self.recover_expired_leases()
                if recovered:
                    logger.debug("Worker %s recovered %s expired lease(s)", self.worker_name, recovered)
                processed = await self.process_one()
                if not processed:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.warning("Worker %s poll loop error: %s", self.worker_name, exc)
                await asyncio.sleep(self.poll_interval)
        logger.info("Worker %s stopped", self.worker_name)

    async def run_once(self) -> bool:
        """Run a single poll/claim/execute cycle and return whether a job ran."""
        await self.recover_expired_leases()
        return await self.process_one()

    async def recover_expired_leases(self) -> int:
        """Recover any running attempts whose lease has expired."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT public.recover_expired_leases()") or 0

    async def claim(self) -> asyncpg.Record | None:
        """Atomically claim the next eligible job."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM public.claim_next_job($1, $2)",
                self.worker_name,
                self.timeout_seconds,
            )

    async def heartbeat(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
    ) -> None:
        """Extend the lease for a running attempt."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "SELECT public.worker_heartbeat($1, $2, $3, $4)",
                job_id,
                attempt_number,
                lease_token,
                self.timeout_seconds,
            )

    async def complete(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
        result: ExecutorResult,
    ) -> None:
        """Record a successful attempt completion."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                SELECT public.complete_job(
                    $1, $2, $3, $4, $5, $6::jsonb, $7
                )
                """,
                job_id,
                attempt_number,
                lease_token,
                result.output_id,
                result.validation_status,
                json.dumps(result.token_usage),
                result.cost,
            )

    async def fail(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
        error_category: str,
        error: str,
    ) -> None:
        """Record a failed attempt, with bounded retry or terminal state."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "SELECT public.fail_job($1, $2, $3, $4, $5)",
                job_id,
                attempt_number,
                lease_token,
                error_category,
                error,
            )

    async def process_one(self) -> bool:
        """Claim, execute, and finalize one job. Returns True if a job ran."""
        claim = await self.claim()
        if claim is None:
            return False

        job_id = claim["job_id"]
        attempt_number = claim["attempt_number"]
        lease_token = claim["lease_token"]

        job = {
            "job_id": str(job_id),
            "org_id": str(claim["org_id"]),
            "account_id": str(claim["account_id"]),
            "transcript_id": str(claim["transcript_id"]),
            "opportunity_id": str(claim["opportunity_id"]) if claim["opportunity_id"] else None,
            "payload": claim["payload"],
            "input_refs": claim["input_refs"],
            "source_manifest": claim["source_manifest"],
        }

        heartbeat_stop = asyncio.Event()

        async def _heartbeat_loop() -> None:
            while not heartbeat_stop.is_set():
                try:
                    await self.heartbeat(job_id, attempt_number, lease_token)
                except Exception as exc:
                    logger.debug("Heartbeat failed for job %s attempt %s: %s", job_id, attempt_number, exc)
                    return
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=self.heartbeat_interval)
                except asyncio.TimeoutError:
                    continue

        heartbeat_task: asyncio.Task[None] | None = None
        try:
            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            result = await self.executor.execute(job)
            await self.complete(job_id, attempt_number, lease_token, result)
            return True
        except Exception as exc:
            logger.warning("Job %s attempt %s failed: %s", job_id, attempt_number, exc)
            error = str(exc)[:500]
            try:
                await self.fail(job_id, attempt_number, lease_token, "executor_error", error)
            except Exception as fail_exc:
                logger.warning("Failed to record job failure: %s", fail_exc)
            return True
        finally:
            heartbeat_stop.set()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
