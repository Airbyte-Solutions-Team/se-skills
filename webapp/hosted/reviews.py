"""Hosted output review, correction, approval, and audit API routes.

Trust model
-----------
The browser only ever supplies user-authorable review data: which version it is
acting on, the replacement Markdown, a change summary, a comment body, and an
idempotency key. Organization, actor identity, Storage paths, provenance,
validation state, and version identity are derived server-side.

Every mutation goes through a narrow `SECURITY DEFINER` function that re-derives
the actor from the signed tenant context, verifies active membership, locks the
output row, enforces the linear correction chain, and writes the review/version
row together with its audit event in one transaction. `app_user` has no direct
INSERT/UPDATE/DELETE on `outputs`, `output_versions`, `reviews`, or
`audit_events`.

A correction reserves its version id and server-generated Storage path *before*
uploading, so a Storage-success/DB-failure path always leaves durable,
recoverable evidence instead of an untracked private object.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

import md_render
import output_schema

from . import storage
from .auth import require_org, tenant_connection
from .models import (
    OrgContext,
    OutputApprovalCreate,
    OutputCommentCreate,
    OutputCorrectionCreate,
    OutputCorrectionPreview,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hosted", tags=["hosted"])

# SQLSTATEs raised by the review functions in migration 009.
_NOT_ACCESSIBLE = "SE001"
_STALE_VERSION = "SE002"
_IDEMPOTENCY_CONFLICT = "SE003"
_MALFORMED_CHAIN = "SE004"
_INVALID_INPUT = "SE005"

_MAX_CORRECTION_BYTES = 400_000

# Hosted review applies the generation-time contract of the skill that produced
# the output. Only skills whose hosted producer path exists may be corrected, so
# an unexpected skill fails closed instead of being validated by a weaker schema.
_REVIEWABLE_SKILLS = frozenset({"post-call"})

_GENERATED_VERSION_REF = "generated"

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output not found")


def _map_db_error(exc: asyncpg.PostgresError) -> HTTPException:
    """Translate a review-function SQLSTATE into a customer-safe HTTP error."""
    sqlstate = exc.sqlstate
    if sqlstate == _NOT_ACCESSIBLE:
        return _NOT_FOUND
    if sqlstate == _STALE_VERSION:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This output changed since you loaded it. Refresh to see the current version.",
        )
    if sqlstate == _IDEMPOTENCY_CONFLICT:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request id was already used for a different submission.",
        )
    if sqlstate == _MALFORMED_CHAIN:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This output's version history is inconsistent and cannot be modified.",
        )
    if sqlstate == _INVALID_INPUT:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid review request")
    logger.warning("Unexpected review database error: %s", type(exc).__name__)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record the review action"
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Return a JSONB function result as a dictionary."""
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    return {}


def _sidecar_dict(value: Any) -> dict[str, Any]:
    data = _as_dict(value)
    return data if isinstance(data, dict) else {}


async def _load_output(
    conn: asyncpg.Connection, org: OrgContext, account_id: uuid.UUID, output_id: uuid.UUID
) -> asyncpg.Record:
    """Return the reviewable output row or raise an indistinguishable 404."""
    row = await conn.fetchrow(
        """
        SELECT id, org_id, job_id, account_id, opportunity_id, transcript_id, requester_id,
               title, validation_status, sidecar, skill, skill_version, model,
               runtime_version, generated_at, created_at, content_storage_path
        FROM public.outputs
        WHERE org_id = $1 AND account_id = $2 AND id = $3
          AND validation_status = 'valid' AND tombstoned_at IS NULL
        """,
        org.org_id,
        account_id,
        output_id,
    )
    if row is None:
        raise _NOT_FOUND
    return row


def _ordered_chain(rows: list[asyncpg.Record]) -> list[asyncpg.Record]:
    """Return corrections ordered from the first correction to the current one.

    The database enforces a single root and a single child per version, but a
    pre-existing malformed chain must fail closed rather than pick a winner.
    """
    if not rows:
        return []
    by_previous: dict[uuid.UUID | None, list[asyncpg.Record]] = {}
    for row in rows:
        by_previous.setdefault(row["previous_version_id"], []).append(row)
    for children in by_previous.values():
        if len(children) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This output's version history is inconsistent and cannot be modified.",
            )
    ordered: list[asyncpg.Record] = []
    cursor: uuid.UUID | None = None
    while True:
        children = by_previous.get(cursor)
        if not children:
            break
        row = children[0]
        ordered.append(row)
        cursor = row["id"]
    if len(ordered) != len(rows):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This output's version history is inconsistent and cannot be modified.",
        )
    return ordered


def _version_payload(row: asyncpg.Record, index: int, emails: dict[uuid.UUID, str]) -> dict[str, Any]:
    sidecar = _sidecar_dict(row["sidecar"])
    return {
        "id": str(row["id"]),
        "label": f"Correction {index}",
        "kind": "correction",
        "previous_version_id": str(row["previous_version_id"]) if row["previous_version_id"] else None,
        "change_summary": row["change_summary"],
        "created_by": str(row["created_by"]) if row["created_by"] else None,
        "created_by_email": emails.get(row["created_by"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "validation_status": sidecar.get("validation_status") or "unvalidated",
        "origin": sidecar.get("origin") or "human_correction",
    }


async def _review_state(
    conn: asyncpg.Connection, org: OrgContext, output: asyncpg.Record
) -> dict[str, Any]:
    """Build the review/version/activity read model for one output."""
    version_rows = await conn.fetch(
        """
        SELECT id, previous_version_id, change_summary, created_by, created_at, sidecar
        FROM public.output_versions
        WHERE org_id = $1 AND output_id = $2
        """,
        org.org_id,
        output["id"],
    )
    review_rows = await conn.fetch(
        """
        SELECT id, action, output_version_id, previous_version_id, user_id, comment, created_at
        FROM public.reviews
        WHERE org_id = $1 AND output_id = $2
        ORDER BY created_at, id
        """,
        org.org_id,
        output["id"],
    )

    actor_ids = {r["created_by"] for r in version_rows if r["created_by"]}
    actor_ids |= {r["user_id"] for r in review_rows if r["user_id"]}
    emails: dict[uuid.UUID, str] = {}
    if actor_ids:
        for record in await conn.fetch(
            "SELECT id, email FROM public.users WHERE id = ANY($1::uuid[])", list(actor_ids)
        ):
            emails[record["id"]] = record["email"]

    ordered = _ordered_chain(list(version_rows))
    generated_sidecar = _sidecar_dict(output["sidecar"])
    versions: list[dict[str, Any]] = [
        {
            "id": None,
            "label": "Generated (V0)",
            "kind": "generated",
            "previous_version_id": None,
            "change_summary": None,
            "created_by": None,
            "created_by_email": None,
            "created_at": output["generated_at"].isoformat() if output["generated_at"] else None,
            "validation_status": output["validation_status"] or "unvalidated",
            "origin": "model_generated",
            "model": output["model"],
            "skill": output["skill"],
            "skill_version": output["skill_version"],
            "source_coverage": generated_sidecar.get("source_coverage"),
        }
    ]
    versions.extend(
        _version_payload(row, index + 1, emails) for index, row in enumerate(ordered)
    )

    current_version_id = str(ordered[-1]["id"]) if ordered else None
    approved = any(
        r["action"] == "approve"
        and (str(r["output_version_id"]) if r["output_version_id"] else None) == current_version_id
        for r in review_rows
    )

    activity = [
        {
            "id": str(r["id"]),
            "action": r["action"],
            "output_version_id": str(r["output_version_id"]) if r["output_version_id"] else None,
            "previous_version_id": str(r["previous_version_id"]) if r["previous_version_id"] else None,
            "user_id": str(r["user_id"]) if r["user_id"] else None,
            "user_email": emails.get(r["user_id"]),
            "comment": r["comment"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in review_rows
    ]

    return {
        "output": {
            "id": str(output["id"]),
            "account_id": str(output["account_id"]),
            "title": output["title"],
            "skill": output["skill"],
            "validation_status": output["validation_status"] or "unvalidated",
            "generated_at": output["generated_at"].isoformat() if output["generated_at"] else None,
        },
        "versions": versions,
        "current_version_id": current_version_id,
        "review_state": "approved" if approved else "needs_review",
        "activity": activity,
    }


async def _download_text(request: Request, org: OrgContext, path: str, bucket: str) -> str:
    """Download a private object and decode it as UTF-8 text."""
    backend = request.app.state.storage_backend
    chunks: list[bytes] = []
    stream = await backend.download(org.user.id, path, bucket=bucket)
    async for chunk in stream:
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _transcript_text(
    request: Request, conn: asyncpg.Connection, org: OrgContext, output: asyncpg.Record
) -> str | None:
    """Load the generation-time transcript text through the private Storage boundary.

    The path comes from trusted database state, never from the browser. A missing
    transcript object yields `None`; the caller decides whether the validation
    contract can still be applied.
    """
    row = await conn.fetchrow(
        """
        SELECT storage_path FROM public.transcripts
        WHERE org_id = $1 AND account_id = $2 AND id = $3
        """,
        org.org_id,
        output["account_id"],
        output["transcript_id"],
    )
    if row is None:
        return None
    try:
        return await _download_text(request, org, row["storage_path"], storage.DEFAULT_BUCKET)
    except storage.StorageError:
        return None


def _validate_correction(
    output: asyncpg.Record, markdown: str, transcript_text: str
) -> output_schema.OutputMetadata:
    """Validate replacement Markdown with the authoritative generation-time contract.

    `mode` comes from the original server-authored sidecar and `transcript_text`
    from the private transcript object, so a human correction is judged by
    exactly the same `output_schema.parse_output` semantics as the generated
    output it replaces.
    """
    sidecar = _sidecar_dict(output["sidecar"])
    mode = sidecar.get("mode") or "full"
    return output_schema.parse_output(
        output["skill"],
        markdown,
        mode=mode,
        transcript_text=transcript_text,
    )


@router.get("/accounts/{account_id}/outputs/{output_id}/review")
async def get_output_review(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Return version chain, review activity, and derived approval state."""
    async with tenant_connection(request, org) as conn:
        output = await _load_output(conn, org, account_id, output_id)
        return await _review_state(conn, org, output)


@router.get("/accounts/{account_id}/outputs/{output_id}/versions/{version_ref}/content")
async def get_output_version_content(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    version_ref: str,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Return rendered content for one version of this output.

    `version_ref` is either `generated` (the immutable V0 produced by the model)
    or the identifier of a correction. Both resolve their Storage path from
    trusted database state only.
    """
    async with tenant_connection(request, org) as conn:
        output = await _load_output(conn, org, account_id, output_id)
        if version_ref == _GENERATED_VERSION_REF:
            row = None
            storage_path = output["content_storage_path"]
            sidecar = _sidecar_dict(output["sidecar"])
        else:
            try:
                version_id = uuid.UUID(version_ref)
            except ValueError:
                raise _NOT_FOUND from None
            row = await conn.fetchrow(
                """
                SELECT id, content_storage_path, sidecar
                FROM public.output_versions
                WHERE org_id = $1 AND output_id = $2 AND id = $3
                """,
                org.org_id,
                output["id"],
                version_id,
            )
            if row is None:
                raise _NOT_FOUND
            storage_path = row["content_storage_path"]
            sidecar = _sidecar_dict(row["sidecar"])

    try:
        markdown = await _download_text(request, org, storage_path, storage.OUTPUTS_BUCKET)
    except storage.ObjectNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Output content not found"
        ) from None
    except storage.StorageAuthError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied") from None

    return {
        "id": _GENERATED_VERSION_REF if row is None else str(row["id"]),
        "output_id": str(output["id"]),
        "validation_status": sidecar.get("validation_status") or "unvalidated",
        "html": md_render.markdown_to_body_html(markdown),
        "markdown": markdown,
    }


@router.post("/accounts/{account_id}/outputs/{output_id}/preview")
async def preview_output_correction(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    payload: OutputCorrectionPreview,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Render draft correction Markdown through the shared sanitizing renderer.

    Nothing is stored: this is the editor preview, so the reviewer sees exactly
    the sanitized HTML the reader would show before submitting a correction.
    """
    if len(payload.markdown.encode("utf-8")) > _MAX_CORRECTION_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Correction is too large"
        )
    async with tenant_connection(request, org) as conn:
        await _load_output(conn, org, account_id, output_id)
    return {"html": md_render.markdown_to_body_html(payload.markdown)}


@router.post("/accounts/{account_id}/outputs/{output_id}/comments", status_code=201)
async def add_output_comment(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    payload: OutputCommentCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Append a plain-text comment tied to an exact version of this output."""
    async with tenant_connection(request, org) as conn:
        try:
            raw = await conn.fetchval(
                "SELECT public.add_output_comment($1, $2, $3, $4, $5)",
                output_id,
                account_id,
                payload.target_version_id,
                payload.body,
                payload.request_id,
            )
        except asyncpg.PostgresError as exc:
            raise _map_db_error(exc) from None
        result = _as_dict(raw)
        output = await _load_output(conn, org, account_id, output_id)
        state = await _review_state(conn, org, output)
    return {"comment": result, "review": state}


@router.post("/accounts/{account_id}/outputs/{output_id}/approvals", status_code=201)
async def approve_output(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    payload: OutputApprovalCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Approve the exact current version of this output."""
    async with tenant_connection(request, org) as conn:
        try:
            raw = await conn.fetchval(
                "SELECT public.approve_output_version($1, $2, $3, $4)",
                output_id,
                account_id,
                payload.target_version_id,
                payload.request_id,
            )
        except asyncpg.PostgresError as exc:
            raise _map_db_error(exc) from None
        result = _as_dict(raw)
        output = await _load_output(conn, org, account_id, output_id)
        state = await _review_state(conn, org, output)
    return {"approval": result, "review": state}


async def _abandon_reservation(
    request: Request,
    org: OrgContext,
    reservation_id: uuid.UUID,
    *,
    object_deleted: bool,
    reason: str,
) -> None:
    """Close a reservation that will never become a version.

    `object_deleted=False` deliberately leaves durable `orphaned` evidence so the
    private object can be retried by the cleanup path instead of leaking.
    """
    try:
        async with tenant_connection(request, org) as conn:
            await conn.fetchval(
                "SELECT public.abandon_output_correction($1, $2, $3)",
                reservation_id,
                object_deleted,
                reason,
            )
    except (asyncpg.PostgresError, OSError) as exc:
        # Ledger bookkeeping must never mask the original failure, and the
        # reservation row stays `pending`, which the cleanup path also treats as
        # possibly-orphaned evidence.
        logger.error(
            "Failed to record abandoned correction %s (object_deleted=%s): %s",
            reservation_id,
            object_deleted,
            type(exc).__name__,
        )


async def _compensate_failed_commit(
    request: Request,
    org: OrgContext,
    *,
    output_id: uuid.UUID,
    reservation_id: uuid.UUID,
    storage_path: str,
    skip_delete: bool,
) -> None:
    """Undo an uncommitted correction upload and close out its reservation.

    The uploaded object is deleted so a failed commit leaves no orphan. When the
    delete itself fails the reservation is marked `orphaned`, which is durable,
    retryable cleanup evidence rather than a silent leak.
    """
    deleted = True
    if not skip_delete:
        try:
            await request.app.state.storage_backend.delete(
                org.user.id, storage_path, bucket=storage.OUTPUTS_BUCKET
            )
        except storage.ObjectNotFound:
            deleted = True
        except storage.StorageError as cleanup_exc:
            deleted = False
            logger.error(
                "Could not delete uncommitted correction object for output %s: %s",
                output_id,
                type(cleanup_exc).__name__,
            )
    await _abandon_reservation(
        request, org, reservation_id, object_deleted=deleted, reason="commit_failed"
    )


@router.post("/accounts/{account_id}/outputs/{output_id}/corrections", status_code=201)
async def submit_output_correction(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    payload: OutputCorrectionCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> dict[str, Any]:
    """Validate and persist a human correction as a new immutable version.

    Order of operations: validate with the authoritative contract (no side
    effects on failure) → reserve version id and server-generated Storage path →
    upload the private object → commit version + review + audit in one
    transaction. Any failure after the upload deletes the object, or records
    recoverable `orphaned` evidence when the delete itself fails.
    """
    markdown = payload.markdown
    markdown_bytes = markdown.encode("utf-8")
    if len(markdown_bytes) > _MAX_CORRECTION_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Correction is too large"
        )

    async with tenant_connection(request, org) as conn:
        output = await _load_output(conn, org, account_id, output_id)
        if output["skill"] not in _REVIEWABLE_SKILLS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Corrections are not supported for this output type.",
            )
        transcript_text = await _transcript_text(request, conn, org, output)

    # Validating without the generation-time transcript would silently weaken the
    # contract (source coverage in particular), so a missing transcript fails the
    # request instead.
    if transcript_text is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load the original call transcript needed to check this correction.",
        )

    metadata = _validate_correction(output, markdown, transcript_text)
    if not metadata.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Correction does not satisfy the output contract",
                "validation_errors": list(metadata.validation_errors)[:50],
            },
        )

    payload_hash = hashlib.sha256(
        markdown_bytes + b"\x1f" + (payload.change_summary or "").encode("utf-8")
    ).hexdigest()

    async with tenant_connection(request, org) as conn:
        try:
            raw = await conn.fetchval(
                "SELECT public.reserve_output_correction($1, $2, $3, $4, $5)",
                output_id,
                account_id,
                payload.base_version_id,
                payload.request_id,
                payload_hash,
            )
        except asyncpg.PostgresError as exc:
            raise _map_db_error(exc) from None
    reservation = _as_dict(raw)
    reservation_id = uuid.UUID(str(reservation["reservation_id"]))
    storage_path = str(reservation["content_storage_path"])
    already_committed = reservation.get("state") == "committed"

    async def _stream() -> Any:
        yield markdown_bytes

    if not already_committed:
        backend = request.app.state.storage_backend
        try:
            await backend.upload(
                org.user.id,
                storage_path,
                _stream(),
                "text/markdown; charset=utf-8",
                bucket=storage.OUTPUTS_BUCKET,
            )
        except storage.StorageError as exc:
            logger.warning("Correction upload failed for output %s: %s", output_id, type(exc).__name__)
            await _abandon_reservation(
                request, org, reservation_id, object_deleted=True, reason="upload_failed"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not store the correction. Please try again.",
            ) from None

    sidecar = {
        "origin": "human_correction",
        "skill": output["skill"],
        "skill_version": output["skill_version"],
        "validation_status": "valid",
        "validation_errors": [],
        "content_hash": hashlib.sha256(markdown_bytes).hexdigest(),
        "payload_hash": payload_hash,
        "content_length": len(markdown_bytes),
        "mode": metadata.mode,
        "schema_version": metadata.schema_version,
    }

    try:
        async with tenant_connection(request, org) as conn:
            raw_commit = await conn.fetchval(
                "SELECT public.commit_output_correction($1, $2, $3::jsonb)",
                reservation_id,
                payload.change_summary,
                json.dumps(sidecar),
            )
    except asyncpg.PostgresError as exc:
        await _compensate_failed_commit(
            request,
            org,
            output_id=output_id,
            reservation_id=reservation_id,
            storage_path=storage_path,
            skip_delete=already_committed,
        )
        raise _map_db_error(exc) from None
    except OSError as exc:
        logger.error(
            "Correction commit failed for output %s: %s", output_id, type(exc).__name__
        )
        await _compensate_failed_commit(
            request,
            org,
            output_id=output_id,
            reservation_id=reservation_id,
            storage_path=storage_path,
            skip_delete=already_committed,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not record the correction",
        ) from None

    committed = _as_dict(raw_commit)

    async with tenant_connection(request, org) as conn:
        output = await _load_output(conn, org, account_id, output_id)
        state = await _review_state(conn, org, output)

    return {"correction": committed, "review": state}

