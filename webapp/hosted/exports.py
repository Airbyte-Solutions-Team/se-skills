"""Hosted export of an approved output (Slice 6B1).

Trust model
-----------
The browser supplies only bounded intent: a format enum (`md` or `pdf`) and an
idempotency key. Organization, actor identity, the exact current version, its
approval state, and the private Storage path of the artifact are all derived
server-side by `public.authorize_output_export`, which locks the output row so
the snapshot is atomic against a concurrent correction or approval.

The authorized version is immutable, so no database lock is held across the
Storage read or the PDF render: bytes for version A can never turn into bytes
for a later version B. The export audit event is appended only after those bytes
exist, so a Storage or renderer failure never records a successful export, and
the audit always names the version that was actually returned.

Reads go through the same user-scoped private Storage path the reader uses
(`app_storage`); no maintenance role, no service-role credential, no signed or
public URL, and nothing is persisted for the export itself.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from . import pdf_export, storage
from .auth import require_org, tenant_connection
from .models import OrgContext, OutputExportCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hosted", tags=["hosted"])

# SQLSTATEs raised by the export functions in migration 011.
_NOT_ACCESSIBLE = "SE001"
_STALE_VERSION = "SE002"
_IDEMPOTENCY_CONFLICT = "SE003"
_MALFORMED_CHAIN = "SE004"
_INVALID_INPUT = "SE005"
_NOT_APPROVED = "SE006"

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output not found")

_MEDIA_TYPES = {"md": "text/markdown; charset=utf-8", "pdf": "application/pdf"}

# Defensive response headers. An export is a download, never hosted HTML, and it
# must not be cached by an intermediary.
_EXPORT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


def _as_dict(value: Any) -> dict[str, Any]:
    """Return a JSONB function result as a dictionary."""
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    return {}


def _map_export_error(exc: asyncpg.PostgresError) -> HTTPException:
    """Translate an export-function SQLSTATE into a customer-safe HTTP error."""
    sqlstate = exc.sqlstate
    if sqlstate == _NOT_ACCESSIBLE:
        return _NOT_FOUND
    if sqlstate == _NOT_APPROVED:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approve the current version to export.",
        )
    if sqlstate == _STALE_VERSION:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This output changed since you loaded it. Refresh to see the current version.",
        )
    if sqlstate == _IDEMPOTENCY_CONFLICT:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request id was already used for a different export.",
        )
    if sqlstate == _MALFORMED_CHAIN:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This output's version history is inconsistent and cannot be exported.",
        )
    if sqlstate == _INVALID_INPUT:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid export request")
    logger.warning("Unexpected export database error: %s", type(exc).__name__)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not export this output"
    )


def _export_filename(output_id: uuid.UUID, ordinal: int, extension: str) -> str:
    """Deterministic, bounded filename built only from safe identifiers.

    Account, opportunity, and document titles are deliberately excluded: a
    downloaded filename ends up in shared folders, chat clients, and mail
    subjects, so it carries no customer-identifying text.
    """
    return f"output-{output_id}-v{ordinal}.{extension}"


async def _download_bytes(request: Request, org: OrgContext, path: str) -> bytes:
    """Read a private output object through the normal user-scoped Storage path."""
    backend = request.app.state.storage_backend
    chunks: list[bytes] = []
    stream = await backend.download(org.user.id, path, bucket=storage.OUTPUTS_BUCKET)
    async for chunk in stream:
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/accounts/{account_id}/outputs/{output_id}/exports")
async def create_output_export(
    request: Request,
    account_id: uuid.UUID,
    output_id: uuid.UUID,
    payload: OutputExportCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> Response:
    """Export the exact approved current version of an output as Markdown or PDF."""
    async with tenant_connection(request, org) as conn:
        try:
            raw = await conn.fetchval(
                "SELECT public.authorize_output_export($1, $2, $3, $4)",
                output_id,
                account_id,
                payload.format,
                payload.request_id,
            )
        except asyncpg.PostgresError as exc:
            raise _map_export_error(exc) from None
    snapshot: dict[str, Any] = _as_dict(raw)
    raw_version = snapshot.get("output_version_id")
    version_id = uuid.UUID(str(raw_version)) if raw_version else None
    storage_path = str(snapshot["content_storage_path"])
    ordinal = int(snapshot.get("version_ordinal") or 0)

    try:
        markdown_bytes = await _download_bytes(request, org, storage_path)
    except storage.ObjectNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Output content not found"
        ) from None
    except storage.StorageAuthError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied") from None
    except storage.StorageError as exc:
        logger.warning("Export storage read failed for output %s: %s", output_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read the approved output. Please try again.",
        ) from None

    if payload.format == "md":
        # The approved Markdown is returned byte for byte: the reviewed artifact
        # is the record, so it is never re-rendered, reformatted, or re-escaped.
        body = markdown_bytes
        extension = "md"
    else:
        try:
            # Strict decoding: bytes that are not valid UTF-8 are not the reviewed
            # document, and replacing the invalid sequences would put text into the
            # PDF that nobody approved. So the export fails instead, and Markdown
            # still returns the stored bytes untouched.
            markdown_text = markdown_bytes.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Export PDF decode failed for output %s", output_id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This output could not be read as text for PDF export. Export as Markdown instead.",
            ) from None
        try:
            body = pdf_export.render_markdown_pdf(markdown_text)
        except pdf_export.PdfFontCoverageError as exc:
            # Failing closed keeps the PDF honest: the reviewed characters are
            # never substituted, and Markdown still carries them exactly.
            logger.warning("Export PDF font coverage gap for output %s: %s", output_id, type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This output has characters the PDF font cannot render. Export as Markdown to keep the exact text.",
            ) from None
        except pdf_export.PdfRenderError as exc:
            logger.warning("Export PDF render failed for output %s: %s", output_id, type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not render this output as a PDF.",
            ) from None
        extension = "pdf"

    async with tenant_connection(request, org) as conn:
        try:
            await conn.fetchval(
                "SELECT public.record_output_export($1, $2, $3, $4, $5)",
                output_id,
                account_id,
                version_id,
                payload.format,
                payload.request_id,
            )
        except asyncpg.PostgresError as exc:
            raise _map_export_error(exc) from None

    filename = _export_filename(output_id, ordinal, extension)
    return Response(
        content=body,
        media_type=_MEDIA_TYPES[payload.format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **_EXPORT_HEADERS,
        },
    )
