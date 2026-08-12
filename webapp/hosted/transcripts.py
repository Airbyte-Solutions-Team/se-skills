"""Organization-scoped transcript upload/list/download/delete APIs."""
from __future__ import annotations

import codecs
import logging
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse

from . import config, models, storage
from .auth import require_org, tenant_connection
from .models import OrgContext, TranscriptList, TranscriptOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hosted", tags=["hosted"])

_ALLOWED_EXTENSIONS = {".txt", ".md", ".vtt", ".srt"}
_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".vtt": "text/vtt",
    ".srt": "text/srt",
}

# HTML/XML/executable signatures that are not allowed even when hidden behind an
# accepted file extension.
_HTML_SIGS = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<style",
    b"<svg",
    b"<iframe",
    b"<object",
    b"<embed",
    b"<meta",
    b"<title",
    b"<?php",
    b"<%",
)

_MAGIC_SIGS = (
    (b"PK\x03\x04", "ZIP"),
    (b"PK\x05\x06", "ZIP"),
    (b"PK\x07\x08", "ZIP"),
    (b"Rar!", "RAR"),
    (b"7z\xbc\xaf'\x1c", "7z"),
    (b"\x7fELF", "ELF"),
    (b"MZ", "PE"),
    (b"\xcf\xfa\xed\xfe", "Mach-O"),
    (b"\xca\xfe\xba\xbe", "Mach-O"),
    (b"\xfe\xed\xfa\xcf", "Mach-O"),
    (b"%PDF", "PDF"),
    (b"\x1f\x8b", "gzip"),
    (b"BZ", "bzip2"),
    (b"\xfd7zXZ", "xz"),
    (b"ustar", "tar"),
)

_MAX_SAFE_FILENAME_LEN = 200


class TranscriptValidationError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class SizedAsyncIterator(AsyncIterable[bytes]):
    """Wrap an async generator and track the total number of bytes yielded."""

    def __init__(self, agen: AsyncGenerator[bytes, Any]) -> None:
        self._agen = agen
        self.total = 0
        self._done = False

    def __aiter__(self) -> "SizedAsyncIterator":
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        try:
            chunk = await self._agen.__anext__()
        except StopAsyncIteration:
            self._done = True
            raise
        self.total += len(chunk)
        return chunk

    async def aclose(self) -> None:
        if not self._done and hasattr(self._agen, "aclose"):
            try:
                await self._agen.aclose()
            except Exception:
                pass


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

    if "." in name:
        base, ext_part = name.rsplit(".", 1)
        ext_part = ext_part.strip()
        if not ext_part:
            return name[:_MAX_SAFE_FILENAME_LEN] or "transcript"
        max_base = _MAX_SAFE_FILENAME_LEN - len(ext_part) - 1
        if max_base <= 0:
            # Extension is too long; truncate the whole name to a safe fallback.
            return name[:_MAX_SAFE_FILENAME_LEN] or "transcript"
        base = base[:max_base].strip()
        if not base:
            base = "transcript"
        return f"{base}.{ext_part}"

    if len(name) > _MAX_SAFE_FILENAME_LEN:
        name = name[:_MAX_SAFE_FILENAME_LEN]
    return name


def _file_extension(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename[filename.rfind("."):].lower()


def _content_type_for(filename: str) -> str:
    """Return a safe content type based solely on the file extension."""
    lower = filename.lower()
    for ext in _EXTENSION_CONTENT_TYPES:
        if lower.endswith(ext):
            return _EXTENSION_CONTENT_TYPES[ext]
    raise TranscriptValidationError("Unsupported file type")


def _is_disallowed_content(first_chunk: bytes) -> bool:
    """Detect HTML/executable/binary content hidden behind an allowed extension."""
    if first_chunk.startswith(b"\xef\xbb\xbf"):
        first_chunk = first_chunk[3:]

    # Magic bytes are checked at the very start of the file.
    for magic, _ in _MAGIC_SIGS:
        if first_chunk.startswith(magic):
            return True

    # HTML-like signatures are only rejected when they appear at the very start
    # (after whitespace), so a transcript that merely mentions a tag is not blocked.
    leading = first_chunk.lstrip()[:128].lower()
    if leading and any(leading.startswith(sig) for sig in _HTML_SIGS):
        return True

    return False


def _validate_chunk(
    chunk: bytes,
    total: int,
    max_bytes: int,
    decoder: codecs.IncrementalDecoder,
    is_first: bool,
) -> int:
    """Validate a single chunk and return the new running total."""
    total += len(chunk)
    if total > max_bytes:
        raise TranscriptValidationError(f"File exceeds {max_bytes} bytes")
    if b"\x00" in chunk:
        raise TranscriptValidationError("File contains NUL bytes")
    try:
        decoder.decode(chunk, final=False)
    except UnicodeDecodeError as exc:
        raise TranscriptValidationError("File is not valid UTF-8") from exc
    if is_first and _is_disallowed_content(chunk):
        raise TranscriptValidationError("File content is not an allowed transcript format")
    return total


async def _validated_stream(
    file: UploadFile, first_chunk: bytes
) -> AsyncGenerator[bytes, None]:
    """Yield validated chunks and enforce size, UTF-8, NUL, and content rules."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    max_bytes = config.TRANSCRIPT_MAX_BYTES
    total = 0

    total = _validate_chunk(first_chunk, total, max_bytes, decoder, is_first=True)
    yield first_chunk

    while True:
        chunk = await file.read(8192)
        if not chunk:
            break
        total = _validate_chunk(chunk, total, max_bytes, decoder, is_first=False)
        yield chunk

    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise TranscriptValidationError("File is not valid UTF-8") from exc


async def _read_and_validate(
    file: UploadFile,
) -> tuple[SizedAsyncIterator, str, str]:
    """Validate the upload and return a sized byte iterator plus safe metadata."""
    raw = file.filename or ""
    if not raw:
        raise TranscriptValidationError("Filename is required")
    ext = _file_extension(raw)
    if ext not in _ALLOWED_EXTENSIONS:
        raise TranscriptValidationError("Only .txt, .md, .vtt, and .srt files are allowed")

    first_chunk = await file.read(8192)
    if not first_chunk:
        raise TranscriptValidationError("File is empty")

    # Eagerly validate the first chunk so malformed/unsafe content is rejected
    # before any bytes are streamed to Storage.
    decoder = codecs.getincrementaldecoder("utf-8")()
    _validate_chunk(first_chunk, 0, config.TRANSCRIPT_MAX_BYTES, decoder, is_first=True)

    original_filename = _safe_filename(raw)
    content_type = _content_type_for(original_filename)
    stream = _validated_stream(file, first_chunk)
    sized = SizedAsyncIterator(stream)
    return sized, original_filename, content_type


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
    data, original_filename, content_type = await _read_and_validate(file)
    transcript_id = uuid.uuid4()
    storage_path = _storage_path(org.org_id, account_id, None, transcript_id)
    backend = request.app.state.storage_backend

    # Stream the object to private Storage first. The object is not listable
    # until the metadata row is committed afterwards.
    try:
        await backend.upload(org.user.id, storage_path, data, content_type)
    except TranscriptValidationError as exc:
        # Invalid content discovered while streaming; attempt to remove any
        # partial object and return 400.
        try:
            await backend.delete(org.user.id, storage_path)
        except storage.ObjectNotFound:
            pass
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to clean up partially uploaded object %s: %s",
                storage_path,
                cleanup_exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload failed",
            ) from cleanup_exc
        raise exc
    except storage.StorageAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Storage access denied",
        ) from exc
    except storage.StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Storage upload failed",
        ) from exc
    except Exception as exc:
        logger.warning("Unexpected storage upload error for path %s: %s", storage_path, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed",
        ) from exc

    size_bytes = data.total
    try:
        async with tenant_connection(request, org) as conn:
            await _require_account_in_org(conn, account_id, org.org_id)
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
                size_bytes,
                content_type,
                org.user.id,
            )
    except HTTPException as http_exc:
        # Account not found/cross-org returns 404 after the object was uploaded.
        # Clean up the orphan and re-raise the 404.
        try:
            await backend.delete(org.user.id, storage_path)
        except storage.ObjectNotFound:
            pass
        except Exception as cleanup_exc:
            logger.warning("Failed to clean up orphaned storage object %s: %s", storage_path, cleanup_exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload failed",
            ) from cleanup_exc
        raise http_exc
    except Exception as insert_exc:
        # Metadata creation or transaction finalization failed. The object is
        # not listable, so attempt to clean up the orphaned Storage object.
        try:
            await backend.delete(org.user.id, storage_path)
        except storage.ObjectNotFound:
            pass
        except Exception as cleanup_exc:
            logger.warning("Failed to clean up orphaned storage object %s: %s", storage_path, cleanup_exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed",
        ) from insert_exc

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
    data, original_filename, content_type = await _read_and_validate(file)
    transcript_id = uuid.uuid4()
    storage_path = _storage_path(org.org_id, account_id, opportunity_id, transcript_id)
    backend = request.app.state.storage_backend

    try:
        await backend.upload(org.user.id, storage_path, data, content_type)
    except TranscriptValidationError as exc:
        try:
            await backend.delete(org.user.id, storage_path)
        except storage.ObjectNotFound:
            pass
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to clean up partially uploaded object %s: %s",
                storage_path,
                cleanup_exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload failed",
            ) from cleanup_exc
        raise exc
    except storage.StorageAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Storage access denied",
        ) from exc
    except storage.StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Storage upload failed",
        ) from exc
    except Exception as exc:
        logger.warning("Unexpected storage upload error for path %s: %s", storage_path, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed",
        ) from exc

    size_bytes = data.total
    try:
        async with tenant_connection(request, org) as conn:
            await _require_opportunity_in_account(conn, opportunity_id, account_id, org.org_id)
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
                size_bytes,
                content_type,
                org.user.id,
            )
    except HTTPException as http_exc:
        try:
            await backend.delete(org.user.id, storage_path)
        except storage.ObjectNotFound:
            pass
        except Exception as cleanup_exc:
            logger.warning("Failed to clean up orphaned storage object %s: %s", storage_path, cleanup_exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload failed",
            ) from cleanup_exc
        raise http_exc
    except Exception as insert_exc:
        try:
            await backend.delete(org.user.id, storage_path)
        except storage.ObjectNotFound:
            pass
        except Exception as cleanup_exc:
            logger.warning("Failed to clean up orphaned storage object %s: %s", storage_path, cleanup_exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed",
        ) from insert_exc

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

    backend = request.app.state.storage_backend
    try:
        data = await backend.download(org.user.id, row["storage_path"])
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
    except storage.StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not download transcript from storage",
        ) from exc

    filename = _safe_filename(row["original_filename"])
    content_type = row["mime_type"]
    return StreamingResponse(
        data,
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
    backend = request.app.state.storage_backend

    async with tenant_connection(request, org) as conn:
        if opportunity_id is not None:
            await _require_opportunity_in_account(conn, opportunity_id, account_id, org.org_id)
        else:
            await _require_account_in_org(conn, account_id, org.org_id)
        row = await _load_transcript(conn, transcript_id, account_id, opportunity_id, org.org_id)
        storage_path = row["storage_path"]

    # Delete the private Storage object first. If this fails, the metadata row
    # is unchanged and the transcript remains listable.
    try:
        await backend.delete(org.user.id, storage_path)
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

    # Then delete the metadata row. If this transaction fails, the Storage object
    # is already gone; the API reports failure rather than claiming success.
    try:
        async with tenant_connection(request, org) as conn:
            await conn.execute(
                "DELETE FROM public.transcripts WHERE id = $1 AND org_id = $2",
                transcript_id,
                org.org_id,
            )
    except Exception as delete_exc:
        logger.warning("Failed to delete transcript metadata %s: %s", transcript_id, delete_exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Delete failed",
        ) from delete_exc
