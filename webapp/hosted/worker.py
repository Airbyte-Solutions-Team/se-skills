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
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from . import config
from .executor import EchoExecutor, Executor, ExecutorResult

logger = logging.getLogger(__name__)

CLEANUP_LEASE_SECONDS = 60


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


class HeartbeatLostError(Exception):
    """Raised when the worker cannot heartbeat and should stop the attempt."""


class Worker:
    """Polls the Postgres job ledger, claims jobs atomically, and runs them.

    The worker is intentionally separate from the FastAPI process and uses a
    dedicated database identity. It does not read transcript bodies, call
    models, execute shell commands, or access the local filesystem by default.

    Each claimed job has two separate timeout concepts:
    - `timeout_seconds`: immutable wall-clock execution deadline for the
      `executor.execute` call. It is measured from the moment the job is claimed
      and is never extended by heartbeats.
    - `heartbeat_interval` / `timeout_seconds` lease: the database `timeout_at`
      column is refreshed by heartbeats so a crashed worker can be recovered.
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

    async def cleanup_next_tombstone(self) -> bool:
        """If the executor supports durable tombstone cleanup, clean one row.

        Any failure is logged and suppressed so a tombstone cleanup problem
        does not block the worker from processing jobs.
        """
        if not hasattr(self.executor, "cleanup_next_tombstone"):
            return False
        try:
            return await self.executor.cleanup_next_tombstone(
                self.worker_name, CLEANUP_LEASE_SECONDS
            )
        except Exception as exc:
            logger.warning(
                "Worker %s tombstone cleanup failed: %s",
                self.worker_name,
                type(exc).__name__,
            )
            return False

    async def run(self) -> None:
        """Poll until stopped, recovering expired leases and cleaning tombstones."""
        logger.info("Worker %s started", self.worker_name)
        while not self._stop.is_set():
            try:
                recovered = await self.recover_expired_leases()
                if recovered:
                    logger.debug("Worker %s recovered %s expired lease(s)", self.worker_name, recovered)
                cleaned = await self.cleanup_next_tombstone()
                if cleaned:
                    logger.debug("Worker %s cleaned one tombstone", self.worker_name)
                processed = await self.process_one()
                if not processed and not cleaned and not recovered:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.warning("Worker %s poll loop error: %s", self.worker_name, exc)
                await asyncio.sleep(self.poll_interval)
        logger.info("Worker %s stopped", self.worker_name)

    async def run_once(self) -> bool:
        """Run a single poll cycle: recover, clean tombstones, and claim work."""
        await self.recover_expired_leases()
        await self.cleanup_next_tombstone()
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
    ) -> bool:
        """Extend the lease for a running attempt and observe cancellation.

        Returns `True` when the job has a pending cancellation request.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
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
        """Record a successful attempt completion with actual runtime lineage."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                SELECT public.complete_job(
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9
                )
                """,
                job_id,
                attempt_number,
                lease_token,
                result.output_id,
                result.validation_status,
                json.dumps(result.token_usage),
                result.cost,
                result.runtime_version,
                result.model,
            )

    async def fail(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
        error_category: str,
        error: str,
        validation_status: str = "unvalidated",
        runtime_version: str | None = None,
        model: str | None = None,
    ) -> None:
        """Record a failed attempt, with bounded retry or terminal state."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                SELECT public.fail_job($1, $2, $3, $4, $5, NULL, $6, $7, $8)
                """,
                job_id,
                attempt_number,
                lease_token,
                error_category,
                error,
                runtime_version,
                model,
                validation_status,
            )

    async def cancel(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
    ) -> None:
        """Record a clean worker-side cancellation."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "SELECT public.cancel_job($1, $2, $3)",
                job_id,
                attempt_number,
                lease_token,
            )

    async def _heartbeat_monitor(
        self,
        job_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
        stop: asyncio.Event,
        cancel_requested: asyncio.Event,
        heartbeat_lost: asyncio.Event,
        completed: asyncio.Event,
    ) -> None:
        """Extend the DB lease until the job completes, times out, or cancels.

        Raises `HeartbeatLostError` if a heartbeat cannot be sent, so the worker
        stops the attempt without falsely marking it as a user cancellation.
        """
        while not stop.is_set() and not completed.is_set():
            try:
                requested = await self.heartbeat(job_id, attempt_number, lease_token)
            except Exception as exc:
                logger.warning(
                    "Heartbeat lost for job %s attempt %s: %s",
                    job_id,
                    attempt_number,
                    exc,
                )
                heartbeat_lost.set()
                raise HeartbeatLostError("Heartbeat failed") from exc
            if requested:
                cancel_requested.set()
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

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
            "attempt_number": attempt_number,
            "lease_token": str(lease_token),
            "worker_id": self.worker_name,
            "org_id": str(claim["org_id"]),
            "account_id": str(claim["account_id"]),
            "transcript_id": str(claim["transcript_id"]),
            "opportunity_id": str(claim["opportunity_id"]) if claim["opportunity_id"] else None,
            "requester_id": str(claim["requester_id"]),
            "skill_version": claim.get("skill_version") or "1.0",
            "model": claim.get("model"),
            "runtime_version": claim.get("runtime_version"),
            "payload": claim["payload"],
            "input_refs": claim["input_refs"],
            "source_manifest": claim["source_manifest"],
            "timeout_seconds": self.timeout_seconds,
        }

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout_seconds
        # The executor boundary receives a timezone-aware UTC wall-clock deadline,
        # not the event-loop monotonic clock.
        job["deadline_ts"] = datetime.now(tz=timezone.utc) + timedelta(seconds=self.timeout_seconds)

        heartbeat_stop = asyncio.Event()
        cancel_requested = asyncio.Event()
        heartbeat_lost = asyncio.Event()
        completed = asyncio.Event()

        if hasattr(self.executor, "set_cancellation"):
            self.executor.set_cancellation(cancel_requested)

        executor_task: asyncio.Task[ExecutorResult] = asyncio.create_task(self.executor.execute(job))
        monitor_task = asyncio.create_task(
            self._heartbeat_monitor(
                job_id,
                attempt_number,
                lease_token,
                heartbeat_stop,
                cancel_requested,
                heartbeat_lost,
                completed,
            )
        )

        try:
            timeout = max(deadline - loop.time(), 0)
            done, pending = await asyncio.wait(
                {executor_task, monitor_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                # Immutable execution deadline exceeded.
                executor_task.cancel()
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await executor_task
                with suppress(asyncio.CancelledError):
                    await monitor_task
                logger.warning(
                    "Job %s attempt %s exceeded wall-clock deadline",
                    job_id,
                    attempt_number,
                )
                await self.fail(
                    job_id,
                    attempt_number,
                    lease_token,
                    "timeout",
                    "Execution exceeded wall-clock deadline",
                )
                return True

            if heartbeat_lost.is_set():
                # The heartbeat monitor lost its lease; stop the attempt and let
                # lease recovery pick it up. Do not treat this as a user
                # cancellation.
                executor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await executor_task
                logger.warning(
                    "Job %s attempt %s heartbeat lost; leaving for lease recovery",
                    job_id,
                    attempt_number,
                )
                return True

            if cancel_requested.is_set():
                # A member requested cancellation while the job was running.
                executor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await executor_task
                logger.info(
                    "Job %s attempt %s cancelled during execution",
                    job_id,
                    attempt_number,
                )
                await self.cancel(job_id, attempt_number, lease_token)
                return True

            # Executor finished before the deadline.
            completed.set()
            heartbeat_stop.set()
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task

            try:
                result = await executor_task
            except Exception as exc:
                logger.warning(
                    "Job %s attempt %s executor raised: %s",
                    job_id,
                    attempt_number,
                    type(exc).__name__,
                )
                await self.fail(
                    job_id,
                    attempt_number,
                    lease_token,
                    "executor_error",
                    "Executor failure: executor raised an exception",
                    runtime_version=getattr(self.executor, "runtime_version", None),
                    model=getattr(self.executor, "model", None),
                )
                return True

            if cancel_requested.is_set():
                if getattr(result, "finalized", False):
                    return True
                await self.cancel(job_id, attempt_number, lease_token)
            elif getattr(result, "error_category", None):
                if getattr(result, "finalized", False):
                    return True
                await self.fail(
                    job_id,
                    attempt_number,
                    lease_token,
                    result.error_category,
                    result.error or "Execution failed",
                    validation_status=getattr(result, "validation_status", "unvalidated") or "unvalidated",
                    runtime_version=result.runtime_version,
                    model=result.model,
                )
            else:
                if getattr(result, "finalized", False):
                    return True
                await self.complete(job_id, attempt_number, lease_token, result)
            return True
        finally:
            heartbeat_stop.set()
            if not executor_task.done():
                executor_task.cancel()
            if not monitor_task.done():
                monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(executor_task, monitor_task, return_exceptions=True)
