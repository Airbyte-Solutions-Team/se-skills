"""Executable feasibility harness for a typed-tool agent loop.

This module implements a manual multi-step Anthropic Messages API runtime that
uses only explicit, typed tools. It is designed to run inside a sandbox with no
real Anthropic API key: all model requests are sent to a worker-side proxy that
adds the platform credential. The harness is deterministic when paired with a
mock HTTP transport, so it can be exercised without network or model access.

Vendor references (2026-08-11):
- Anthropic Messages API: https://platform.claude.com/docs/en/api/messages
- httpx 0.28.1 (transport used to reach the worker model proxy)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from webapp.hosted.runtime_contract import (
    CancellationToken,
    ExecutionMetadata,
    FailureCategory,
    NetworkDestination,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    SandboxOutputSidecar,
    SkillRuntime,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Anthropic Messages API DTOs
# ---------------------------------------------------------------------------

class BaseContentBlock(BaseModel):
    """Fallback content block; accepts any Anthropic block type."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


class TextBlock(BaseModel):
    """A text content block in the Anthropic Messages API."""

    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """A tool_use content block in the Anthropic Messages API."""

    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """A tool_result content block in the Anthropic Messages API."""

    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | BaseContentBlock


class Message(BaseModel):
    """One turn in the Anthropic Messages API conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant"]
    content: list[ContentBlock]


class ToolDefinition(BaseModel):
    """Anthropic tool definition with JSON schema input."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MessageRequest(BaseModel):
    """Request payload for the Anthropic Messages API."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str
    max_tokens: int = 4096
    system: str
    messages: list[Message]
    tools: list[ToolDefinition] = Field(default_factory=list)


class Usage(BaseModel):
    """Token usage reported by the Anthropic Messages API."""

    model_config = ConfigDict(extra="allow", frozen=True)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class MessageResponse(BaseModel):
    """Response payload from the Anthropic Messages API.

    `extra="allow"` is used for the response DTO so a worker proxy that passes
    through upstream fields (citations, thinking blocks, etc.) does not break the
    harness. The harness only consumes documented fields.
    """

    model_config = ConfigDict(extra="allow", frozen=True)
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock]
    model: str
    stop_reason: str | None = None
    stop_sequence: str | None = None
    stop_details: Any | None = None
    usage: Usage = Field(default_factory=Usage)


# ---------------------------------------------------------------------------
# Typed tool inputs
# ---------------------------------------------------------------------------

class EmptyInput(BaseModel):
    """No arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadPriorContextInput(BaseModel):
    """Arguments for read_prior_context."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str


class SearchTranscriptInput(BaseModel):
    """Arguments for search_transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str
    max_results: int = 10

    @field_validator("max_results")  # type: ignore[misc]
    @classmethod
    def _clamp_max_results(cls, value: int) -> int:
        if value < 1:
            return 1
        if value > 100:
            return 100
        return value


class WriteOutputInput(BaseModel):
    """Arguments for write_output."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    markdown: str
    sidecar: dict[str, Any]


class ReportFailureInput(BaseModel):
    """Arguments for report_failure.

    The model may only report a closed failure category. The persisted message is
    generic; any model-supplied detail is not written to the durable job payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    category: FailureCategory
    message: str = "Unknown failure"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _tool_definitions(allowed: frozenset[str]) -> list[ToolDefinition]:
    """Return tool definitions only for tools in the job allowlist, sorted by name."""
    definitions: dict[str, ToolDefinition] = {
        "read_transcript": ToolDefinition(
            name="read_transcript",
            description="Read the full transcript. Returns the transcript text.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        "read_prior_context": ToolDefinition(
            name="read_prior_context",
            description="Read an approved prior-context file by its manifest reference id.",
            input_schema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        ),
        "list_priors": ToolDefinition(
            name="list_priors",
            description="List the manifest reference ids of available prior-context files.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        "search_transcript": ToolDefinition(
            name="search_transcript",
            description="Search the transcript for a keyword and return matching line ranges.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        "write_output": ToolDefinition(
            name="write_output",
            description="Write the final Markdown report and sidecar to the output workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "markdown": {"type": "string", "description": "Complete Markdown output"},
                    "sidecar": {"type": "object", "description": "JSON sidecar metadata"},
                },
                "required": ["markdown", "sidecar"],
            },
        ),
        "finish": ToolDefinition(
            name="finish",
            description="Signal that the report is complete.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        "report_failure": ToolDefinition(
            name="report_failure",
            description="Report a non-recoverable failure with a redacted reason.",
            input_schema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["category"],
            },
        ),
    }
    return sorted(
        (definitions[name] for name in allowed if name in definitions),
        key=lambda d: d.name,
    )


def _proxy_base_url(network: frozenset[NetworkDestination]) -> str:
    """Return the single allowed model proxy destination as a base URL.

    The worker is responsible for providing exactly one network destination:
    the worker model proxy. Direct external destinations must not be reachable.
    gVisor/network-namespace enforcement is a Slice 5B integration acceptance
    gate and is not asserted by this unit-testable harness.
    """
    if len(network) != 1:
        raise RuntimeError(
            f"Sandbox allowlist must contain exactly one network destination (the worker model proxy); got {len(network)}"
        )
    return str(next(iter(network)))


class TypedToolRuntime:
    """Manual multi-step agent loop with explicit typed tools.

    The runtime does not hold the Anthropic API key. It sends model turn requests
    to the worker model proxy declared in `job.allowlist.network`. The proxy is
    responsible for attaching the platform API key and forwarding the request.
    """

    def __init__(self, model_client: httpx.AsyncClient | None = None, max_turns: int = 10) -> None:
        self.model_client = model_client
        self.max_turns = max_turns

    def _transcript_path(self, job: RuntimeJob) -> Path | None:
        """Return the manifest-authorized transcript path inside the input workspace."""
        input_dir = Path(job.input_workspace)
        if not input_dir.exists():
            return None
        ref = job.input_manifest.transcript_ref
        candidate = input_dir / ref
        try:
            candidate.relative_to(input_dir)
        except ValueError:
            return None
        if candidate.is_file():
            return candidate
        return None

    def _prior_paths(self, job: RuntimeJob) -> list[Path] | None:
        """Return the manifest-authorized prior-context files, or None if any are missing or unsafe."""
        input_dir = Path(job.input_workspace)
        if not input_dir.exists():
            return None
        paths: list[Path] = []
        for ref in sorted(job.input_manifest.prior_context_refs):
            candidate = input_dir / ref
            try:
                candidate.relative_to(input_dir)
            except ValueError:
                return None
            if candidate.is_file():
                paths.append(candidate)
            else:
                return None
        return paths

    def _system_prompt(self, job: RuntimeJob) -> str:
        return (
            f"You are an SE assistant executing the '{job.skill}' skill version {job.skill_version}. "
            "Read the provided transcript and prior context, then produce a Markdown report. "
            "Use only the provided tools. Do not emit shell commands, browser automation, "
            "git operations, or arbitrary HTTP requests."
        )

    def _user_prompt(self, job: RuntimeJob, transcript_text: str, prior_texts: list[str]) -> str:
        parts = ["Transcript:", transcript_text]
        if prior_texts:
            parts.extend(["\nPrior context:", *prior_texts])
        parts.append(
            f"\nProduce a '{job.mode}' post-call output and then call the finish tool. "
            "Use write_output to write the Markdown and sidecar first if needed."
        )
        return "\n".join(parts)

    async def execute(self, job: RuntimeJob, cancellation: CancellationToken) -> RuntimeResult:
        """Run the manual typed-tool loop until the output is complete or the job expires."""
        if cancellation.is_cancelled():
            return _failure("cancelled")

        transcript_path = self._transcript_path(job)
        if transcript_path is None:
            return _failure("input_error")

        if job.input_manifest.transcript_ref in job.input_manifest.prior_context_refs:
            return _failure("input_error")

        prior_paths = self._prior_paths(job)
        if prior_paths is None:
            return _failure("input_error")
        transcript_text = transcript_path.read_text(encoding="utf-8")
        prior_texts = [p.read_text(encoding="utf-8") for p in prior_paths]

        output_dir = Path(job.output_workspace)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            proxy_url = _proxy_base_url(job.allowlist.network)
        except (RuntimeError, ValueError):
            return _failure("configuration_error")

        client = self.model_client or _default_client(proxy_url)
        if str(client.base_url) != proxy_url:
            return _failure("configuration_error")

        allowed_tools = job.allowlist.tools
        tools = _tool_definitions(allowed_tools)
        messages: list[Message] = [
            Message(
                role="user",
                content=[TextBlock(text=self._user_prompt(job, transcript_text, prior_texts))],
            ),
        ]

        total_input = 0
        total_output = 0
        total_cache_creation = 0
        total_cache_read = 0
        model_name: str = job.requested_model
        output_written = False

        for _turn in range(self.max_turns):
            if cancellation.is_cancelled():
                return _failure("cancelled")

            now = datetime.now(timezone.utc)
            if now >= job.execution_deadline:
                return _failure("timeout")

            remaining = (job.execution_deadline - now).total_seconds()

            request = MessageRequest(
                model=job.requested_model,
                system=self._system_prompt(job),
                messages=messages,
                tools=tools,
            )
            try:
                response = await _cancellable_await(
                    _call_proxy(client, request),
                    cancellation,
                    timeout=remaining,
                )
            except Exception:
                return _failure("model_error")

            if isinstance(response, RuntimeResult):
                return response

            model_name = response.model
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens
            if response.usage.cache_creation_input_tokens:
                total_cache_creation += response.usage.cache_creation_input_tokens
            if response.usage.cache_read_input_tokens:
                total_cache_read += response.usage.cache_read_input_tokens

            tool_use_blocks = [block for block in response.content if isinstance(block, ToolUseBlock)]

            # Fail-closed on terminal stop reasons before dispatching any tool calls.
            if response.stop_reason in {"max_tokens", "refusal", "pause_turn"}:
                return _failure("model_error")
            if response.stop_reason in {"end_turn", "stop_sequence"}:
                if tool_use_blocks:
                    return _failure("model_error")
                break
            if not tool_use_blocks:
                if response.stop_reason == "tool_use":
                    return _failure("model_error")
                break

            messages.append(Message(role="assistant", content=list(response.content)))

            results: list[ContentBlock] = []
            finished = False
            for tool in tool_use_blocks:
                if tool.name not in allowed_tools:
                    return _failure("forbidden_tool")

                result = _run_tool(tool, job, output_dir, transcript_text)
                if isinstance(result, RedactedFailure):
                    return _failure(result.category, result.message)

                results.append(ToolResultBlock(tool_use_id=tool.id, content=result))

                if tool.name == "write_output":
                    output_written = True
                if tool.name == "finish":
                    finished = True

            messages.append(Message(role="user", content=results))

            if finished:
                break

        if not output_written:
            return _failure("output_error")

        return await _collect_result(
            output_dir,
            job,
            model_name,
            total_input,
            total_output,
            total_cache_creation,
            total_cache_read,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAILURE_MESSAGES: dict[FailureCategory, str] = {
    "cancelled": "Execution was cancelled",
    "configuration_error": "Runtime configuration error",
    "forbidden_tool": "Disallowed tool requested",
    "input_error": "Invalid manifest or input file",
    "model_error": "Model request or response error",
    "output_error": "Sandbox produced invalid output",
    "runtime_error": "Runtime error",
    "timeout": "Execution timed out",
    "tool_input_error": "Tool received invalid arguments",
    "unknown_tool": "Unknown tool requested",
}


def _failure(category: FailureCategory, message: str | None = None) -> RuntimeResult:
    """Return a redacted failure. If no message is supplied a fixed generic one is used."""
    return RuntimeResult(failure=RedactedFailure(category=category, message=message or FAILURE_MESSAGES[category]))


def _default_client(base_url: str) -> httpx.AsyncClient:
    """Build a client that targets the single allowlisted model proxy destination."""
    return httpx.AsyncClient(
        base_url=base_url,
        # No Anthropic API key is attached here; the worker proxy adds it.
        headers={"Content-Type": "application/json"},
        timeout=httpx.Timeout(60.0),
    )


async def _cancellable_await(
    coro,
    cancellation: CancellationToken,
    timeout: float | None = None,
) -> Any:
    """Race a coroutine against cancellation and an optional execution deadline."""
    request_task = asyncio.create_task(coro)
    cancel_task = asyncio.create_task(cancellation.wait())
    tasks: set[asyncio.Task] = {request_task, cancel_task}
    deadline_task: asyncio.Task | None = None

    if timeout is not None and timeout > 0:
        deadline_task = asyncio.create_task(asyncio.sleep(timeout))
        tasks.add(deadline_task)

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for p in pending:
        p.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await p

    if deadline_task is not None and deadline_task in done:
        request_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request_task
        return _failure("timeout")

    if cancel_task in done:
        request_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request_task
        return _failure("cancelled")

    return request_task.result()


async def _call_proxy(client: httpx.AsyncClient, request: MessageRequest) -> MessageResponse:
    """POST a model turn to the worker proxy and parse the response.

    `exclude_none=True` keeps unknown/redacted/thinking fallback blocks from
    being reserialized with invented null fields that Anthropic rejects.
    """
    response = await client.post("/v1/messages", json=request.model_dump(exclude_none=True))
    response.raise_for_status()
    return MessageResponse(**response.json())


def _run_tool(
    tool: ToolUseBlock,
    job: RuntimeJob,
    output_dir: Path,
    transcript_text: str,
) -> str | RedactedFailure:
    """Execute one typed tool in the sandbox.

    Tool inputs are validated with strict Pydantic models. Failure diagnostics
    returned to the job ledger are generic and do not echo model-controlled values
    such as tool names, references, or sidecar fields.
    """

    def _parse(cls, data: dict[str, Any]) -> Any:
        try:
            return cls.model_validate(data)
        except ValidationError:
            return RedactedFailure(category="tool_input_error", message=FAILURE_MESSAGES["tool_input_error"])

    input_dir = Path(job.input_workspace)

    if tool.name == "read_transcript":
        parsed = _parse(EmptyInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        return transcript_text[:500_000]

    if tool.name == "read_prior_context":
        parsed = _parse(ReadPriorContextInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        ref = parsed.ref
        if ref not in job.input_manifest.prior_context_refs:
            return RedactedFailure(category="input_error", message=FAILURE_MESSAGES["input_error"])
        prior_path = input_dir / ref
        try:
            prior_path.relative_to(input_dir)
        except ValueError:
            return RedactedFailure(category="input_error", message=FAILURE_MESSAGES["input_error"])
        if not prior_path.is_file():
            return RedactedFailure(category="input_error", message=FAILURE_MESSAGES["input_error"])
        return prior_path.read_text(encoding="utf-8")[:100_000]

    if tool.name == "list_priors":
        parsed = _parse(EmptyInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        return json.dumps([{"ref": ref, "index": i} for i, ref in enumerate(sorted(job.input_manifest.prior_context_refs))])

    if tool.name == "search_transcript":
        parsed = _parse(SearchTranscriptInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        query = parsed.query.lower()
        matches: list[str] = []
        for i, line in enumerate(transcript_text.splitlines(), start=1):
            if query in line.lower():
                matches.append(f"Line {i}: {line.strip()[:200]}")
            if len(matches) >= parsed.max_results:
                break
        return "\n".join(matches) if matches else "No matches"

    if tool.name == "write_output":
        parsed = _parse(WriteOutputInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        try:
            sidecar = SandboxOutputSidecar(**parsed.sidecar)
        except ValidationError:
            return RedactedFailure(category="output_error", message=FAILURE_MESSAGES["output_error"])
        (output_dir / "output.md").write_text(parsed.markdown, encoding="utf-8")
        (output_dir / "sidecar.json").write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")
        return "Output written"

    if tool.name == "finish":
        parsed = _parse(EmptyInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        return "Finished"

    if tool.name == "report_failure":
        parsed = _parse(ReportFailureInput, tool.input)
        if isinstance(parsed, RedactedFailure):
            return parsed
        # The model-supplied message is treated as untrusted and is not propagated
        # verbatim to the job ledger. The category is closed and the message is
        # fixed and generic.
        return RedactedFailure(category=parsed.category, message="Model reported a failure")

    return RedactedFailure(category="unknown_tool", message=FAILURE_MESSAGES["unknown_tool"])


async def _collect_result(
    output_dir: Path,
    job: RuntimeJob,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> RuntimeResult:
    """Read the candidate output from the sandbox workspace and return a RuntimeResult.

    The sidecar is not fabricated: a missing or malformed sidecar fails the attempt.
    Failure diagnostics are generic and do not echo model-controlled sidecar fields.
    """
    output_md = output_dir / "output.md"
    sidecar_path = output_dir / "sidecar.json"

    if not output_md.exists():
        return _failure("output_error")

    if not sidecar_path.exists():
        return _failure("output_error")

    try:
        sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar = SandboxOutputSidecar(**sidecar_data)
    except (OSError, ValueError):
        return _failure("output_error")

    if sidecar.skill != job.skill:
        return _failure("output_error")
    if sidecar.skill_version != job.skill_version:
        return _failure("output_error")
    if sidecar.mode != job.mode:
        return _failure("output_error")

    markdown = output_md.read_text(encoding="utf-8")
    return RuntimeResult(
        output_artifact=markdown,
        sidecar=sidecar,
        execution_metadata=ExecutionMetadata(
            runtime_version=job.requested_runtime_version,
            model=model_name,
            token_usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens or None,
                cache_read_input_tokens=cache_read_input_tokens or None,
            ),
            cost=None,
        ),
    )


# Register as satisfying the SkillRuntime protocol at runtime.
assert isinstance(TypedToolRuntime(), SkillRuntime)
