"""Dedicated constrained Claude Code executor for canonical opportunity state."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from opportunity_state import OpportunityStateCandidate
from services.transcription_service import ResolvedTranscriptEvidence


UPDATER_VERSION = "opportunity-overview-slice-2a-v1"
VERIFIED_CLAUDE_VERSION = "2.1.272"


class CanonicalStateExecutionError(Exception):
    """A safe executor failure that never contains evidence or raw CLI output."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class CanonicalStateExecutionRequest:
    account: str
    opportunity_slug: str
    opportunity_name: str
    opportunity_metadata: dict[str, Any]
    metadata_source_id: str
    transcripts: list[ResolvedTranscriptEvidence]


@dataclass(frozen=True)
class CanonicalStateExecutionResult:
    candidate: OpportunityStateCandidate
    model: str
    runtime: str = "claude-code-restricted"
    cli_version: str = VERIFIED_CLAUDE_VERSION


class CanonicalStateExecutor(Protocol):
    async def execute(self, request: CanonicalStateExecutionRequest) -> CanonicalStateExecutionResult: ...


class FakeCanonicalStateExecutor:
    """Deterministic injectable executor used by automated tests."""

    def __init__(self, candidate: OpportunityStateCandidate) -> None:
        self.candidate = candidate
        self.requests: list[CanonicalStateExecutionRequest] = []

    async def execute(self, request: CanonicalStateExecutionRequest) -> CanonicalStateExecutionResult:
        self.requests.append(request)
        return CanonicalStateExecutionResult(candidate=self.candidate, model="fake-canonical-state-model")


class ClaudeCanonicalStateExecutor:
    """Run Claude once, with no tools or customizations, from an isolated cwd."""

    def __init__(
        self,
        *,
        model: str,
        forbidden_roots: list[Path],
        executable: str = "claude",
        timeout_seconds: float = 180.0,
        max_stdin_bytes: int = 900_000,
        max_stdout_bytes: int = 1_000_000,
        max_stderr_bytes: int = 65_536,
    ) -> None:
        self.model = model
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.max_stdin_bytes = max_stdin_bytes
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.forbidden_roots = [Path(root).resolve() for root in forbidden_roots]

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return OpportunityStateCandidate.model_json_schema()

    def command(self) -> list[str]:
        """Return the audited 2.1.272 command contract, without any evidence."""
        executable = shutil.which(self.executable) or self.executable
        return [
            executable,
            "-p",
            "--restricted",
            "--safe-mode",
            "--tools",
            "",
            "--disallowedTools",
            "*,mcp__*",
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
    def _prompt(request: CanonicalStateExecutionRequest) -> bytes:
        payload = {
            "task": (
                "Create the first canonical opportunity overview from only the supplied opportunity metadata "
                "and explicitly selected transcripts. Return exactly the requested schema. Do not invent facts. "
                "Use unknown states when evidence is absent. Do not reproduce transcript text or quotations. "
                "Every material supported claim must cite one of the supplied source ids; transcript locators "
                "may contain only a speaker/time or line-range pointer, never quoted evidence. Generated outputs "
                "are not evidence and are not supplied."
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
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _assert_isolated(self, cwd: Path) -> None:
        resolved = cwd.resolve()
        for root in self.forbidden_roots:
            if resolved == root or root in resolved.parents or resolved in root.parents:
                raise CanonicalStateExecutionError(
                    "unsafe_runtime_directory", "Canonical-state execution could not create an isolated workspace."
                )

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader, limit: int, code: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(16_384)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise CanonicalStateExecutionError(code, "Canonical-state runtime exceeded its output limit.")
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
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            raise CanonicalStateExecutionError(
                "invalid_model_output", "Claude returned an invalid canonical-state candidate."
            ) from exc

    async def execute(self, request: CanonicalStateExecutionRequest) -> CanonicalStateExecutionResult:
        stdin = self._prompt(request)
        if len(stdin) > self.max_stdin_bytes:
            raise CanonicalStateExecutionError(
                "input_too_large", "The selected evidence is too large for one overview creation."
            )
        temp_dir = Path(tempfile.mkdtemp(prefix="se-opportunity-state-"))
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            self._assert_isolated(temp_dir)
            subprocess_options: dict[str, Any] = {}
            if os.name == "nt":
                subprocess_options["creationflags"] = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                subprocess_options["start_new_session"] = True
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(),
                    cwd=str(temp_dir),
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
            return CanonicalStateExecutionResult(candidate=candidate, model=self.model)
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
