"""Storage backends for hosted transcript objects.

The production backend streams bytes to/from Supabase Storage using a
server-signed JWT for a dedicated `app_storage` Postgres role. The Storage
RLS policy (created in migrations) enforces that a user can only touch objects
under an organization where they have an active membership. FastAPI never uses
the Supabase service-role key for normal transcript operations, and the user's
browser token is never authorized for direct Storage object access.

A memory backend is provided for tests so the transcript surface can be exercised
without a paid Supabase project.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
import jwt

from . import config

logger = logging.getLogger(__name__)

# Supabase Storage bucket names. These are fixed in the migration policies;
# runtime configuration of the bucket name is not supported because the RLS
# policies are static SQL. The default remains "transcripts" for backwards
# compatibility.
DEFAULT_BUCKET = "transcripts"
OUTPUTS_BUCKET = "outputs"

# Postgres role names used as the `role` claim of the server-signed Storage JWT.
# `app_storage` carries a user subject and inherits the active-membership
# contract. `app_storage_maintenance` carries an *organization* subject plus the
# exact bucket and object being reconciled, and may only delete that one object
# under that organization's path in that bucket, so cleanup of an abandoned
# private object never depends on the historical actor still being an active
# member.
STORAGE_ROLE = "app_storage"
MAINTENANCE_ROLE = "app_storage_maintenance"

# JWT claims naming the single bucket and object a maintenance token may act on.
# The maintenance policies require `storage.objects.bucket_id` and
# `storage.objects.name` to equal them, so the credential cannot reach any other
# object even inside its own organization, and an outputs token is invalid
# against the transcripts bucket and vice versa.
MAINTENANCE_OBJECT_CLAIM = "maintenance_object"
MAINTENANCE_BUCKET_CLAIM = "maintenance_bucket"

# Buckets with exact-target maintenance policies: generated outputs (tombstoned
# or abandoned correction uploads) and transcripts (tombstoned customer
# uploads). Nothing else is reachable with a maintenance credential.
MAINTENANCE_BUCKETS = frozenset({OUTPUTS_BUCKET, DEFAULT_BUCKET})


class StorageError(Exception):
    """Base class for storage backend failures."""


class ObjectNotFound(StorageError):
    """Raised when a requested object does not exist or is not accessible."""


class StorageAuthError(StorageError):
    """Raised when the caller is not authorized for the requested object."""


def check_maintenance_target(org_id: uuid.UUID | None, path: str, bucket: str) -> None:
    """Reject anything the Storage maintenance policies would not authorize.

    Enforced in the backend as well as in SQL so the narrow contract holds for
    the memory backend and for any caller mistake: a bucket that has exact-target
    maintenance policies, and an object whose leading org path segment equals the
    token subject.
    """
    if not org_id:
        raise StorageAuthError("Missing organization identity")
    if bucket not in MAINTENANCE_BUCKETS:
        raise StorageAuthError("Maintenance deletion is not supported for this bucket")
    parts = path.split("/")
    if not parts or not parts[0]:
        raise StorageAuthError("Invalid object path")
    try:
        path_org = uuid.UUID(parts[0])
    except ValueError as exc:
        raise StorageAuthError("Invalid organization segment") from exc
    if path_org != org_id:
        raise StorageAuthError("Object is outside the maintenance organization path")


class StorageBackend:
    """Abstract storage backend for private organization-scoped objects."""

    async def upload(
        self,
        user_id: uuid.UUID,
        path: str,
        data: AsyncIterable[bytes],
        content_type: str,
        bucket: str = DEFAULT_BUCKET,
    ) -> None:
        """Upload an object at *path* in *bucket* with the given byte stream."""
        raise NotImplementedError

    async def download(
        self, user_id: uuid.UUID, path: str, bucket: str = DEFAULT_BUCKET
    ) -> AsyncGenerator[bytes, None]:
        """Return an async iterator over the object's bytes from *bucket*."""
        raise NotImplementedError

    async def delete(
        self, user_id: uuid.UUID, path: str, bucket: str = DEFAULT_BUCKET
    ) -> None:
        """Delete the object at *path* in *bucket*."""
        raise NotImplementedError

    async def delete_for_maintenance(
        self, org_id: uuid.UUID, path: str, bucket: str = OUTPUTS_BUCKET
    ) -> None:
        """Delete an abandoned private object as the Storage maintenance identity.

        Authorization is object-scoped rather than user-scoped: the credential is
        minted for exactly *bucket* and *path*, which must live under *org_id*,
        and it authorizes nothing else. No membership is consulted, so a
        deactivated correction author or transcript deleter cannot pin customer
        content in Storage. *path* comes from the trusted cleanup claim or
        tombstone row, never from a request body.
        """
        raise NotImplementedError

    async def list_prefix(
        self, user_id: uuid.UUID, prefix: str, bucket: str = DEFAULT_BUCKET
    ) -> list[str]:
        """Return object paths under *prefix* in *bucket*."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release backend resources."""
        pass


class SupabaseStorageBackend(StorageBackend):
    """Call Supabase Storage with a server-signed `app_storage` JWT.

    The public anon key is sent only as the `apikey` project identifier. The
    `Authorization` header carries a short-lived JWT signed by the backend with
    `SUPABASE_JWT_SECRET` and the `app_storage` role. The Storage RLS policies
    for each private bucket then verify the `sub` claim against an active
    membership in the organization path segment.
    """

    def __init__(self) -> None:
        base = (config.SUPABASE_STORAGE_ENDPOINT or config.SUPABASE_URL or "").rstrip("/")
        if not base:
            raise RuntimeError("SUPABASE_URL or SUPABASE_STORAGE_ENDPOINT is required for Storage")
        if not config.SUPABASE_ANON_KEY:
            raise RuntimeError("SUPABASE_ANON_KEY is required for Storage")
        if not config.SUPABASE_JWT_SECRET:
            raise RuntimeError("SUPABASE_JWT_SECRET is required for Storage")
        self.base_url = base
        self.anon_key = config.SUPABASE_ANON_KEY
        self.jwt_secret = config.SUPABASE_JWT_SECRET
        self.client = httpx.AsyncClient(timeout=30)

    def _storage_token(
        self,
        subject: uuid.UUID,
        role: str = STORAGE_ROLE,
        maintenance_object: str | None = None,
        maintenance_bucket: str | None = None,
    ) -> str:
        """Return a short-lived JWT for a dedicated Storage role.

        The subject is a user id for `app_storage` and an organization id for
        `app_storage_maintenance`; the bucket policies read it via `auth.uid()`.
        A maintenance token additionally carries `maintenance_object`, the exact
        object being reconciled, which the maintenance policies require `name` to
        equal — so the credential is scoped to one object rather than to the
        organization's outputs.
        """
        now = datetime.now(timezone.utc).timestamp()
        claims: dict[str, str | float] = {
            "sub": str(subject),
            "role": role,
            "iat": now,
            "exp": now + 60,
        }
        if maintenance_object is not None:
            claims[MAINTENANCE_OBJECT_CLAIM] = maintenance_object
        if maintenance_bucket is not None:
            claims[MAINTENANCE_BUCKET_CLAIM] = maintenance_bucket
        return jwt.encode(claims, self.jwt_secret, algorithm="HS256")

    def _headers(
        self,
        user_id: uuid.UUID,
        role: str = STORAGE_ROLE,
        maintenance_object: str | None = None,
        maintenance_bucket: str | None = None,
    ) -> dict[str, str]:
        token = self._storage_token(
            user_id, role, maintenance_object, maintenance_bucket
        )
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token}",
        }

    async def upload(
        self,
        user_id: uuid.UUID,
        path: str,
        data: AsyncIterable[bytes],
        content_type: str,
        bucket: str = DEFAULT_BUCKET,
    ) -> None:
        url = f"{self.base_url}/storage/v1/object/{bucket}/{path}"
        headers = {
            **self._headers(user_id),
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

    async def download(
        self, user_id: uuid.UUID, path: str, bucket: str = DEFAULT_BUCKET
    ) -> AsyncGenerator[bytes, None]:
        url = f"{self.base_url}/storage/v1/object/authenticated/{bucket}/{path}"
        request = self.client.build_request("GET", url, headers=self._headers(user_id))
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

    async def delete(self, user_id: uuid.UUID, path: str, bucket: str = DEFAULT_BUCKET) -> None:
        url = f"{self.base_url}/storage/v1/object/{bucket}/{path}"
        try:
            response = await self.client.delete(url, headers=self._headers(user_id))
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

    async def delete_for_maintenance(
        self, org_id: uuid.UUID, path: str, bucket: str = OUTPUTS_BUCKET
    ) -> None:
        check_maintenance_target(org_id, path, bucket)
        url = f"{self.base_url}/storage/v1/object/{bucket}/{path}"
        try:
            response = await self.client.delete(
                url,
                headers=self._headers(
                    org_id,
                    MAINTENANCE_ROLE,
                    maintenance_object=path,
                    maintenance_bucket=bucket,
                ),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Storage maintenance delete request failed for path %s: %s",
                path,
                type(exc).__name__,
            )
            raise StorageError("Storage delete failed") from exc
        if response.status_code in (401, 403):
            raise StorageAuthError("Storage denied maintenance deletion")
        if response.status_code == 404:
            raise ObjectNotFound("Object not found in storage")
        if response.status_code >= 400:
            logger.warning(
                "Storage maintenance delete failed for path %s: status %s",
                path,
                response.status_code,
            )
            raise StorageError("Storage delete failed")

    async def list_prefix(self, user_id: uuid.UUID, prefix: str, bucket: str = DEFAULT_BUCKET) -> list[str]:
        url = f"{self.base_url}/storage/v1/object/list/{bucket}"
        try:
            response = await self.client.post(
                url,
                json={"prefix": prefix, "limit": 1000},
                headers=self._headers(user_id),
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
    Supabase Storage service. It checks that the object's organization prefix
    matches an active membership for the provided user id.

    When no admin pool is provided, a pool is created lazily from the
    `DATABASE_ADMIN_URL` environment variable so the backend always operates in
    the event loop where its methods are called.
    """

    def __init__(self, admin_pool: asyncpg.Pool | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self._provided_pool = admin_pool

    async def _is_active_member(self, user_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        """Check membership using the provided pool or a per-call connection.

        A fresh connection is used when no admin pool is supplied so the backend
        can be exercised from different event loops in tests.
        """
        if self._provided_pool is not None:
            async with self._provided_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM public.memberships WHERE user_id = $1 AND org_id = $2 AND active = true",
                    user_id,
                    org_id,
                )
                return row is not None

        if not config.DATABASE_ADMIN_URL:
            raise RuntimeError("DATABASE_ADMIN_URL is required for MemoryStorageBackend")
        conn = await asyncpg.connect(config.DATABASE_ADMIN_URL)
        try:
            row = await conn.fetchrow(
                "SELECT 1 FROM public.memberships WHERE user_id = $1 AND org_id = $2 AND active = true",
                user_id,
                org_id,
            )
            return row is not None
        finally:
            await conn.close()

    async def _validate(self, user_id: uuid.UUID | None, path: str) -> None:
        if not user_id:
            raise StorageAuthError("Missing user identity")
        parts = path.split("/")
        if not parts or not parts[0]:
            raise StorageAuthError("Invalid object path")
        try:
            org_id = uuid.UUID(parts[0])
        except ValueError as exc:
            raise StorageAuthError("Invalid organization segment") from exc
        if not await self._is_active_member(user_id, org_id):
            raise StorageAuthError("User is not an active member of this organization")

    async def upload(
        self,
        user_id: uuid.UUID | None,
        path: str,
        data: AsyncIterable[bytes],
        content_type: str,
        bucket: str = DEFAULT_BUCKET,
    ) -> None:
        await self._validate(user_id, path)
        key = f"{bucket}:{path}"
        chunks: list[bytes] = []
        if isinstance(data, bytes):
            chunks = [data]
        else:
            async for chunk in data:
                chunks.append(chunk)
        self.objects[key] = b"".join(chunks)
        self.content_types[key] = content_type

    async def download(
        self, user_id: uuid.UUID | None, path: str, bucket: str = DEFAULT_BUCKET
    ) -> AsyncGenerator[bytes, None]:
        await self._validate(user_id, path)
        key = f"{bucket}:{path}"
        if key not in self.objects:
            raise ObjectNotFound("Object not found")
        data = self.objects[key]

        async def _stream() -> AsyncGenerator[bytes, None]:
            chunk_size = 4096
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

        return _stream()

    async def delete(
        self, user_id: uuid.UUID | None, path: str, bucket: str = DEFAULT_BUCKET
    ) -> None:
        await self._validate(user_id, path)
        key = f"{bucket}:{path}"
        if key not in self.objects:
            raise ObjectNotFound("Object not found")
        del self.objects[key]
        self.content_types.pop(key, None)

    async def delete_for_maintenance(
        self, org_id: uuid.UUID, path: str, bucket: str = OUTPUTS_BUCKET
    ) -> None:
        # Mirrors the maintenance policies: a bucket with exact-target policies,
        # an org path segment equal to the token subject, and no membership
        # lookup at all.
        check_maintenance_target(org_id, path, bucket)
        key = f"{bucket}:{path}"
        if key not in self.objects:
            raise ObjectNotFound("Object not found")
        del self.objects[key]
        self.content_types.pop(key, None)

    async def list_prefix(
        self, user_id: uuid.UUID | None, prefix: str, bucket: str = DEFAULT_BUCKET
    ) -> list[str]:
        # For listing we only verify the org prefix, matching the Supabase policy.
        parts = prefix.split("/")
        if not parts or not parts[0]:
            raise StorageAuthError("Invalid list prefix")
        await self._validate(user_id, parts[0])
        key_prefix = f"{bucket}:{prefix}"
        return [path[len(bucket) + 1:] for path in self.objects if path.startswith(key_prefix)]

    async def close(self) -> None:
        pass


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
