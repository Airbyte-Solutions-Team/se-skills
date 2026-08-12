"""Organization-scoped job enqueue/list/detail/cancel API routes."""
from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

import asyncpg
from asyncpg.exceptions import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Request, status

from . import config, models
from .auth import require_org, tenant_connection
from .models import JobCreate, JobDetail, JobList, JobOut, OrgContext

router = APIRouter(prefix="/api/hosted", tags=["hosted"])


def _source_manifest_from_transcript(
    transcript: asyncpg.Record, opportunity_id: uuid.UUID | None
) -> dict[str, Any]:
    """Build an immutable, reconstructable manifest from a transcript record.

    The manifest contains only stable identifiers and non-sensitive metadata;
    transcript bodies, bearer tokens, and storage credentials are never stored.
    """
    return {
        "transcript_id": str(transcript["id"]),
        "account_id": str(transcript["account_id"]),
        "opportunity_id": str(opportunity_id) if opportunity_id else None,
        "org_id": str(transcript["org_id"]),
        "original_filename": transcript["original_filename"],
        "mime_type": transcript["mime_type"],
        "size_bytes": transcript["size_bytes"],
        "storage_path": transcript["storage_path"],
    }


async def _load_transcript_for_job(
    conn: asyncpg.Connection,
    transcript_id: uuid.UUID,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None,
    org_id: uuid.UUID,
) -> asyncpg.Record:
    """Fetch the transcript row and verify the account/opportunity/org chain."""
    if opportunity_id is not None:
        row = await conn.fetchrow(
            """
            SELECT * FROM public.transcripts
            WHERE id = $1 AND account_id = $2 AND opportunity_id = $3 AND org_id = $4
            """,
            transcript_id,
            account_id,
            opportunity_id,
            org_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT * FROM public.transcripts
            WHERE id = $1 AND account_id = $2 AND org_id = $3 AND opportunity_id IS NULL
            """,
            transcript_id,
            account_id,
            org_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transcript not found for this account or opportunity",
        )
    return row


@router.get("/accounts/{account_id}/jobs", response_model=JobList)
async def list_account_jobs(
    request: Request,
    account_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> JobList:
    """List jobs for the active organization's account."""
    async with tenant_connection(request, org) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM public.jobs
            WHERE org_id = $1 AND account_id = $2
            ORDER BY created_at DESC
            """,
            org.org_id,
            account_id,
        )
    return JobList(jobs=[JobOut.from_record(r) for r in rows])


@router.post("/accounts/{account_id}/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    account_id: uuid.UUID,
    data: JobCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> JobOut:
    """Enqueue a durable job for an uploaded transcript.

    The organization, account, opportunity, and transcript ownership are verified
    by a database function that checks the signed tenant context token and the
    same-organization foreign keys.
    """
    if data.account_id != account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="account_id in body must match the URL",
        )

    try:
        async with tenant_connection(request, org) as conn:
            # Load the transcript under tenant context to build the source manifest.
            # This also proves the transcript belongs to the account and organization.
            transcript = await _load_transcript_for_job(
                conn,
                data.transcript_id,
                data.account_id,
                data.opportunity_id,
                org.org_id,
            )

            payload = {
                "skill": data.skill,
                "model": data.model,
                "runtime_version": data.runtime_version,
            }
            input_refs = {"transcript_id": str(data.transcript_id)}
            if data.opportunity_id:
                input_refs["opportunity_id"] = str(data.opportunity_id)
            source_manifest = _source_manifest_from_transcript(transcript, data.opportunity_id)

            row = await conn.fetchrow(
                """
                SELECT * FROM public.enqueue_job(
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12::jsonb, $13::jsonb, $14::jsonb
                )
                """,
                org.context_token,
                data.account_id,
                data.transcript_id,
                data.opportunity_id,
                data.skill,
                data.skill_version,
                data.model,
                data.runtime_version,
                data.idempotency_key,
                data.max_attempts,
                config.WORKER_TIMEOUT_SECONDS,
                json.dumps(payload),
                json.dumps(input_refs),
                json.dumps(source_manifest),
            )
    except PostgresError as exc:
        if getattr(exc, "sqlstate", None) == "40901":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key reused with different scope",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc).split("\n")[0],
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue job",
        )

    # Fetch the full job record for the response.
    async with tenant_connection(request, org) as conn:
        job_row = await conn.fetchrow(
            "SELECT * FROM public.jobs WHERE id = $1 AND org_id = $2",
            row["job_id"],
            org.org_id,
        )
    if job_row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Enqueued job could not be retrieved",
        )
    return JobOut.from_record(job_row)


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(
    request: Request,
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> JobDetail:
    """Return a job and its append-only attempt history."""
    async with tenant_connection(request, org) as conn:
        job_row = await conn.fetchrow(
            "SELECT * FROM public.jobs WHERE id = $1 AND org_id = $2",
            job_id,
            org.org_id,
        )
        if job_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )
        attempt_rows = await conn.fetch(
            """
            SELECT * FROM public.job_attempts
            WHERE job_id = $1 AND org_id = $2
            ORDER BY attempt_number
            """,
            job_id,
            org.org_id,
        )
    return JobDetail(
        job=JobOut.from_record(job_row),
        attempts=[models.JobAttemptOut.from_record(r) for r in attempt_rows],
    )


@router.post("/jobs/{job_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    request: Request,
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> None:
    """Request cancellation of a queued or running job."""
    pool: asyncpg.Pool = request.app.state.hosted_user_pool
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT public.request_job_cancellation($1, $2)",
                org.context_token,
                job_id,
            )
    except PostgresError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc).split("\n")[0],
        ) from exc
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
