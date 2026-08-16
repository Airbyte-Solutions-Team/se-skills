"""Trusted worker-side post-call orchestration and output persistence.

The orchestrator materializes authorized inputs from the durable job, invokes an
injected `SkillRuntime`, validates the returned Markdown + sidecar outside the
runtime, and persists valid artifacts to private org-scoped Storage and Postgres.
No Anthropic key, DB credential, Storage credential, signed URL, or transcript
body enters the runtime job payload or sandbox workspace.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import asyncpg

import output_schema
from hosted import config, storage
from hosted.executor import Executor, ExecutorResult
from hosted.runtime_contract import (
    Allowlist,
    ExecutionMetadata,
    FailureCategory,
    InputManifest,
    NetworkDestination,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    SandboxOutputSidecar,
    SkillRuntime,
    TokenUsage,
    ToolName,
    ValidationResult,
)

logger = logging.getLogger(__name__)

_OUTPUT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
MAX_OUTPUT_MD_BYTES = 10 * 1024 * 1024
MAX_SIDECAR_BYTES = 1024 * 1024
INPUT_DIR_MODE = 0o555
INPUT_FILE_MODE = 0o444


class RuntimeOutputError(ValueError):
    """Raised when the runtime output workspace contains an unsafe artifact."""


@runtime_checkable
class CancellationSource(Protocol):
    """Something the worker can poll to see if cancellation was requested."""

    def is_cancelled(self) -> bool:
        ...

    async def wait(self) -> None:
        ...


class _AsyncioEventCancellationSource:
    """Adapter that exposes an `asyncio.Event` as a `CancellationSource`."""

    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class _CancellationToken:
    """Host-side cancellation signal for the runtime.

    Optionally wraps an external `CancellationSource` (e.g. the worker's
    `asyncio.Event`) so a long-running runtime can observe cancellation without
    polling the database.
    """

    def __init__(self, external: CancellationSource | None = None) -> None:
        self._event = asyncio.Event()
        self._external = external

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._external is not None:
            return self._external.is_cancelled()
        return False

    async def wait(self) -> None:
        tasks: list[asyncio.Task[Any]] = [asyncio.create_task(self._event.wait())]
        if self._external is not None:
            tasks.append(asyncio.create_task(self._external.wait()))
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            with contextlib.suppress(asyncio.CancelledError):
                await t


@dataclass
class OrchestratorContext:
    """Execution-time state shared with the worker."""

    cancellation: CancellationSource | None = None


@dataclass
class _PersistedOutput:
    """An output row plus the object path that was staged for it."""

    output_id: uuid.UUID
    content_storage_path: str
    validation_status: str
    token_usage: dict[str, Any]
    cost: float | None
    runtime_version: str
    model: str


def _storage_path_for_output(
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    transcript_id: uuid.UUID,
    output_id: uuid.UUID,
) -> str:
    """Build the private, org-scoped Storage key for a generated output."""
    return "/".join([
        str(org_id),
        str(account_id),
        str(transcript_id),
        str(output_id),
        "output.md",
    ])


def _safe_filename(name: str) -> str:
    """Return a safe filename for use inside a runtime workspace."""
    base = name.strip().replace(" ", "_")
    base = "".join(c for c in base if c.isalnum() or c in ("_", "-", "."))
    if not base or base.startswith("."):
        base = "transcript.txt"
    return base


def _deterministic_output_id(job_id: uuid.UUID) -> uuid.UUID:
    """Return a stable output id for this job.

    Using only the job_id means a retried attempt after a failed `complete_job`
    reuses the same output evidence instead of creating a duplicate row.
    """
    text = f"output:{job_id}"
    return uuid.uuid5(_OUTPUT_NAMESPACE, text)


class PostCallOrchestrator:
    """Trusted worker-side orchestration for the `post-call` skill."""

    def __init__(
        self,
        runtime: SkillRuntime,
        db_pool: asyncpg.Pool,
        storage_backend: storage.StorageBackend | None = None,
    ) -> None:
        self.runtime = runtime
        self.db_pool = db_pool
        self.storage = storage_backend or storage.get_backend()

    def _redacted_failure(
        self, category: FailureCategory, *, finalized: bool = False
    ) -> ExecutorResult:
        failure = RedactedFailure(category=category)
        return ExecutorResult(
            output_id=None,
            validation_status="invalid",
            token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            cost=None,
            runtime_version="post-call-orchestrator",
            model=None,
            error_category=category,
            error=failure.message,
            finalized=finalized,
        )

    @staticmethod
    def _json_field(job: dict[str, Any], key: str) -> Any:
        """Return a JSONB field, parsing it if asyncpg returned a JSON string."""
        value = job.get(key)
        if isinstance(value, str):
            return json.loads(value)
        return value

    @staticmethod
    def _make_writable(path: Path) -> None:
        """Recursively chmod a workspace so `shutil.rmtree` can remove it.

        Symlinks are not followed so a compromised runtime cannot redirect this
        cleanup to an unrelated host path.
        """
        if not path.exists():
            return
        os.chmod(str(path), 0o700, follow_symlinks=False)
        if path.is_dir() and not path.is_symlink():
            for child in path.iterdir():
                PostCallOrchestrator._make_writable(child)

    @staticmethod
    def _safe_read_text(path: Path, max_bytes: int) -> str:
        """Open a path without following symlinks and read at most `max_bytes`.

        The file is opened with `O_NOFOLLOW` so a runtime cannot replace a file
        with a symlink after our existence check. `O_NONBLOCK` prevents opening
        a FIFO from blocking. The file descriptor is `fstat`-ed after open to
        avoid a check-then-open TOCTOU gap.
        """
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise RuntimeOutputError(f"cannot open runtime output {path.name}: {exc}") from exc

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeOutputError(f"runtime output {path.name} is not a regular file")
            f = io.FileIO(fd, closefd=False)
            try:
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            finally:
                f.close()
            if len(data) > max_bytes:
                raise RuntimeOutputError(f"runtime output {path.name} exceeds maximum size")
            return data.decode("utf-8", errors="replace")
        finally:
            os.close(fd)

    @classmethod
    def _safe_read_json(cls, path: Path, max_bytes: int) -> Any:
        """Read a JSON file without following symlinks and with a size cap."""
        text = cls._safe_read_text(path, max_bytes)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeOutputError(f"runtime output {path.name} is not valid JSON") from exc

    async def _resolve_inputs(
        self, job: dict[str, Any]
    ) -> tuple[dict[str, Any], InputManifest, dict[uuid.UUID, str]]:
        """Resolve canonical transcript and approved prior-context references.

        Uses the lease-bound `resolve_job_inputs` worker function so the
        transcript storage path and prior outputs are loaded from trusted DB state,
        not from the manifest alone. Rejects missing, aliased, cross-org,
        cross-account, mismatched-opportunity, duplicated, or unlisted references.
        """
        job_id = uuid.UUID(str(job["job_id"]))
        attempt_number = int(job.get("attempt_number") or 1)
        lease_token = uuid.UUID(str(job["lease_token"]))

        input_refs = self._json_field(job, "input_refs") or {}
        prior_ids: list[uuid.UUID] = []
        if isinstance(input_refs, dict):
            prior_output_ids = input_refs.get("prior_output_ids") or []
            if prior_output_ids:
                seen: set[str] = set()
                for ref in prior_output_ids:
                    ref_str = str(ref)
                    if ref_str in seen:
                        raise ValueError("duplicate prior context reference")
                    seen.add(ref_str)
                    try:
                        prior_ids.append(uuid.UUID(ref_str))
                    except ValueError as exc:
                        raise ValueError("invalid prior context reference") from exc

        async with self.db_pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT public.resolve_job_inputs($1, $2, $3, $4::uuid[])",
                job_id,
                attempt_number,
                lease_token,
                prior_ids,
            )
        resolved = self._json_field({"resolved": raw}, "resolved") if isinstance(raw, str) else raw
        if not isinstance(resolved, dict):
            raise ValueError("resolve_job_inputs did not return a JSON object")

        source_manifest = self._json_field(job, "source_manifest") or {}
        if not isinstance(source_manifest, dict):
            raise ValueError("source_manifest must be a JSON object")

        # The source manifest must agree with the trusted DB state returned by
        # `resolve_job_inputs`. A tampered manifest cannot redirect the worker to
        # an unlisted or cross-scope storage object.
        for key in ("transcript_id", "account_id", "org_id"):
            if source_manifest.get(key) != str(resolved.get(key)):
                raise ValueError(f"manifest {key} does not match trusted transcript")
        expected_opp = str(resolved.get("opportunity_id")) if resolved.get("opportunity_id") else None
        if source_manifest.get("opportunity_id") != expected_opp:
            raise ValueError("manifest opportunity_id does not match trusted transcript")
        if source_manifest.get("storage_path") != resolved.get("storage_path"):
            raise ValueError("manifest storage_path does not match trusted transcript")

        canonical_path = resolved.get("storage_path") or ""
        if ".." in canonical_path or canonical_path.startswith(("/", "~", "\\")):
            raise ValueError("trusted transcript storage_path is unsafe")

        original_filename = resolved.get("original_filename") or "transcript.txt"
        transcript_ref = _safe_filename(original_filename)

        prior_paths: dict[uuid.UUID, str] = {}
        prior_refs: set[str] = set()
        for entry in resolved.get("prior_outputs") or []:
            if not isinstance(entry, dict):
                raise ValueError("prior output entry is not an object")
            output_id = uuid.UUID(str(entry["output_id"]))
            storage_path = str(entry["storage_path"])
            if ".." in storage_path or storage_path.startswith(("/", "~", "\\")):
                raise ValueError("prior context storage_path is unsafe")
            prior_paths[output_id] = storage_path
            prior_refs.add(str(output_id))

        manifest = InputManifest(
            transcript_id=uuid.UUID(str(resolved["transcript_id"])),
            transcript_ref=transcript_ref,
            account_id=uuid.UUID(str(resolved["account_id"])),
            org_id=uuid.UUID(str(resolved["org_id"])),
            opportunity_id=(
                uuid.UUID(str(resolved["opportunity_id"]))
                if resolved.get("opportunity_id")
                else None
            ),
            prior_context_refs=frozenset(prior_refs),
        )
        return source_manifest, manifest, prior_paths

    async def _materialize_inputs(
        self,
        manifest: InputManifest,
        resolved: dict[str, Any],
        prior_paths: dict[uuid.UUID, str],
        requester_id: uuid.UUID,
        input_dir: Path,
    ) -> str:
        """Fetch transcript and approved prior context through Storage.

        Returns the transcript text. Only manifest-listed files are materialized,
        read-only, and with no signed URLs or credentials passed to the runtime.
        The input directory itself is made read-only so the runtime cannot create
        new files or modify evidence, preserving the bind-mount contract for 5B2.
        """
        input_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = resolved["storage_path"]
        transcript_bytes = b""
        stream = await self.storage.download(
            requester_id, transcript_path, bucket=storage.DEFAULT_BUCKET
        )
        async for chunk in stream:
            transcript_bytes += chunk

        transcript_text = transcript_bytes.decode("utf-8", errors="replace")
        transcript_file = input_dir / manifest.transcript_ref
        transcript_file.write_text(transcript_text, encoding="utf-8")
        os.chmod(str(transcript_file), INPUT_FILE_MODE, follow_symlinks=False)

        for output_id, storage_path in prior_paths.items():
            prior_bytes = b""
            stream = await self.storage.download(
                requester_id, storage_path, bucket=storage.OUTPUTS_BUCKET
            )
            async for chunk in stream:
                prior_bytes += chunk
            prior_text = prior_bytes.decode("utf-8", errors="replace")
            prior_file = input_dir / str(output_id)
            prior_file.write_text(prior_text, encoding="utf-8")
            os.chmod(str(prior_file), INPUT_FILE_MODE, follow_symlinks=False)

        os.chmod(str(input_dir), INPUT_DIR_MODE, follow_symlinks=False)

        return transcript_text

    def _build_runtime_job(
        self,
        job: dict[str, Any],
        manifest: InputManifest,
        input_dir: Path,
        output_dir: Path,
    ) -> RuntimeJob:
        """Construct the immutable `RuntimeJob` sent to the sandbox runtime."""
        job_id = uuid.UUID(str(job["job_id"]))
        org_id = uuid.UUID(str(job["org_id"]))
        account_id = uuid.UUID(str(job["account_id"]))
        transcript_id = uuid.UUID(str(job["transcript_id"]))
        requester_id = uuid.UUID(str(job["requester_id"]))
        opportunity_id = uuid.UUID(str(job["opportunity_id"])) if job.get("opportunity_id") else None

        payload = self._json_field(job, "payload") or {}
        model = payload.get("model") or "claude-sonnet-4-6"
        runtime_version = payload.get("runtime_version") or "slice5b1"
        mode = payload.get("mode") or "full"
        if mode not in ("full", "brief"):
            mode = "full"

        # The runtime is allowed a single worker-model-proxy destination.
        # In 5B1 tests the fake runtime does not use the network; this keeps the
        # contract valid for 5B2 gVisor deployment.
        allowlist = Allowlist(
            tools=frozenset([
                "read_transcript",
                "read_prior_context",
                "list_priors",
                "search_transcript",
                "write_output",
                "finish",
                "report_failure",
            ]),  # type: ignore[arg-type]
            network=frozenset([
                NetworkDestination(host="worker-proxy", scheme="http", port=8080),
            ]),
        )

        deadline_ts = job.get("deadline_ts")
        if isinstance(deadline_ts, datetime):
            deadline = deadline_ts.replace(tzinfo=timezone.utc) if deadline_ts.tzinfo is None else deadline_ts
        else:
            # Tests that invoke the executor directly without a worker-provided
            # deadline use a short default window.
            deadline = datetime.now(tz=timezone.utc) + timedelta(minutes=5)

        return RuntimeJob(
            job_id=job_id,
            org_id=org_id,
            account_id=account_id,
            transcript_id=transcript_id,
            requester_id=requester_id,
            opportunity_id=opportunity_id,
            skill="post-call",
            skill_version=job.get("skill_version") or "1.0",
            requested_model=model,
            requested_runtime_version=runtime_version,
            mode=mode,  # type: ignore[arg-type]
            input_manifest=manifest,
            allowlist=allowlist,
            execution_deadline=deadline,
            input_workspace=str(input_dir),
            output_workspace=str(output_dir),
        )

    async def _read_runtime_output(
        self, output_dir: Path
    ) -> tuple[str, SandboxOutputSidecar]:
        """Read the candidate Markdown and sidecar from the output workspace.

        Uses no-follow, safe-open semantics so a compromised runtime cannot
        redirect reads through symlinks, substitute a FIFO/device, or cause
        unbounded memory use with an oversized file.
        """
        md_path = output_dir / "output.md"
        sidecar_path = output_dir / "sidecar.json"

        markdown, sidecar_data = await asyncio.gather(
            asyncio.to_thread(self._safe_read_text, md_path, MAX_OUTPUT_MD_BYTES),
            asyncio.to_thread(self._safe_read_json, sidecar_path, MAX_SIDECAR_BYTES),
        )
        sidecar = SandboxOutputSidecar(**sidecar_data)
        return markdown, sidecar

    async def _validate_artifact(
        self,
        job: dict[str, Any],
        markdown: str,
        sidecar: SandboxOutputSidecar,
        transcript_text: str,
    ) -> ValidationResult:
        """Validate the candidate sidecar and Markdown against the post-call contract."""
        if sidecar.skill != "post-call":
            raise RuntimeOutputError("sidecar skill does not match job")
        expected_version = job.get("skill_version") or "1.0"
        if sidecar.skill_version != expected_version:
            raise RuntimeOutputError("sidecar skill_version does not match job")

        metadata = output_schema.parse_output(
            "post-call",
            markdown,
            mode=sidecar.mode,
            transcript_text=transcript_text,
        )
        if metadata.valid:
            return ValidationResult(status="valid", errors=tuple(metadata.validation_errors))
        return ValidationResult(status="invalid", errors=tuple(metadata.validation_errors))

    async def _hash_storage_object(self, requester_id: uuid.UUID, storage_path: str) -> str | None:
        """Download an existing output object and return its SHA-256 hex digest.

        Returns `None` when the object is missing. Other Storage errors are raised
        so the caller can treat them as retryable runtime errors.
        """
        hasher = hashlib.sha256()
        try:
            stream = await self.storage.download(
                requester_id, storage_path, bucket=storage.OUTPUTS_BUCKET
            )
        except storage.ObjectNotFound:
            return None
        async for chunk in stream:
            hasher.update(chunk)
        return hasher.hexdigest()

    def _content_hash(self, markdown: str) -> str:
        """Return the SHA-256 hex digest of the Markdown artifact."""
        return hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    async def _persist_output(
        self,
        job: dict[str, Any],
        attempt_number: int,
        lease_token: uuid.UUID,
        markdown: str,
        sidecar: SandboxOutputSidecar,
        validation: ValidationResult,
        runtime_version: str,
        model: str,
        token_usage: dict[str, Any],
        cost: float | None,
        context: OrchestratorContext,
        timeout_seconds: int,
    ) -> _PersistedOutput | None:
        """Create the authoritative outputs row and upload the Storage object.

        The metadata row is created first so a retried attempt can recover after a
        successful persistence but failed completion. If Storage upload fails, the
        unvalidated metadata row is rolled back. If a previous attempt left an
        unvalidated row and the object is missing or does not match the expected
        hash, the object is repaired; if it differs from the expected evidence, the
        attempt fails without silently overwriting immutable data.

        Returns `None` if cancellation is requested after the outputs row is
        created or after the Storage upload finishes; the caller must then treat
        the attempt as cancelled and finalize the job.
        """
        job_id = uuid.UUID(job["job_id"])
        org_id = uuid.UUID(job["org_id"])
        account_id = uuid.UUID(job["account_id"])
        transcript_id = uuid.UUID(job["transcript_id"])
        requester_id = uuid.UUID(job["requester_id"])

        output_id = _deterministic_output_id(job_id)
        content_storage_path = _storage_path_for_output(
            org_id, account_id, transcript_id, output_id
        )

        expected_hash = self._content_hash(markdown)
        sidecar_payload = sidecar.model_dump()
        sidecar_payload["validation_status"] = validation.status
        sidecar_payload["validation_errors"] = list(validation.errors)
        sidecar_payload["content_hash"] = expected_hash
        sidecar_payload["content_length"] = len(markdown.encode("utf-8"))
        sidecar_payload["token_usage"] = token_usage
        sidecar_payload["cost"] = cost

        markdown_bytes = markdown.encode("utf-8")

        async def _stream() -> Any:
            yield markdown_bytes

        # 1. Create the metadata row first in its own transaction so a retried
        #    attempt can recover, and so cancellation after this point can be
        #    compensated by deleting the staged row and any uploaded object.
        async with self.db_pool.acquire() as conn:
            try:
                created_id = await conn.fetchval(
                    "SELECT public.create_job_output($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)",
                    job_id,
                    attempt_number,
                    lease_token,
                    output_id,
                    content_storage_path,
                    sidecar.title or "",
                    json.dumps(sidecar_payload),
                    runtime_version,
                    model,
                )
            except Exception as exc:
                logger.warning(
                    "outputs row creation failed for job %s: %s",
                    job_id,
                    type(exc).__name__,
                )
                raise RuntimeError("failed to create output row") from exc

        # create_job_output returns NULL when an unvalidated row already exists
        # and its stored sidecar/path/job/org match. We must still verify that
        # the Storage object exists and matches the expected content.
        row_existed = created_id is None

        # 2. Cancellation can be requested after the outputs row is created but
        #    before the Storage object is uploaded. Clean up the staged row now
        #    so an unvalidated artifact is never left behind.
        if await self._cancel_requested(job, context, timeout_seconds):
            await self._cleanup_staged_output(job, output_id, attempt_number, lease_token)
            return None

        # 3. Upload or recover the Storage object.
        if row_existed:
            try:
                observed_hash = await self._hash_storage_object(
                    requester_id, content_storage_path
                )
                if observed_hash is None:
                    logger.warning(
                        "Output object missing for existing row %s; re-uploading",
                        output_id,
                    )
                    await self.storage.upload(
                        requester_id,
                        content_storage_path,
                        _stream(),
                        "text/markdown; charset=utf-8",
                        bucket=storage.OUTPUTS_BUCKET,
                    )
                elif observed_hash != expected_hash:
                    logger.warning(
                        "Output object hash mismatch for existing row %s",
                        output_id,
                    )
                    raise RuntimeError("existing output object hash mismatch")
            except storage.StorageError as exc:
                logger.warning(
                    "Output recovery failed for job %s: %s",
                    job_id,
                    type(exc).__name__,
                )
                raise RuntimeError("failed to recover output object") from exc
        else:
            try:
                await self.storage.upload(
                    requester_id,
                    content_storage_path,
                    _stream(),
                    "text/markdown; charset=utf-8",
                    bucket=storage.OUTPUTS_BUCKET,
                )
            except storage.StorageError as exc:
                logger.warning(
                    "Output Storage upload failed for job %s: %s",
                    job_id,
                    type(exc).__name__,
                )
                await self._cleanup_staged_output(
                    job, output_id, attempt_number, lease_token
                )
                raise RuntimeError("failed to upload output object") from exc

        # 4. Cancellation can also be requested immediately after the Storage
        #    upload finishes. Remove the staged evidence before returning.
        if await self._cancel_requested(job, context, timeout_seconds):
            await self._cleanup_staged_output(job, output_id, attempt_number, lease_token)
            return None

        return _PersistedOutput(
            output_id=output_id,
            content_storage_path=content_storage_path,
            validation_status=validation.status,
            token_usage=token_usage,
            cost=cost,
            runtime_version=runtime_version,
            model=model,
        )

    async def _cancel_requested(
        self,
        job: dict[str, Any],
        context: OrchestratorContext,
        timeout_seconds: int,
    ) -> bool:
        """Return True if the worker or DB indicates cancellation was requested."""
        if context.cancellation is not None and context.cancellation.is_cancelled():
            return True
        job_id = uuid.UUID(job["job_id"])
        attempt_number = int(job.get("attempt_number") or 1)
        lease_token = uuid.UUID(job["lease_token"])
        try:
            async with self.db_pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT public.worker_heartbeat($1, $2, $3, $4)",
                    job_id,
                    attempt_number,
                    lease_token,
                    timeout_seconds,
                )
        except Exception as exc:
            logger.warning(
                "worker_heartbeat failed for job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return False

    async def _complete_job_sql(
        self,
        job: dict[str, Any],
        attempt_number: int,
        lease_token: uuid.UUID,
        persisted: _PersistedOutput,
    ) -> str:
        """Return 'completed' or 'cancelled' from `public.complete_job`."""
        async with self.db_pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT public.complete_job(
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9
                )
                """,
                uuid.UUID(job["job_id"]),
                attempt_number,
                lease_token,
                persisted.output_id,
                persisted.validation_status,
                json.dumps(persisted.token_usage),
                persisted.cost,
                persisted.runtime_version,
                persisted.model,
            ) or "completed"

    async def _cancel_job_sql(
        self,
        job: dict[str, Any],
        attempt_number: int,
        lease_token: uuid.UUID,
    ) -> None:
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "SELECT public.cancel_job($1, $2, $3)",
                uuid.UUID(job["job_id"]),
                attempt_number,
                lease_token,
            )

    async def _cleanup_staged_output(
        self,
        job: dict[str, Any],
        output_id: uuid.UUID,
        attempt_number: int,
        lease_token: uuid.UUID,
    ) -> None:
        """Delete an unvalidated outputs row and its Storage object."""
        job_id = uuid.UUID(job["job_id"])
        org_id = uuid.UUID(job["org_id"])
        account_id = uuid.UUID(job["account_id"])
        transcript_id = uuid.UUID(job["transcript_id"])
        requester_id = uuid.UUID(job["requester_id"])
        content_storage_path = _storage_path_for_output(
            org_id, account_id, transcript_id, output_id
        )

        async with self.db_pool.acquire() as conn:
            try:
                await conn.fetchval(
                    "SELECT public.delete_job_output($1, $2, $3, $4)",
                    output_id,
                    job_id,
                    attempt_number,
                    lease_token,
                )
            except Exception as exc:
                logger.warning(
                    "delete_job_output failed during cleanup for job %s: %s",
                    job_id,
                    type(exc).__name__,
                )

        try:
            await self.storage.delete(
                requester_id, content_storage_path, bucket=storage.OUTPUTS_BUCKET
            )
        except storage.ObjectNotFound:
            pass
        except storage.StorageError as exc:
            logger.warning(
                "Storage delete failed during cleanup for job %s: %s",
                job_id,
                type(exc).__name__,
            )

    async def _reconcile_existing_output(
        self,
        job: dict[str, Any],
        attempt_number: int,
        lease_token: uuid.UUID,
    ) -> ExecutorResult | None:
        """Finalize an output staged by a previous attempt without rerunning the model.

        If a prior attempt created the outputs row and Storage object but failed
        during `complete_job`, this attempt verifies the object hash, re-validates
        the Markdown, and completes the job. If the object is missing or the hash
        does not match, the stale row is removed and the orchestrator falls back
        to a fresh runtime invocation.
        """
        job_id = uuid.UUID(job["job_id"])
        org_id = uuid.UUID(job["org_id"])
        account_id = uuid.UUID(job["account_id"])
        transcript_id = uuid.UUID(job["transcript_id"])
        requester_id = uuid.UUID(job["requester_id"])
        output_id = _deterministic_output_id(job_id)
        content_storage_path = _storage_path_for_output(
            org_id, account_id, transcript_id, output_id
        )

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM public.get_staged_output($1, $2, $3, $4)",
                job_id,
                attempt_number,
                lease_token,
                output_id,
            )
        if row is None:
            return None

        sidecar_payload = self._json_field({"sidecar": row["sidecar"]}, "sidecar") or {}
        expected_hash = sidecar_payload.get("content_hash")

        try:
            observed_hash = await self._hash_storage_object(
                requester_id, content_storage_path
            )
        except storage.StorageError as exc:
            logger.warning(
                "failed to hash staged object for job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return self._redacted_failure("runtime_error", finalized=False)

        if observed_hash is None:
            logger.warning("staged object missing for job %s; removing stale row", job_id)
            await self._cleanup_staged_output(job, output_id, attempt_number, lease_token)
            return None

        if expected_hash is None or observed_hash != expected_hash:
            logger.warning("staged object hash mismatch for job %s", job_id)
            await self._cleanup_staged_output(job, output_id, attempt_number, lease_token)
            return self._redacted_failure("runtime_error", finalized=False)

        # If the output is already validated (the job was completed on a previous
        # attempt), return the completed result without re-running the runtime.
        if row["validation_status"] == "valid":
            token_usage = sidecar_payload.get("token_usage")
            if not isinstance(token_usage, dict):
                token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            cost = sidecar_payload.get("cost")
            return ExecutorResult(
                output_id=output_id,
                validation_status="valid",
                token_usage=token_usage,
                cost=float(cost) if cost is not None else None,
                runtime_version=row.get("runtime_version") or "post-call-orchestrator",
                model=row.get("model") or "claude-sonnet-4-6",
                finalized=True,
            )

        try:
            stream = await self.storage.download(
                requester_id, content_storage_path, bucket=storage.OUTPUTS_BUCKET
            )
            markdown_bytes = b""
            async for chunk in stream:
                markdown_bytes += chunk
            markdown = markdown_bytes.decode("utf-8", errors="replace")
        except storage.StorageError as exc:
            logger.warning(
                "failed to download staged object for job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return self._redacted_failure("runtime_error", finalized=False)

        try:
            sidecar_fields = {
                k: v
                for k, v in sidecar_payload.items()
                if k in SandboxOutputSidecar.model_fields
            }
            sidecar = SandboxOutputSidecar(**sidecar_fields)
        except Exception as exc:
            logger.warning(
                "staged sidecar invalid for job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return self._redacted_failure("output_error", finalized=False)

        try:
            source_manifest, manifest, prior_paths = await self._resolve_inputs(job)
            input_dir = Path(tempfile.mkdtemp(prefix="se-runtime-input-", dir="/tmp"))
            try:
                transcript_text = await self._materialize_inputs(
                    manifest, source_manifest, prior_paths, requester_id, input_dir
                )
            finally:
                self._make_writable(input_dir)
                shutil.rmtree(input_dir, ignore_errors=True)
        except Exception as exc:
            logger.warning(
                "failed to re-materialize inputs for reconciliation job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return self._redacted_failure("input_error", finalized=False)

        try:
            validation = await self._validate_artifact(job, markdown, sidecar, transcript_text)
        except Exception as exc:
            logger.warning(
                "failed to re-validate staged output for job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return self._redacted_failure("output_error", finalized=False)

        if validation.status != "valid":
            return ExecutorResult(
                output_id=None,
                validation_status="invalid",
                token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                cost=None,
                runtime_version=row.get("runtime_version") or "post-call-orchestrator",
                model=row.get("model") or "claude-sonnet-4-6",
                error_category="output_error",
                error=RedactedFailure(category="output_error").message,
                validation_errors=list(validation.errors),
                finalized=False,
            )

        token_usage = sidecar_payload.get("token_usage")
        if not isinstance(token_usage, dict):
            token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        cost = sidecar_payload.get("cost")
        persisted = _PersistedOutput(
            output_id=output_id,
            content_storage_path=content_storage_path,
            validation_status="valid",
            token_usage=token_usage,
            cost=float(cost) if cost is not None else None,
            runtime_version=row.get("runtime_version") or "post-call-orchestrator",
            model=row.get("model") or "claude-sonnet-4-6",
        )

        try:
            completion_status = await self._complete_job_sql(
                job, attempt_number, lease_token, persisted
            )
        except Exception as exc:
            logger.warning(
                "complete_job failed during reconciliation for job %s: %s",
                job_id,
                type(exc).__name__,
            )
            return self._redacted_failure("runtime_error", finalized=False)

        if completion_status == "cancelled":
            await self._cleanup_staged_output(
                job, output_id, attempt_number, lease_token
            )
            return self._redacted_failure("cancelled", finalized=True)

        return ExecutorResult(
            output_id=output_id,
            validation_status="valid",
            token_usage=persisted.token_usage,
            cost=persisted.cost,
            runtime_version=persisted.runtime_version,
            model=persisted.model,
            finalized=True,
        )

    @staticmethod
    def _runtime_metadata(
        job: dict[str, Any], runtime_result: RuntimeResult
    ) -> tuple[str, str, dict[str, Any], float | None]:
        """Return (runtime_version, model, token_usage, cost) from the runtime result."""
        meta = runtime_result.execution_metadata
        runtime_version = meta.runtime_version or job.get("runtime_version") or "slice5b1"
        model = meta.model or job.get("model") or "claude-sonnet-4-6"
        token_usage = meta.token_usage.model_dump() if meta.token_usage else {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        cost = meta.cost
        return runtime_version, model, token_usage, cost

    async def execute(
        self,
        job: dict[str, Any],
        context: OrchestratorContext | None = None,
    ) -> ExecutorResult:
        """Run one post-call attempt from a claimed job record."""
        context = context or OrchestratorContext()
        attempt_number = int(job.get("attempt_number") or 1)
        lease_token = uuid.UUID(str(job["lease_token"]))
        timeout_seconds = int(job.get("timeout_seconds") or config.WORKER_TIMEOUT_SECONDS)

        input_dir: Path | None = None
        output_dir: Path | None = None
        try:
            # A previous attempt may have staged the output and then failed to
            # complete. Finalize that evidence rather than invoking the runtime again.
            reconciled = await self._reconcile_existing_output(
                job, attempt_number, lease_token
            )
            if reconciled is not None:
                return reconciled

            if await self._cancel_requested(job, context, timeout_seconds):
                await self._cancel_job_sql(job, attempt_number, lease_token)
                return self._redacted_failure("cancelled", finalized=True)

            source_manifest, manifest, prior_paths = await self._resolve_inputs(job)

            input_dir = Path(tempfile.mkdtemp(prefix="se-runtime-input-", dir="/tmp"))
            output_dir = Path(tempfile.mkdtemp(prefix="se-runtime-output-", dir="/tmp"))

            requester_id = uuid.UUID(str(job["requester_id"]))
            transcript_text = await self._materialize_inputs(
                manifest, source_manifest, prior_paths, requester_id, input_dir
            )

            runtime_job = self._build_runtime_job(job, manifest, input_dir, output_dir)

            cancellation = _CancellationToken(external=context.cancellation)
            runtime_result = await self.runtime.execute(runtime_job, cancellation)

            if runtime_result.failure is not None:
                return ExecutorResult(
                    output_id=None,
                    validation_status="invalid",
                    token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    cost=None,
                    runtime_version=job.get("runtime_version") or "slice5b1",
                    model=job.get("model") or "claude-sonnet-4-6",
                    error_category=runtime_result.failure.category,
                    error=runtime_result.failure.message,
                    finalized=False,
                )

            try:
                markdown, sidecar = await self._read_runtime_output(output_dir)

                if runtime_result.output_artifact != markdown:
                    return ExecutorResult(
                        output_id=None,
                        validation_status="invalid",
                        token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        cost=None,
                        runtime_version=job.get("runtime_version") or "slice5b1",
                        model=job.get("model") or "claude-sonnet-4-6",
                        error_category="output_error",
                        error=RedactedFailure(category="output_error").message,
                        validation_errors=["runtime output_artifact does not match output.md"],
                        finalized=False,
                    )

                if (
                    runtime_result.sidecar is not None
                    and runtime_result.sidecar.model_dump() != sidecar.model_dump()
                ):
                    return ExecutorResult(
                        output_id=None,
                        validation_status="invalid",
                        token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        cost=None,
                        runtime_version=job.get("runtime_version") or "slice5b1",
                        model=job.get("model") or "claude-sonnet-4-6",
                        error_category="output_error",
                        error=RedactedFailure(category="output_error").message,
                        validation_errors=["runtime sidecar does not match sidecar.json"],
                        finalized=False,
                    )

                validation = await self._validate_artifact(
                    job, markdown, sidecar, transcript_text
                )
            except Exception as exc:
                logger.warning(
                    "Post-call output validation failed for job %s: %s",
                    job.get("job_id", "unknown"),
                    type(exc).__name__,
                )
                return self._redacted_failure("output_error", finalized=False)

            if validation.status != "valid":
                return ExecutorResult(
                    output_id=None,
                    validation_status="invalid",
                    token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    cost=None,
                    runtime_version=job.get("runtime_version") or "slice5b1",
                    model=job.get("model") or "claude-sonnet-4-6",
                    error_category="output_error",
                    error=RedactedFailure(category="output_error").message,
                    validation_errors=list(validation.errors),
                    finalized=False,
                )

            if await self._cancel_requested(job, context, timeout_seconds):
                await self._cancel_job_sql(job, attempt_number, lease_token)
                return self._redacted_failure("cancelled", finalized=True)

            runtime_version, model, token_usage, cost = self._runtime_metadata(
                job, runtime_result
            )

            try:
                persisted = await self._persist_output(
                    job,
                    attempt_number,
                    lease_token,
                    markdown,
                    sidecar,
                    validation,
                    runtime_version,
                    model,
                    token_usage,
                    cost,
                    context,
                    timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "Post-call output persistence failed for job %s: %s",
                    job.get("job_id", "unknown"),
                    type(exc).__name__,
                )
                return self._redacted_failure("runtime_error", finalized=False)

            if persisted is None:
                # _persist_output already cleaned up the staged row/object.
                await self._cancel_job_sql(job, attempt_number, lease_token)
                return self._redacted_failure("cancelled", finalized=True)

            try:
                completion_status = await self._complete_job_sql(
                    job, attempt_number, lease_token, persisted
                )
            except Exception as exc:
                logger.warning(
                    "complete_job failed for job %s attempt %s: %s",
                    job.get("job_id", "unknown"),
                    job.get("attempt_number", "unknown"),
                    type(exc).__name__,
                )
                # The staged output row and object remain. The next attempt will
                # reconcile them without invoking the runtime again.
                return self._redacted_failure("runtime_error", finalized=False)

            if completion_status == "cancelled":
                await self._cleanup_staged_output(
                    job, persisted.output_id, attempt_number, lease_token
                )
                return self._redacted_failure("cancelled", finalized=True)

            return ExecutorResult(
                output_id=persisted.output_id,
                validation_status="valid",
                token_usage=persisted.token_usage,
                cost=persisted.cost,
                runtime_version=persisted.runtime_version,
                model=persisted.model,
                finalized=True,
            )
        except Exception as exc:
            logger.warning(
                "Post-call orchestration failed for job %s attempt %s: %s",
                job.get("job_id", "unknown"),
                job.get("attempt_number", "unknown"),
                type(exc).__name__,
            )
            return self._redacted_failure("input_error", finalized=False)
        finally:
            if input_dir is not None:
                self._make_writable(input_dir)
                shutil.rmtree(input_dir, ignore_errors=True)
            if output_dir is not None:
                self._make_writable(output_dir)
                shutil.rmtree(output_dir, ignore_errors=True)


class PostCallExecutor:
    """Adapter that lets `PostCallOrchestrator` satisfy the worker `Executor` protocol."""

    def __init__(
        self,
        runtime: SkillRuntime,
        db_pool: asyncpg.Pool,
        storage_backend: storage.StorageBackend | None = None,
    ) -> None:
        self.orchestrator = PostCallOrchestrator(
            runtime=runtime,
            db_pool=db_pool,
            storage_backend=storage_backend,
        )
        self._cancellation: CancellationSource | None = None

    def set_cancellation(self, event: asyncio.Event | None) -> None:
        """Bridge the worker's `asyncio.Event` into the orchestrator context."""
        if event is None:
            self._cancellation = None
        else:
            self._cancellation = _AsyncioEventCancellationSource(event)

    async def execute(self, job: dict[str, Any]) -> ExecutorResult:
        context = OrchestratorContext(cancellation=self._cancellation)
        return await self.orchestrator.execute(job, context)
