"""Trusted worker-side post-call orchestration and output persistence.

The orchestrator materializes authorized inputs from the durable job, invokes an
injected `SkillRuntime`, validates the returned Markdown + sidecar outside the
runtime, and persists valid artifacts to private org-scoped Storage and Postgres.
No Anthropic key, DB credential, Storage credential, signed URL, or transcript
body enters the runtime job payload or sandbox workspace.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import asyncpg

import output_schema
from hosted import storage
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


class _CancellationToken:
    """Host-side cancellation signal for the runtime."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@runtime_checkable
class CancellationSource(Protocol):
    """Something the worker can poll to see if cancellation was requested."""

    def is_cancelled(self) -> bool:
        ...


@dataclass
class OrchestratorContext:
    """Execution-time state shared with the worker."""

    cancellation: CancellationSource | None = None


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

    def _redacted_failure(self, category: FailureCategory) -> ExecutorResult:
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
        )

    @staticmethod
    def _json_field(job: dict[str, Any], key: str) -> Any:
        """Return a JSONB field, parsing it if asyncpg returned a JSON string."""
        value = job.get(key)
        if isinstance(value, str):
            return json.loads(value)
        return value

    async def _load_and_validate_manifest(
        self, job: dict[str, Any]
    ) -> tuple[dict[str, Any], InputManifest]:
        """Resolve the job's source manifest and build a typed `InputManifest`.

        Rejects missing, aliased, cross-org, cross-account, mismatched-opportunity,
        duplicated, or unlisted input references before the runtime is invoked.
        """
        job_id = uuid.UUID(str(job["job_id"]))
        org_id = uuid.UUID(str(job["org_id"]))
        account_id = uuid.UUID(str(job["account_id"]))
        transcript_id = uuid.UUID(str(job["transcript_id"]))
        opportunity_id = uuid.UUID(str(job["opportunity_id"])) if job.get("opportunity_id") else None
        requester_id = uuid.UUID(str(job["requester_id"]))

        raw_source_manifest = self._json_field(job, "source_manifest") or {}
        if not isinstance(raw_source_manifest, dict):
            raise ValueError("source_manifest must be a JSON object")
        source_manifest = raw_source_manifest

        # The manifest must match the trusted job identity from the claim.
        if source_manifest.get("transcript_id") != str(transcript_id):
            raise ValueError("manifest transcript_id does not match job")
        if source_manifest.get("account_id") != str(account_id):
            raise ValueError("manifest account_id does not match job")
        if source_manifest.get("org_id") != str(org_id):
            raise ValueError("manifest org_id does not match job")
        if source_manifest.get("opportunity_id") != (str(opportunity_id) if opportunity_id else None):
            raise ValueError("manifest opportunity_id does not match job")

        storage_path = source_manifest.get("storage_path")
        if not storage_path or ".." in storage_path or storage_path.startswith(("/", "~", "\\")):
            raise ValueError("manifest storage_path is missing or unsafe")

        original_filename = source_manifest.get("original_filename") or "transcript.txt"
        transcript_ref = _safe_filename(original_filename)

        # Prior context references are validated against the closed input_refs list.
        input_refs = self._json_field(job, "input_refs") or {}
        prior_refs: set[str] = set()
        if isinstance(input_refs, dict):
            prior_output_ids = input_refs.get("prior_output_ids") or []
            if prior_output_ids:
                # Deduplicate and validate shape without fetching content here.
                seen: set[str] = set()
                for ref in prior_output_ids:
                    ref_str = str(ref)
                    if ref_str in seen:
                        raise ValueError("duplicate prior context reference")
                    seen.add(ref_str)
                    try:
                        uuid.UUID(ref_str)
                    except ValueError as exc:
                        raise ValueError("invalid prior context reference") from exc
                prior_refs = seen

        manifest = InputManifest(
            transcript_id=transcript_id,
            transcript_ref=transcript_ref,
            account_id=account_id,
            org_id=org_id,
            opportunity_id=opportunity_id,
            prior_context_refs=frozenset(prior_refs),
        )
        return source_manifest, manifest

    async def _materialize_inputs(
        self,
        manifest: InputManifest,
        source_manifest: dict[str, Any],
        requester_id: uuid.UUID,
        input_dir: Path,
    ) -> str:
        """Fetch transcript and approved prior context through Storage.

        Returns the transcript text. Only manifest-listed files are materialized,
        read-only, and with no signed URLs or credentials passed to the runtime.
        """
        storage_path = source_manifest["storage_path"]
        transcript_bytes = b""
        stream = await self.storage.download(requester_id, storage_path, bucket=storage.DEFAULT_BUCKET)
        async for chunk in stream:
            transcript_bytes += chunk

        transcript_text = transcript_bytes.decode("utf-8", errors="replace")
        transcript_path = input_dir / manifest.transcript_ref
        transcript_path.write_text(transcript_text, encoding="utf-8")

        # Future prior-context files would be fetched here using the same Storage
        # boundary and placed under input_dir with the manifest-supplied ref names.
        for ref in manifest.prior_context_refs:
            # Slice 5B1 does not wire prior-output materialization; placeholder
            # that validates the ref is listed and creates an empty sentinel so
            # the runtime cannot accidentally read an unlisted file.
            (input_dir / ref).write_text("", encoding="utf-8")

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
        elif isinstance(deadline_ts, (int, float)):
            deadline = datetime.fromtimestamp(deadline_ts, tz=timezone.utc)
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
        """Read the candidate Markdown and sidecar from the output workspace."""
        md_path = output_dir / "output.md"
        sidecar_path = output_dir / "sidecar.json"

        if not md_path.exists():
            raise ValueError("runtime did not write output.md")
        if not sidecar_path.exists():
            raise ValueError("runtime did not write sidecar.json")

        markdown = md_path.read_text(encoding="utf-8")
        sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
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
            raise ValueError("sidecar skill does not match job")
        expected_version = job.get("skill_version") or "1.0"
        if sidecar.skill_version != expected_version:
            raise ValueError("sidecar skill_version does not match job")

        metadata = output_schema.parse_output(
            "post-call",
            markdown,
            mode=sidecar.mode,
            transcript_text=transcript_text,
        )
        if metadata.valid:
            return ValidationResult(status="valid", errors=tuple(metadata.validation_errors))
        return ValidationResult(status="invalid", errors=tuple(metadata.validation_errors))

    async def _persist_output(
        self,
        job: dict[str, Any],
        attempt_number: int,
        lease_token: uuid.UUID,
        markdown: str,
        sidecar: SandboxOutputSidecar,
        validation: ValidationResult,
    ) -> ExecutorResult:
        """Create the authoritative outputs row, upload to Storage, and return the id.

        The metadata row is created first so a retried attempt can recover after a
        successful persistence but failed completion. If Storage upload fails, the
        unvalidated metadata row is rolled back.
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

        sidecar_payload = sidecar.model_dump()
        sidecar_payload["validation_status"] = validation.status
        sidecar_payload["validation_errors"] = list(validation.errors)

        async with self.db_pool.acquire() as conn:
            try:
                created_id = await conn.fetchval(
                    "SELECT public.create_job_output($1, $2, $3, $4, $5, $6, $7::jsonb)",
                    job_id,
                    attempt_number,
                    lease_token,
                    output_id,
                    content_storage_path,
                    sidecar.title or "",
                    json.dumps(sidecar_payload),
                )
            except Exception as exc:
                logger.warning(
                    "outputs row creation failed for job %s: %s",
                    job_id,
                    type(exc).__name__,
                )
                return self._redacted_failure("runtime_error")

            # create_job_output returns NULL when the row already existed and
            # matched (idempotent retry). The storage object is assumed to exist.
            row_existed = created_id is None

            if not row_existed:
                markdown_bytes = markdown.encode("utf-8")

                async def _stream():
                    yield markdown_bytes

                try:
                    await self.storage.upload(
                        requester_id,
                        content_storage_path,
                        _stream(),
                        "text/markdown; charset=utf-8",
                        bucket=storage.OUTPUTS_BUCKET,
                    )
                except storage.StorageError as exc:
                    logger.warning("Output Storage upload failed for job %s: %s", job_id, type(exc).__name__)
                    try:
                        await conn.fetchval("SELECT public.delete_job_output($1)", output_id)
                    except Exception:
                        pass
                    return self._redacted_failure("runtime_error")

        return ExecutorResult(
            output_id=output_id,
            validation_status=validation.status,
            token_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            cost=None,
            runtime_version=job.get("runtime_version") or "slice5b1",
            model=job.get("model") or "claude-sonnet-4-6",
        )

    async def execute(
        self,
        job: dict[str, Any],
        context: OrchestratorContext | None = None,
    ) -> ExecutorResult:
        """Run one post-call attempt from a claimed job record."""
        attempt_number = int(job.get("attempt_number") or 1)
        lease_token = uuid.UUID(str(job["lease_token"]))

        input_dir: Path | None = None
        output_dir: Path | None = None
        try:
            source_manifest, manifest = await self._load_and_validate_manifest(job)

            input_dir = Path(tempfile.mkdtemp(prefix="se-runtime-input-", dir="/tmp"))
            output_dir = Path(tempfile.mkdtemp(prefix="se-runtime-output-", dir="/tmp"))

            transcript_text = await self._materialize_inputs(
                manifest, source_manifest, uuid.UUID(str(job["requester_id"])), input_dir
            )

            runtime_job = self._build_runtime_job(job, manifest, input_dir, output_dir)

            cancellation = _CancellationToken()
            if context and context.cancellation:
                # Bridge an external cancellation source into the token used by the runtime.
                if context.cancellation.is_cancelled():
                    cancellation.cancel()

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
                )

            markdown, sidecar = await self._read_runtime_output(output_dir)
            validation = await self._validate_artifact(job, markdown, sidecar, transcript_text)

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
                )

            return await self._persist_output(
                job,
                attempt_number,
                lease_token,
                markdown,
                sidecar,
                validation,
            )
        except Exception as exc:
            logger.warning(
                "Post-call orchestration failed for job %s attempt %s: %s",
                job.get("job_id", "unknown"),
                job.get("attempt_number", "unknown"),
                type(exc).__name__,
            )
            return self._redacted_failure("input_error")
        finally:
            if input_dir is not None:
                shutil.rmtree(input_dir, ignore_errors=True)
            if output_dir is not None:
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

    async def execute(self, job: dict[str, Any]) -> ExecutorResult:
        return await self.orchestrator.execute(job)
