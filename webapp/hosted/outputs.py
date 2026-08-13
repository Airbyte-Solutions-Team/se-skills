"""Organization-scoped output list/detail/content API routes."""
from __future__ import annotations

import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from . import storage
from .auth import require_org
from .models import OrgContext
from .storage import get_backend

router = APIRouter(prefix="/api/hosted", tags=["hosted"])


def _output_from_record(record: asyncpg.Record) -> dict[str, Any]:
    """Return a public, sanitized output detail dictionary."""
    sidecar = record.get("sidecar") or {}
    if isinstance(sidecar, str):
        import json

        sidecar = json.loads(sidecar)
    return {
        "id": str(record["id"]),
        "org_id": str(record["org_id"]),
        "job_id": str(record["job_id"]),
        "account_id": str(record["account_id"]),
        "opportunity_id": str(record["opportunity_id"]) if record.get("opportunity_id") else None,
        "transcript_id": str(record["transcript_id"]),
        "title": record.get("title"),
        "validation_status": record.get("validation_status") or "unvalidated",
        "validation_errors": sidecar.get("validation_errors") if isinstance(sidecar, dict) else [],
        "skill": record["skill"],
        "skill_version": record.get("skill_version"),
        "model": record.get("model"),
        "runtime_version": record.get("runtime_version"),
        "generated_at": record["generated_at"].isoformat() if record.get("generated_at") else None,
        "created_at": record["created_at"].isoformat() if record.get("created_at") else None,
    }


@router.get("/accounts/{account_id}/outputs")
async def list_account_outputs(
    request: Request,
    account_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, list[Any]]:
    """List generated outputs for the active organization's account."""
    from .auth import tenant_connection

    async with tenant_connection(request, org) as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, job_id, account_id, opportunity_id, transcript_id,
                   title, validation_status, sidecar, skill, skill_version, model,
                   runtime_version, generated_at, created_at
            FROM public.outputs
            WHERE org_id = $1 AND account_id = $2
            ORDER BY created_at DESC
            """,
            org.org_id,
            account_id,
        )
    return {"outputs": [_output_from_record(r) for r in rows]}


@router.get("/accounts/{account_id}/outputs/{output_id}")
async def get_output(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Return output metadata for a single generated output."""
    from .auth import tenant_connection

    async with tenant_connection(request, org) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, org_id, job_id, account_id, opportunity_id, transcript_id,
                   title, validation_status, sidecar, skill, skill_version, model,
                   runtime_version, generated_at, created_at
            FROM public.outputs
            WHERE org_id = $1 AND account_id = $2 AND id = $3
            """,
            org.org_id,
            account_id,
            output_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output not found")
    return {"output": _output_from_record(row)}


@router.get("/accounts/{account_id}/outputs/{output_id}/content")
async def get_output_content(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Return sanitized Markdown content for a generated output."""
    import md_render
    from .auth import tenant_connection

    async with tenant_connection(request, org) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, content_storage_path, validation_status
            FROM public.outputs
            WHERE org_id = $1 AND account_id = $2 AND id = $3
            """,
            org.org_id,
            account_id,
            output_id,
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output not found")

    storage_path: str = row["content_storage_path"]
    backend = request.app.state.storage_backend
    chunks: list[bytes] = []
    try:
        stream = await backend.download(org.user.id, storage_path, bucket=storage.OUTPUTS_BUCKET)
        async for chunk in stream:
            chunks.append(chunk)
    except storage.ObjectNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output content not found")
    except storage.StorageAuthError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    markdown = b"".join(chunks).decode("utf-8", errors="replace")
    return {
        "id": str(output_id),
        "validation_status": row["validation_status"] or "unvalidated",
        "html": md_render.markdown_to_body_html(markdown),
        "markdown": markdown,
    }
