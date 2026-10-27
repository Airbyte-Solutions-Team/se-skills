"""Dedicated constrained Claude Code executor for canonical opportunity state."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from opportunity_state import OpportunityStateCandidate
from services.transcription_service import ResolvedTranscriptEvidence

logger = logging.getLogger(__name__)

UPDATER_VERSION = "opportunity-overview-complete-v1"
MINIMUM_CLAUDE_VERSION = "2.1.272"
_CLAUDE_VERSION_RE = re.compile(r"^\s*(?P<version>\d+\.\d+\.\d+)(?:\s|\(|$)")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class CanonicalStateExecutionError(Exception):
    """A safe executor failure that never contains evidence or raw CLI output."""

    def __init__(self, code: str, detail: str, *, hints: list[str] | None = None) -> None:
        self.code = code
        self.detail = detail
        # Evidence-free validation diagnostics (field path + rule message) that may be fed back to
        # the model on a retry. Never contains model output values.
        self.hints = hints or []
        super().__init__(detail)


@dataclass(frozen=True)
class CanonicalStateExecutionRequest:
    account: str
    opportunity_slug: str
    opportunity_name: str
    opportunity_metadata: dict[str, Any]
    metadata_source_id: str
    transcripts: list[ResolvedTranscriptEvidence]
    base_state: OpportunityStateCandidate | None = None
    base_version_id: str | None = None
    base_revision: int | None = None

    def __post_init__(self) -> None:
        values = (self.base_state, self.base_version_id, self.base_revision)
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("update execution requires a complete base relationship")


@dataclass(frozen=True)
class CanonicalStateExecutionResult:
    candidate: OpportunityStateCandidate
    model: str
    cli_version: str
    runtime: str = "claude-code-restricted"


class CanonicalStateExecutor(Protocol):
    async def execute(self, request: CanonicalStateExecutionRequest) -> CanonicalStateExecutionResult: ...


class FakeCanonicalStateExecutor:
    """Deterministic injectable executor used by automated tests."""

    def __init__(self, candidate: OpportunityStateCandidate, *, cli_version: str = "fake-cli-version") -> None:
        self.candidate = candidate
        self.cli_version = cli_version
        self.requests: list[CanonicalStateExecutionRequest] = []

    async def execute(self, request: CanonicalStateExecutionRequest) -> CanonicalStateExecutionResult:
        self.requests.append(request)
        return CanonicalStateExecutionResult(
            candidate=self.candidate,
            model="fake-canonical-state-model",
            cli_version=self.cli_version,
        )


class ClaudeCanonicalStateExecutor:
    """Run Claude once, with no tools or customizations, from an isolated cwd."""

    def __init__(
        self,
        *,
        model: str,
        forbidden_roots: list[Path],
        executable: str = "claude",
        timeout_seconds: float = 600.0,
        version_timeout_seconds: float = 5.0,
        max_stdin_bytes: int = 900_000,
        max_stdout_bytes: int = 1_000_000,
        max_stderr_bytes: int = 65_536,
        max_version_stdout_bytes: int = 256,
        max_version_stderr_bytes: int = 1_024,
        max_thinking_tokens: int | None = 0,
        max_attempts: int = 2,
    ) -> None:
        self.max_attempts = max_attempts
        # The overview is schema-constrained extraction whose evidence refs are re-validated after
        # generation, so hidden extended thinking only adds latency: measured ~50% of output tokens
        # and ~2x wall time (287s vs 146s) for identical output. None keeps the CLI default.
        self.max_thinking_tokens = max_thinking_tokens
        self.model = model
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.version_timeout_seconds = version_timeout_seconds
        self.max_stdin_bytes = max_stdin_bytes
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_version_stdout_bytes = max_version_stdout_bytes
        self.max_version_stderr_bytes = max_version_stderr_bytes
        self.forbidden_roots = [Path(root).resolve() for root in forbidden_roots]

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return OpportunityStateCandidate.model_json_schema()

    def version_command(self, executable: str | None = None) -> list[str]:
        """Return the evidence-free runtime preflight command."""
        return [executable or shutil.which(self.executable) or self.executable, "--version"]

    def child_env(self) -> dict[str, str] | None:
        """Environment for the generation subprocess (None inherits the parent's unchanged)."""
        if self.max_thinking_tokens is None:
            return None
        return {**os.environ, "MAX_THINKING_TOKENS": str(self.max_thinking_tokens)}

    def command(self, executable: str | None = None) -> list[str]:
        """Return the command contract audited against Claude Code 2.1.272+, without any evidence."""
        return [
            executable or shutil.which(self.executable) or self.executable,
            "-p",
            "--max-turns",
            "1",
            "--restricted",
            "--safe-mode",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(self.json_schema(), separators=(",", ":")),
            "--model",
            self.model,
        ]

    @staticmethod
    def _prompt(request: CanonicalStateExecutionRequest, rejected_errors: list[str] | None = None) -> bytes:
        is_update = request.base_state is not None
        payload = {
            "task": (
                (
                    "Update the canonical opportunity overview. Treat prior_canonical_state as the validated "
                    "baseline, not as an evidence source. Reconcile only the supplied current metadata and "
                    "explicitly selected delta transcripts. Preserve prior facts and their evidence references "
                    "unless new evidence changes, contradicts, or makes them obsolete. Do not cite the baseline "
                    "JSON as evidence. Use unknown, partial, or conflicting states instead of inventing a "
                    "resolution. "
                ) if is_update else (
                    "Create the first canonical opportunity overview from only the supplied opportunity metadata "
                    "and explicitly selected transcripts. "
                )
            ) + (
                "Return exactly the requested schema. Do not invent facts. Do not reproduce transcript text or "
                "quotations. Every material supported claim must cite an authorized source id; transcript "
                "locators may contain only a speaker/time or line-range pointer, never quoted evidence. "
                "Generated outputs are not evidence and are not supplied. Complete all four business_case "
                "areas, all eight meddpicc dimensions, and the stakeholders map. When concrete evidence-backed "
                "supporting facts exist for a business_case area beyond its knowledge value, add up to six "
                "short points capturing them; omit points entirely rather than restating the value field or "
                "inventing a fact when no such facts exist. Add a stakeholder only when "
                "authorized evidence establishes the person; never invent a name, title, category, influence, "
                "engagement, stance, or sentiment. Every stakeholder requires evidence_refs. Record influence, "
                "engagement, stance, blocker status or reason, recommended_next_step, and explicit "
                "missing_information honestly, using unknown enum values or null when evidence is absent. "
                "Explicitly list champion, economic_buyer, and technical_decision_maker in missing_key_roles "
                "when no authorized evidence establishes those categories. missing_key_roles may contain only "
                "those three categories and never a category also held by an established stakeholder; note any "
                "other missing stakeholder category (e.g. security_approver, procurement, end_user) in the map's "
                "general missing_information list instead. Use each knowledge claim's explicit unknown, partial, "
                "or conflicting state when evidence is absent, incomplete, or contradictory. A claim's value must "
                "be null whenever its knowledge_state is unknown or not_applicable -- never fill in a guess, "
                "placeholder, or explanatory sentence there; a value is required only for known, partial, or "
                "conflicting states. A claim's confirmation and evidence_refs must agree: confirmation "
                "evidence_backed requires at least one evidence ref; confirmation unknown requires an empty "
                "evidence_refs list, so a claim that cites any evidence must use evidence_backed or inferred, "
                "never unknown. Record concrete "
                "missing_information for incomplete business-case areas and MEDDPICC dimensions, and record "
                "suggested_discovery for every MEDDPICC gap."
            ),
            "opportunity": {
                "account": request.account,
                "slug": request.opportunity_slug,
                "name": request.opportunity_name,
                "metadata_source_id": request.metadata_source_id,
                "metadata": request.opportunity_metadata,
            },
            "selected_transcripts": [
                {
                    "source_id": item.evidence_id,
                    "sha256": item.sha256,
                    "content": item.content.decode("utf-8", errors="strict"),
                }
                for item in request.transcripts
            ],
        }
        if rejected_errors:
            payload["previous_attempt_rejected"] = {
                "errors": rejected_errors,
                "instruction": (
                    "Your previous response failed validation with the errors listed. Return the complete "
                    "corrected object and fix every listed error."
                ),
            }
        if is_update:
            payload["base"] = {
                "version_id": request.base_version_id,
                "revision": request.base_revision,
                "prior_canonical_state": request.base_state.model_dump(mode="json"),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _assert_isolated(self, cwd: Path) -> None:
        resolved = cwd.resolve()
        for root in self.forbidden_roots:
            if resolved == root or root in resolved.parents or resolved in root.parents:
                raise CanonicalStateExecutionError(
                    "unsafe_runtime_directory", "Canonical-state execution could not create an isolated workspace."
                )

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader,
        limit: int,
        code: str,
        detail: str = "Canonical-state runtime exceeded its output limit.",
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(16_384)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise CanonicalStateExecutionError(code, detail)
            chunks.append(chunk)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _parse_verified_version(stdout: bytes) -> str:
        try:
            text = stdout.decode("utf-8", errors="strict").strip()
        except UnicodeError as exc:
            raise CanonicalStateExecutionError(
                "runtime_version_invalid", "Claude Code returned an invalid version response."
            ) from exc
        match = _CLAUDE_VERSION_RE.match(text)
        if match is None:
            raise CanonicalStateExecutionError(
                "runtime_version_invalid", "Claude Code returned an invalid version response."
            )
        actual = match.group("version")
        if _version_tuple(actual) < _version_tuple(MINIMUM_CLAUDE_VERSION):
            raise CanonicalStateExecutionError(
                "runtime_version_unsupported",
                f"Claude Code {MINIMUM_CLAUDE_VERSION} or newer is required to create an overview.",
            )
        return actual

    async def _verify_cli_version(
        self,
        *,
        executable: str,
        cwd: Path,
        subprocess_options: dict[str, Any],
    ) -> str:
        """Verify the CLI meets the minimum audited version before constructing or sending evidence."""
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.version_command(executable),
                    cwd=str(cwd),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **subprocess_options,
                )
            except FileNotFoundError as exc:
                raise CanonicalStateExecutionError(
                    "runtime_unavailable", f"Claude Code {MINIMUM_CLAUDE_VERSION} or newer is required to create an overview."
                ) from exc
            assert proc.stdout is not None and proc.stderr is not None
            safe_detail = "Claude Code version check exceeded its output limit."
            stdout_task = asyncio.create_task(self._read_bounded(
                proc.stdout,
                self.max_version_stdout_bytes,
                "runtime_version_output_too_large",
                safe_detail,
            ))
            stderr_task = asyncio.create_task(self._read_bounded(
                proc.stderr,
                self.max_version_stderr_bytes,
                "runtime_version_output_too_large",
                safe_detail,
            ))
            try:
                return_code, stdout, _stderr = await asyncio.wait_for(
                    asyncio.gather(proc.wait(), stdout_task, stderr_task),
                    timeout=self.version_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise CanonicalStateExecutionError(
                    "runtime_version_timeout", "Claude Code version check timed out."
                ) from exc
            if return_code != 0:
                raise CanonicalStateExecutionError(
                    "runtime_version_failed", "Claude Code version could not be verified."
                )
            return self._parse_verified_version(stdout)
        except CanonicalStateExecutionError:
            if proc is not None:
                await self._terminate(proc)
            raise
        finally:
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()

    @staticmethod
    def _extract_candidate(stdout: bytes) -> OpportunityStateCandidate:
        try:
            outer = json.loads(stdout.decode("utf-8", errors="strict"))
            candidate: Any = outer
            if isinstance(outer, dict) and "structured_output" in outer:
                candidate = outer["structured_output"]
            elif isinstance(outer, dict) and "result" in outer:
                candidate = outer["result"]
            if isinstance(candidate, str):
                candidate_text = candidate.strip()
                lines = candidate_text.splitlines()
                if (
                    len(lines) >= 3
                    and lines[0].strip().lower() in {"```", "```json"}
                    and lines[-1].strip() == "```"
                ):
                    candidate_text = "\n".join(lines[1:-1]).strip()
                candidate = json.loads(candidate_text)
            elif not isinstance(candidate, dict):
                raise TypeError("candidate must be a JSON object")
            return OpportunityStateCandidate.model_validate(candidate)
        except ValidationError as exc:
            # `msg`/`type`/`loc` are safe to log: Pydantic v2 never embeds the actual offending
            # value in `msg` (that only appears in `input`/`ctx`, which we deliberately omit,
            # since it could echo back model-generated, evidence-derived text).
            safe_errors = [
                {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"], "msg": e["msg"]}
                for e in exc.errors()
            ]
            logger.warning("Canonical-state candidate failed schema validation: %s", safe_errors)
            hints = [f"{e['loc']}: {e['msg']}"[:300] for e in safe_errors[:20]]
            raise CanonicalStateExecutionError(
                "invalid_model_output", "Claude returned an invalid canonical-state candidate.", hints=hints
            ) from exc
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Canonical-state candidate could not be parsed: %s: %s", type(exc).__name__, exc)
            raise CanonicalStateExecutionError(
                "invalid_model_output", "Claude returned an invalid canonical-state candidate."
            ) from exc

    async def execute(self, request: CanonicalStateExecutionRequest) -> CanonicalStateExecutionResult:
        """Run the model, retrying once with the rejection reasons if it returns an invalid candidate.

        Cross-field rules (e.g. evidence_backed claims must cite evidence) can't be expressed in the
        JSON schema, so the model intermittently violates them (~1 in 3 runs observed). Only
        ``invalid_model_output`` is retried; timeouts and runtime failures are not.
        """
        rejected_errors: list[str] | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._execute_once(request, rejected_errors)
            except CanonicalStateExecutionError as exc:
                if exc.code != "invalid_model_output" or attempt >= self.max_attempts:
                    raise
                logger.warning("Canonical-state output rejected (attempt %d/%d); retrying", attempt, self.max_attempts)
                rejected_errors = exc.hints or None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _execute_once(
        self, request: CanonicalStateExecutionRequest, rejected_errors: list[str] | None = None
    ) -> CanonicalStateExecutionResult:
        temp_dir = Path(tempfile.mkdtemp(prefix="se-opportunity-state-"))
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            self._assert_isolated(temp_dir)
            executable = shutil.which(self.executable)
            if executable is None:
                raise CanonicalStateExecutionError(
                    "runtime_unavailable", f"Claude Code {MINIMUM_CLAUDE_VERSION} or newer is required to create an overview."
                )
            subprocess_options: dict[str, Any] = {}
            if os.name == "nt":
                subprocess_options["creationflags"] = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                subprocess_options["start_new_session"] = True
            actual_cli_version = await self._verify_cli_version(
                executable=executable,
                cwd=temp_dir,
                subprocess_options=subprocess_options,
            )
            stdin = self._prompt(request, rejected_errors) if rejected_errors else self._prompt(request)
            if len(stdin) > self.max_stdin_bytes:
                raise CanonicalStateExecutionError(
                    "input_too_large", "The selected evidence is too large for one overview creation."
                )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(executable),
                    cwd=str(temp_dir),
                    env=self.child_env(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **subprocess_options,
                )
            except FileNotFoundError as exc:
                raise CanonicalStateExecutionError(
                    "runtime_unavailable", "Claude Code 2.1.272 is required to create an overview."
                ) from exc
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            stdout_task = asyncio.create_task(
                self._read_bounded(proc.stdout, self.max_stdout_bytes, "stdout_too_large")
            )
            stderr_task = asyncio.create_task(
                self._read_bounded(proc.stderr, self.max_stderr_bytes, "stderr_too_large")
            )

            async def exchange() -> list[Any]:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
                return await asyncio.gather(proc.wait(), stdout_task, stderr_task)

            try:
                results = await asyncio.wait_for(exchange(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise CanonicalStateExecutionError(
                    "runtime_timeout", "Overview creation timed out; no state was saved."
                ) from exc
            return_code, stdout, _stderr = results
            if return_code != 0:
                raise CanonicalStateExecutionError(
                    "runtime_failed", "Claude could not create a valid overview; no state was saved."
                )
            candidate = self._extract_candidate(stdout)
            return CanonicalStateExecutionResult(
                candidate=candidate,
                model=self.model,
                cli_version=actual_cli_version,
            )
        except CanonicalStateExecutionError:
            if proc is not None:
                await self._terminate(proc)
            raise
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            if proc is not None:
                await self._terminate(proc)
            raise CanonicalStateExecutionError(
                "runtime_failed", "Claude could not create a valid overview; no state was saved."
            ) from exc
        finally:
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            shutil.rmtree(temp_dir, ignore_errors=True)
