"""Storage backends for hosted transcript objects.

The production backend streams bytes to/from Supabase Storage using the
authenticated user's JWT for authorization. The Storage RLS policy (created in
migrations) enforces that a user can only touch objects under an organization
where they have an active membership. FastAPI never uses the Supabase
service-role key for normal transcript operations.

A memory backend is provided for tests so the transcript surface can be exercised
without a paid Supabase project.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

import asyncpg
import httpx

from . import auth, config

logger = logging.getLogger(__name__)

# Supabase Storage bucket used for transcript objects. This is fixed in the
# migration policies; runtime configuration of the bucket name is not supported
# because the RLS policies are static SQL.
BUCKET = "transcripts"


class StorageError(Exception):
    """Base class for storage backend failures."""


class ObjectNotFound(StorageError):
    """Raised when a requested object does not exist or is not accessible."""


class StorageAuthError(StorageError):
    """Raised when the caller is not authorized for the requested object."""


class StorageBackend:
    """Abstract storage backend for transcript bytes."""

    async def upload(
        self, token: str, path: str, data: AsyncIterable[bytes], content_type: str
    ) -> None:
        """Upload an object at *path* with the given byte stream and content type."""
        raise NotImplementedError

    async def download(self, token: str, path: str) -> AsyncGenerator[bytes, None]:
        """Return an async iterator over the object's bytes."""
        raise NotImplementedError

    async def delete(self, token: str, path: str) -> None:
        """Delete the object at *path*."""
        raise NotImplementedError

    async def list_prefix(self, token: str, prefix: str) -> list[str]:
        """Return object paths under *prefix*."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release backend resources."""
        pass


class SupabaseStorageBackend(StorageBackend):
    """Call Supabase Storage using the user's JWT and the public anon key.

    The anon key is required by the Supabase REST API as an `apikey` header;
    it is not a service-role credential and cannot access private objects by
    itself. Storage RLS uses the `Authorization` bearer token (the user's
    Supabase JWT) to decide access.
    """

    def __init__(self) -> None:
        base = (config.SUPABASE_STORAGE_ENDPOINT or config.SUPABASE_URL or "").rstrip("/")
        if not base:
            raise RuntimeError("SUPABASE_URL or SUPABASE_STORAGE_ENDPOINT is required for Storage")
        if not config.SUPABASE_ANON_KEY:
            raise RuntimeError("SUPABASE_ANON_KEY is required for Storage")
        self.base_url = base
        self.anon_key = config.SUPABASE_ANON_KEY
        self.client = httpx.AsyncClient(timeout=30)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
        }

    async def upload(
        self, token: str, path: str, data: AsyncIterable[bytes], content_type: str
    ) -> None:
        url = f"{self.base_url}/storage/v1/object/{BUCKET}/{path}"
        headers = {
            **self._headers(token),
            "Content-Type": content_type,
            "x-upsert": "false",
        }
        try:
            response = await self.client.post(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Storage upload request failed for path %s: %s", path, type(exc).__name__)
            raise StorageError("Storage upload failed") from exc
        if response.status_code == 401:
            raise StorageAuthError("Storage rejected upload")
        if response.status_code == 403:
            raise StorageAuthError("Storage access denied")
        if response.status_code >= 400:
            logger.warning(
                "Storage upload failed for path %s: status %s", path, response.status_code
            )
            raise StorageError("Storage upload failed")

    async def download(self, token: str, path: str) -> AsyncGenerator[bytes, None]:
        url = f"{self.base_url}/storage/v1/object/authenticated/{BUCKET}/{path}"
        request = self.client.build_request("GET", url, headers=self._headers(token))
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning(
                "Storage download request failed for path %s: %s", path, type(exc).__name__
            )
            raise StorageError("Storage download failed") from exc

        if response.status_code == 401:
            await response.aclose()
            raise StorageAuthError("Storage rejected download")
        if response.status_code == 403:
            await response.aclose()
            raise StorageAuthError("Storage access denied")
        if response.status_code == 404:
            await response.aclose()
            raise ObjectNotFound("Object not found in storage")
        if response.status_code >= 400:
            await response.aclose()
            logger.warning(
                "Storage download failed for path %s: status %s", path, response.status_code
            )
            raise StorageError("Storage download failed")

        async def _stream(resp: httpx.Response) -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return _stream(response)

    async def delete(self, token: str, path: str) -> None:
        url = f"{self.base_url}/storage/v1/object/{BUCKET}/{path}"
        try:
            response = await self.client.delete(url, headers=self._headers(token))
        except httpx.HTTPError as exc:
            logger.warning("Storage delete request failed for path %s: %s", path, type(exc).__name__)
            raise StorageError("Storage delete failed") from exc
        if response.status_code == 401:
            raise StorageAuthError("Storage rejected deletion")
        if response.status_code == 403:
            raise StorageAuthError("Storage access denied")
        if response.status_code == 404:
            raise ObjectNotFound("Object not found in storage")
        if response.status_code >= 400:
            logger.warning(
                "Storage delete failed for path %s: status %s", path, response.status_code
            )
            raise StorageError("Storage delete failed")

    async def list_prefix(self, token: str, prefix: str) -> list[str]:
        url = f"{self.base_url}/storage/v1/object/list/{BUCKET}"
        try:
            response = await self.client.post(
                url,
                json={"prefix": prefix, "limit": 1000},
                headers=self._headers(token),
            )
        except httpx.HTTPError as exc:
            logger.warning("Storage list request failed for prefix %s: %s", prefix, type(exc).__name__)
            raise StorageError("Storage list failed") from exc
        if response.status_code == 401:
            raise StorageAuthError("Storage rejected list")
        if response.status_code == 403:
            raise StorageAuthError("Storage access denied")
        if response.status_code >= 400:
            logger.warning(
                "Storage list failed for prefix %s: status %s", prefix, response.status_code
            )
            raise StorageError("Storage list failed")
        items: list[dict[str, Any]] = response.json()
        return [item["name"] for item in items if "name" in item]

    async def close(self) -> None:
        await self.client.aclose()


class MemoryStorageBackend(StorageBackend):
    """In-memory storage backend that mirrors the Supabase Storage RLS policy.

    Used by tests to verify application behavior without requiring a live
    Supabase Storage service. It validates the bearer token with the same
    configuration as `auth.verify_token` and checks that the object's
    organization prefix matches an active membership.

    When no admin pool is provided, a pool is created lazily from the
    `DATABASE_ADMIN_URL` environment variable so the backend always operates in
    the event loop where its methods are called.
    """

    def __init__(self, admin_pool: asyncpg.Pool | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self._provided_pool = admin_pool
        self._lazy_pool: asyncpg.Pool | None = None

    async def _pool(self) -> asyncpg.Pool:
        if self._provided_pool is not None:
            return self._provided_pool
        if self._lazy_pool is None:
            if not config.DATABASE_ADMIN_URL:
                raise RuntimeError("DATABASE_ADMIN_URL is required for MemoryStorageBackend")
            self._lazy_pool = await asyncpg.create_pool(
                config.DATABASE_ADMIN_URL, min_size=1, max_size=2
            )
        return self._lazy_pool

    async def _is_active_member(self, user_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM public.memberships WHERE user_id = $1 AND org_id = $2 AND active = true",
                user_id,
                org_id,
            )
            return row is not None

    async def _validate(self, token: str, path: str) -> None:
        if not token:
            raise StorageAuthError("Missing token")
        try:
            verified = auth.verify_token(token)
        except auth.AuthError as exc:
            raise StorageAuthError(str(exc)) from exc
        parts = path.split("/")
        if not parts or not parts[0]:
            raise StorageAuthError("Invalid object path")
        try:
            org_id = uuid.UUID(parts[0])
        except ValueError as exc:
            raise StorageAuthError("Invalid organization segment") from exc
        if not await self._is_active_member(verified.user_id, org_id):
            raise StorageAuthError("User is not an active member of this organization")

    async def upload(
        self, token: str, path: str, data: AsyncIterable[bytes], content_type: str
    ) -> None:
        await self._validate(token, path)
        chunks: list[bytes] = []
        if isinstance(data, bytes):
            chunks = [data]
        else:
            async for chunk in data:
                chunks.append(chunk)
        self.objects[path] = b"".join(chunks)
        self.content_types[path] = content_type

    async def download(self, token: str, path: str) -> AsyncGenerator[bytes, None]:
        await self._validate(token, path)
        if path not in self.objects:
            raise ObjectNotFound("Object not found")
        data = self.objects[path]

        async def _stream() -> AsyncGenerator[bytes, None]:
            chunk_size = 4096
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

        return _stream()

    async def delete(self, token: str, path: str) -> None:
        await self._validate(token, path)
        if path not in self.objects:
            raise ObjectNotFound("Object not found")
        del self.objects[path]
        self.content_types.pop(path, None)

    async def list_prefix(self, token: str, prefix: str) -> list[str]:
        # For listing we only verify the org prefix, matching the Supabase policy.
        parts = prefix.split("/")
        if not parts or not parts[0]:
            raise StorageAuthError("Invalid list prefix")
        await self._validate(token, parts[0])
        return [path for path in self.objects if path.startswith(prefix)]

    async def close(self) -> None:
        if self._lazy_pool is not None:
            await self._lazy_pool.close()
            self._lazy_pool = None


_backend: StorageBackend | None = None


def get_backend() -> StorageBackend:
    """Return the configured storage backend singleton."""
    global _backend
    if _backend is None:
        _backend = SupabaseStorageBackend()
    return _backend


def set_backend(backend: StorageBackend) -> None:
    """Replace the singleton backend (used by tests)."""
    global _backend
    _backend = backend


def clear_backend() -> None:
    """Reset the singleton backend (used by tests)."""
    global _backend
    if _backend is not None:
        import asyncio
        try:
            asyncio.get_running_loop()
            asyncio.create_task(_backend.close())
        except RuntimeError:
            pass
    _backend = None
