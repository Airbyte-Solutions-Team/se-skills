"""Deterministic fakes shared by hosted smoke tests and the offline harness."""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterable

from hosted import storage


class SmokeStorage:
    """In-memory Storage boundary with no database or network access."""

    def __init__(self, transcript_path: str, transcript: str) -> None:
        self._objects = {transcript_path: transcript.encode("utf-8")}

    async def download(
        self,
        user_id: uuid.UUID | None,
        path: str,
        bucket: str = storage.DEFAULT_BUCKET,
    ) -> AsyncGenerator[bytes, None]:
        del user_id, bucket
        data = self._objects[path]

        async def _stream() -> AsyncGenerator[bytes, None]:
            yield data

        return _stream()

    async def upload(
        self,
        user_id: uuid.UUID | None,
        path: str,
        data: AsyncIterable[bytes],
        content_type: str,
        bucket: str = storage.DEFAULT_BUCKET,
    ) -> None:
        del user_id, content_type
        chunks: list[bytes] = []
        async for chunk in data:
            chunks.append(chunk)
        self._objects[f"{bucket}:{path}"] = b"".join(chunks)

    async def delete(
        self,
        user_id: uuid.UUID | None,
        path: str,
        bucket: str = storage.DEFAULT_BUCKET,
    ) -> None:
        del user_id
        self._objects.pop(f"{bucket}:{path}", None)

    async def delete_for_maintenance(
        self,
        org_id: uuid.UUID | None,
        path: str,
        bucket: str = storage.OUTPUTS_BUCKET,
    ) -> None:
        del org_id
        self._objects.pop(f"{bucket}:{path}", None)


class _Acquire:
    def __init__(self, connection: "SmokeConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "SmokeConnection":
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class SmokeConnection:
    """Small SQL-function double for one successful orchestrator attempt."""

    def __init__(self, resolved_inputs: dict[str, object]) -> None:
        self.resolved_inputs = resolved_inputs
        self.output_id: uuid.UUID | None = None

    async def fetchval(self, query: str, *args: object) -> object:
        if "resolve_job_inputs" in query:
            return self.resolved_inputs
        if "worker_heartbeat" in query:
            return False
        if "create_job_output" in query:
            self.output_id = args[3] if isinstance(args[3], uuid.UUID) else None
            return self.output_id
        if "complete_job" in query:
            return "completed"
        raise AssertionError(f"unexpected smoke SQL function: {query.strip()[:64]}")

    async def fetchrow(self, query: str, *args: object) -> None:
        if "get_staged_output" in query:
            return None
        raise AssertionError(f"unexpected smoke SQL query: {query.strip()[:64]}")

    async def execute(self, query: str, *args: object) -> None:
        if "cancel_job" in query:
            return None
        raise AssertionError(f"unexpected smoke SQL command: {query.strip()[:64]}")


class SmokePool:
    """Injectable Postgres boundary for the successful smoke attempt."""

    def __init__(self, resolved_inputs: dict[str, object]) -> None:
        self.connection = SmokeConnection(resolved_inputs)

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)
