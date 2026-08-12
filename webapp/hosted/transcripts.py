"""Organization-scoped transcript upload/list/download/delete APIs."""
from __future__ import annotations

import logging
import re
import uuid
from io import BytesIO
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

from . import config, models, storage
from .auth import get_token_from_request, require_org, tenant_connection
from .models import OrgContext, TranscriptList, TranscriptOut

router = APIRouter(prefix="/api/hosted", tags=["hosted"])

_ALLOWED_EXTENSIONS = {".txt", ".md", ".vtt", ".srt"}
_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".vtt": "text/vtt",
    ".srt": "text/srt",
}


class TranscriptValidationError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _safe_filename(name: str) -> str:
    """Return a safe original filename for display/download headers.

    Strips path components, removes control characters, and collapses runs of
    dangerous punctuation. The result is never used as a filesystem or storage
    path; it is only returned as attachment metadata.
    """
    if not name:
        raise TranscriptValidationError("Filename is required")
    # Drop any path components.
    name = name.split("/")[-1].split("\\")[-1]
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", name).strip(". ")
    if not name:
        name = "transcript"
    if len(name) > 200:
        name = name[:200].rsplit(".", 1)[0] if "." in name[:200] else name[:200]
    return name or "transcript"


def _content_type_for(filename: str) -> str:
    """Return a safe content type based solely on the file extension."""
    lower = filename.lower()
    for ext in _EXTENSION_CONTENT_TYPES:
        if lower.endswith(ext):
            return _EXTENSION_CONTENT_TYPES[ext]
    raise TranscriptValidationError("Unsupported file type")


def _read_and_validate(file: UploadFile) -> tuple[bytes, str]:
    """Read the upload, enforce size, type, and content safety rules.

    Returns the validated bytes and the safe original filename.
    """
    original = _safe_filename(file.filename or "")
    suffix = _file_extension(original)
    if suffix not in _ALLOWED_EXTENSIONS:
        raise TranscriptValidationError("Only .txt, .md, .vtt, and .srt files are allowed")

    max_bytes = config.TRANSCRIPT_MAX_BYTES
    buffer = BytesIO()
    total = 0
    while chunk := file.file.read(65536):
        total += len(chunk)
        if total > max_bytes:
            raise TranscriptValidationError(f"File exceeds {max_bytes} bytes")
        buffer.write(chunk)

    data = buffer.getvalue()
    if not data:
        raise TranscriptValidationError("File is empty")
    if b"\x00" in data:
        raise TranscriptValidationError("File contains NUL bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TranscriptValidationError("File is not valid UTF-8") from exc

    return data, original


def _file_extension(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename[filename.rfind("."):].lower()


def _storage_path(
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None,
    transcript_id: uuid.UUID,
) -> str:
    """Build the organization-scoped object key from trusted IDs only."""
    parts = [str(org_id), str(account_id)]
    if opportunity_id:
        parts.append(str(opportunity_id))
    parts.extend(["transcripts", str(transcript_id)])
    return "/".join(parts)


async def _require_account_in_org(
    conn: asyncpg.Connection,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    row = await conn.fetchrow(
        "SELECT id FROM public.accounts WHERE id = $1 AND org_id = $2",
        account_id,
        org_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )


async def _require_opportunity_in_account(
    conn: asyncpg.Connection,
    opportunity_id: uuid.UUID,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    row = await conn.fetchrow(
        "SELECT id FROM public.opportunities WHERE id = $1 AND account_id = $2 AND org_id = $3",
        opportunity_id,
        account_id,
        org_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Opportunity not found",
        )


def _transcript_from_record(record: asyncpg.Record) -> TranscriptOut:
    return TranscriptOut.from_record(record)


@router.get("/accounts/{account_id}/transcripts", response_model=TranscriptList)
async def list_account_transcripts(
    request: Request,
    account_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> TranscriptList:
    async with tenant_connection(request, org) as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, account_id, opportunity_id, original_filename,
                   size_bytes, mime_type, uploaded_by, created_at, updated_at
            FROM public.transcripts
            WHERE account_id = $1 AND org_id = $2 AND opportunity_id IS NULL
            ORDER BY created_at DESC
            """,
            account_id,
            org.org_id,
        )
    return TranscriptList(transcripts=[_transcript_from_record(r) for r in rows])


@router.post("/accounts/{account_id}/transcripts", response_model=TranscriptOut, status_code=status.HTTP_201_CREATED)
async def upload_account_transcript(
    request: Request,
    account_id: uuid.UUID,
    file: UploadFile,
    org: Annotated[OrgContext, Depends(require_org)],
) -> TranscriptOut:
    data, original_filename = _read_and_validate(file)
    content_type = _content_type_for(original_filename)
    transcript_id = uuid.uuid4()
    storage_path = _storage_path(org.org_id, account_id, None, transcript_id)
    token = get_token_from_request(request)

    backend = request.app.state.storage_backend
    async with tenant_connection(request, org) as conn:
        await _require_account_in_org(conn, account_id, org.org_id)
        try:
            await backend.upload(token, storage_path, data, content_type)
            row = await conn.fetchrow(
                """
                INSERT INTO public.transcripts
                    (id, org_id, account_id, storage_path, original_filename,
                     size_bytes, mime_type, uploaded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id, org_id, account_id, opportunity_id, original_filename,
                          size_bytes, mime_type, uploaded_by, created_at, updated_at
                """,
                transcript_id,
                org.org_id,
                account_id,
                storage_path,
                original_filename,
                len(data),
                content_type,
                org.user.id,
            )
        except Exception as upload_or_insert_exc:
            # Compensating cleanup: the object is not listable until metadata
            # creation succeeds, so remove the orphaned object on failure.
            try:
                await backend.delete(token, storage_path)
            except Exception as cleanup_exc:
                logger.warning("Failed to clean up orphaned storage object %s: %s", storage_path, cleanup_exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload failed",
            ) from upload_or_insert_exc

    return _transcript_from_record(row)


@router.get("/accounts/{account_id}/transcripts/{transcript_id}/download")
async def download_account_transcript(
    request: Request,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> StreamingResponse:
    return await _download_transcript(request, account_id, None, transcript_id, org)


@router.delete("/accounts/{account_id}/transcripts/{transcript_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_transcript(
    request: Request,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> None:
    await _delete_transcript(request, account_id, None, transcript_id, org)


@router.get("/accounts/{account_id}/opportunities/{opportunity_id}/transcripts", response_model=TranscriptList)
async def list_opportunity_transcripts(
    request: Request,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> TranscriptList:
    async with tenant_connection(request, org) as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, account_id, opportunity_id, original_filename,
                   size_bytes, mime_type, uploaded_by, created_at, updated_at
            FROM public.transcripts
            WHERE account_id = $1 AND opportunity_id = $2 AND org_id = $3
            ORDER BY created_at DESC
            """,
            account_id,
            opportunity_id,
            org.org_id,
        )
    return TranscriptList(transcripts=[_transcript_from_record(r) for r in rows])


@router.post(
    "/accounts/{account_id}/opportunities/{opportunity_id}/transcripts",
    response_model=TranscriptOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_opportunity_transcript(
    request: Request,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    file: UploadFile,
    org: Annotated[OrgContext, Depends(require_org)],
) -> TranscriptOut:
    data, original_filename = _read_and_validate(file)
    content_type = _content_type_for(original_filename)
    transcript_id = uuid.uuid4()
    storage_path = _storage_path(org.org_id, account_id, opportunity_id, transcript_id)
    token = get_token_from_request(request)

    backend = request.app.state.storage_backend
    async with tenant_connection(request, org) as conn:
        await _require_opportunity_in_account(conn, opportunity_id, account_id, org.org_id)
        try:
            await backend.upload(token, storage_path, data, content_type)
            row = await conn.fetchrow(
                """
                INSERT INTO public.transcripts
                    (id, org_id, account_id, opportunity_id, storage_path, original_filename,
                     size_bytes, mime_type, uploaded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id, org_id, account_id, opportunity_id, original_filename,
                          size_bytes, mime_type, uploaded_by, created_at, updated_at
                """,
                transcript_id,
                org.org_id,
                account_id,
                opportunity_id,
                storage_path,
                original_filename,
                len(data),
                content_type,
                org.user.id,
            )
        except Exception as upload_or_insert_exc:
            try:
                await backend.delete(token, storage_path)
            except Exception as cleanup_exc:
                logger.warning("Failed to clean up orphaned storage object %s: %s", storage_path, cleanup_exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload failed",
            ) from upload_or_insert_exc

    return _transcript_from_record(row)


@router.get("/accounts/{account_id}/opportunities/{opportunity_id}/transcripts/{transcript_id}/download")
async def download_opportunity_transcript(
    request: Request,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    transcript_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> StreamingResponse:
    return await _download_transcript(request, account_id, opportunity_id, transcript_id, org)


@router.delete(
    "/accounts/{account_id}/opportunities/{opportunity_id}/transcripts/{transcript_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_opportunity_transcript(
    request: Request,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    transcript_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> None:
    await _delete_transcript(request, account_id, opportunity_id, transcript_id, org)


async def _load_transcript(
    conn: asyncpg.Connection,
    transcript_id: uuid.UUID,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None,
    org_id: uuid.UUID,
) -> asyncpg.Record:
    """Fetch a transcript row guarded by account/opportunity/org membership."""
    if opportunity_id is None:
        row = await conn.fetchrow(
            """
            SELECT id, org_id, account_id, opportunity_id, original_filename,
                   size_bytes, mime_type, uploaded_by, created_at, updated_at, storage_path
            FROM public.transcripts
            WHERE id = $1 AND account_id = $2 AND org_id = $3 AND opportunity_id IS NULL
            """,
            transcript_id,
            account_id,
            org_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT id, org_id, account_id, opportunity_id, original_filename,
                   size_bytes, mime_type, uploaded_by, created_at, updated_at, storage_path
            FROM public.transcripts
            WHERE id = $1 AND account_id = $2 AND opportunity_id = $3 AND org_id = $4
            """,
            transcript_id,
            account_id,
            opportunity_id,
            org_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transcript not found",
        )
    return row


async def _download_transcript(
    request: Request,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None,
    transcript_id: uuid.UUID,
    org: OrgContext,
) -> StreamingResponse:
    async with tenant_connection(request, org) as conn:
        if opportunity_id is not None:
            await _require_opportunity_in_account(conn, opportunity_id, account_id, org.org_id)
        else:
            await _require_account_in_org(conn, account_id, org.org_id)
        row = await _load_transcript(conn, transcript_id, account_id, opportunity_id, org.org_id)

    token = get_token_from_request(request)
    backend = request.app.state.storage_backend
    try:
        data, _ = await backend.download(token, row["storage_path"])
    except storage.ObjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transcript object not found",
        ) from exc
    except storage.StorageAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Storage access denied",
        ) from exc

    filename = _safe_filename(row["original_filename"])
    content_type = row["mime_type"]
    return StreamingResponse(
        BytesIO(data),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _delete_transcript(
    request: Request,
    account_id: uuid.UUID,
    opportunity_id: uuid.UUID | None,
    transcript_id: uuid.UUID,
    org: OrgContext,
) -> None:
    token = get_token_from_request(request)
    async with tenant_connection(request, org) as conn:
        if opportunity_id is not None:
            await _require_opportunity_in_account(conn, opportunity_id, account_id, org.org_id)
        else:
            await _require_account_in_org(conn, account_id, org.org_id)
        row = await _load_transcript(conn, transcript_id, account_id, opportunity_id, org.org_id)
        storage_path = row["storage_path"]

    # Delete the storage object first. If this fails, metadata is unchanged and
    # the transcript remains listable; no partial-success state.
    backend = request.app.state.storage_backend
    try:
        await backend.delete(token, storage_path)
    except storage.ObjectNotFound:
        # Already gone; continue to clean up metadata.
        pass
    except storage.StorageAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Storage access denied",
        ) from exc
    except storage.StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete transcript from storage",
        ) from exc

    # Then delete metadata. If this fails, the storage object is gone and an
    # operator or retry path is needed; the API reports failure rather than
    # claiming success.
    async with tenant_connection(request, org) as conn:
        await conn.execute(
            "DELETE FROM public.transcripts WHERE id = $1 AND org_id = $2",
            transcript_id,
            org.org_id,
        )
