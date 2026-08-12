"""Executable feasibility harness for a typed-tool agent loop.

This module implements a manual multi-step Anthropic Messages API runtime that
uses only explicit, typed tools. It is designed to run inside a sandbox with no
real Anthropic API key: all model requests are sent to a worker-side proxy that
adds the platform credential. The harness is deterministic when paired with a
mock HTTP transport, so it can be exercised without network or model access.

Vendor references (2026-08-11):
- Anthropic Messages API: https://docs.anthropic.com/en/api/messages
- httpx 0.28.1 (transport used to reach the worker model proxy)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from webapp.hosted.runtime_contract import (
    CancellationToken,
    ExecutionMetadata,
    NetworkDestination,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    SandboxOutputSidecar,
    SkillRuntime,
    TokenUsage,
)


class TextBlock(BaseModel):
    """A text content block in the Anthropic Messages API."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """A tool_use content block in the Anthropic Messages API."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """A tool_result content block in the Anthropic Messages API."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str


MessageContent = TextBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    """One turn in the Anthropic Messages API conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant"]
    content: list[MessageContent]


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

    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: int = 0
    output_tokens: int = 0


class MessageResponse(BaseModel):
    """Response payload from the Anthropic Messages API."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[MessageContent]
    model: str
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)


def _tool_definitions(allowed: frozenset[str]) -> list[ToolDefinition]:
    """Return tool definitions only for tools in the job allowlist."""
    definitions: dict[str, ToolDefinition] = {
        "read_transcript": ToolDefinition(
            name="read_transcript",
            description="Read the full transcript. Already loaded into context; returns confirmation.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        "read_prior_context": ToolDefinition(
            name="read_prior_context",
            description="Read an approved prior-context file by reference id.",
            input_schema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        ),
        "list_priors": ToolDefinition(
            name="list_priors",
            description="List the reference ids of available prior-context files.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        "search_transcript": ToolDefinition(
            name="search_transcript",
            description="Search the transcript for a keyword and return matching line ranges.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
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
                "required": ["category", "message"],
            },
        ),
    }
    return [definitions[name] for name in allowed if name in definitions]


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
        """Locate the single transcript file in the read-only input workspace."""
        input_dir = Path(job.input_workspace)
        if not input_dir.exists():
            return None
        candidates = [p for p in input_dir.iterdir() if p.is_file() and p.suffix in {".txt", ".md", ".vtt", ".srt"}]
        # The first text-like file is the transcript; all others are prior context.
        return candidates[0] if candidates else None

    def _prior_paths(self, job: RuntimeJob) -> list[Path]:
        """Locate prior-context files in the read-only input workspace."""
        input_dir = Path(job.input_workspace)
        if not input_dir.exists():
            return []
        transcript = self._transcript_path(job)
        return [p for p in input_dir.iterdir() if p.is_file() and p != transcript]

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
            return _failure("cancelled", "Cancelled before execution started")

        if not job.requested_model:
            return _failure("input_error", "requested_model must be specified by the worker")

        transcript_path = self._transcript_path(job)
        if transcript_path is None:
            return _failure("input_error", "No transcript found in input workspace")

        transcript_text = transcript_path.read_text(encoding="utf-8")
        prior_texts = [p.read_text(encoding="utf-8") for p in self._prior_paths(job)]

        output_dir = Path(job.output_workspace)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            proxy_url = _proxy_base_url(job.allowlist.network)
        except (RuntimeError, ValueError) as exc:
            return _failure("configuration_error", str(exc))

        client = self.model_client or _default_client(proxy_url)
        if str(client.base_url) != proxy_url:
            return _failure(
                "configuration_error",
                f"Model client base_url {client.base_url!r} does not match allowlisted proxy {proxy_url!r}",
            )

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
        model_name: str | None = job.requested_model
        failure: RedactedFailure | None = None

        for _turn in range(self.max_turns):
            if cancellation.is_cancelled():
                return _failure("cancelled", "Cancelled during agent loop")
            if datetime.now(timezone.utc) > job.execution_deadline:
                return _failure("timeout", "Execution deadline expired")

            request = MessageRequest(
                model=job.requested_model,
                system=self._system_prompt(job),
                messages=messages,
                tools=tools,
            )
            response = await _cancellable_await(_call_proxy(client, request), cancellation)
            if isinstance(response, RuntimeResult):
                return response
            model_name = response.model
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            assistant_content: list[MessageContent] = []
            tool_use_blocks: list[ToolUseBlock] = []
            for block in response.content:
                assistant_content.append(block)
                if isinstance(block, ToolUseBlock):
                    tool_use_blocks.append(block)

            messages.append(Message(role="assistant", content=assistant_content))

            if not tool_use_blocks:
                break

            results: list[MessageContent] = []
            for tool in tool_use_blocks:
                result = _run_tool(tool, job, output_dir)
                if isinstance(result, RedactedFailure):
                    failure = result
                results.append(
                    ToolResultBlock(tool_use_id=tool.id, content=result if isinstance(result, str) else "failure")
                )
            messages.append(Message(role="user", content=results))

            if failure:
                return _failure(failure.category, failure.message)

            if _stop_reason(response, tool_use_blocks):
                break

        return await _collect_result(output_dir, job, model_name, total_input, total_output)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _failure(category: str, message: str) -> RuntimeResult:
    return RuntimeResult(failure=RedactedFailure(category=category, message=message))


def _default_client(base_url: str) -> httpx.AsyncClient:
    """Build a client that targets the single allowlisted model proxy destination."""
    return httpx.AsyncClient(
        base_url=base_url,
        # No Anthropic API key is attached here; the worker proxy adds it.
        headers={"Content-Type": "application/json"},
        timeout=httpx.Timeout(60.0),
    )


async def _cancellable_await(coro, cancellation: CancellationToken) -> Any:
    """Race a coroutine against cancellation so a blocked request can be interrupted."""
    request_task = asyncio.create_task(coro)
    cancel_task = asyncio.create_task(cancellation.wait())
    done, pending = await asyncio.wait({request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await p
    if cancel_task in done:
        request_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request_task
        return _failure("cancelled", "Model request cancelled")
    return request_task.result()


async def _call_proxy(client: httpx.AsyncClient, request: MessageRequest) -> MessageResponse:
    """POST a model turn to the worker proxy and parse the response."""
    response = await client.post("/v1/messages", json=request.model_dump())
    response.raise_for_status()
    return MessageResponse(**response.json())


def _run_tool(tool: ToolUseBlock, job: RuntimeJob, output_dir: Path) -> str | RedactedFailure:
    """Execute one typed tool in the sandbox. Disabled tools and unknown tools fail."""
    if tool.name not in job.allowlist.tools:
        return RedactedFailure(category="forbidden_tool", message=f"Tool '{tool.name}' is not in the job allowlist")

    if tool.name == "read_transcript":
        return "Transcript loaded from context"

    if tool.name == "write_output":
        markdown = tool.input.get("markdown", "")
        sidecar = tool.input.get("sidecar", {})
        (output_dir / "output.md").write_text(markdown, encoding="utf-8")
        (output_dir / "sidecar.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        return "Output written"

    if tool.name == "finish":
        return "Finished"

    if tool.name == "report_failure":
        return RedactedFailure(
            category=tool.input.get("category", "runtime_error"),
            message=tool.input.get("message", "Unknown failure"),
        )

    return RedactedFailure(category="unknown_tool", message=f"Unhandled tool '{tool.name}'")


def _stop_reason(response: MessageResponse, tool_use_blocks: list[ToolUseBlock]) -> bool:
    """Return True when the assistant signalled completion."""
    if response.stop_reason in {"end_turn", "stop_sequence"}:
        return True
    for block in tool_use_blocks:
        if block.name == "finish":
            return True
    return False


async def _collect_result(
    output_dir: Path,
    job: RuntimeJob,
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
) -> RuntimeResult:
    """Read the candidate output from the sandbox workspace and return a RuntimeResult.

    The sidecar is not fabricated: a missing or malformed sidecar fails the attempt.
    """
    output_md = output_dir / "output.md"
    sidecar_path = output_dir / "sidecar.json"
    if not output_md.exists():
        return _failure("output_error", "No output.md produced by the sandbox runtime")

    if not sidecar_path.exists():
        return _failure("output_error", "No sidecar.json produced by the sandbox runtime")

    try:
        sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar = SandboxOutputSidecar(**sidecar_data)
    except (OSError, ValueError) as exc:
        return _failure("output_error", f"Malformed sidecar.json: {exc.__class__.__name__}")

    if sidecar.skill != job.skill:
        return _failure("output_error", f"Sidecar skill {sidecar.skill!r} does not match job skill {job.skill!r}")
    if sidecar.mode != job.mode:
        return _failure("output_error", f"Sidecar mode {sidecar.mode!r} does not match job mode {job.mode!r}")

    markdown = output_md.read_text(encoding="utf-8")
    return RuntimeResult(
        output_artifact=markdown,
        sidecar=sidecar,
        execution_metadata=ExecutionMetadata(
            model=model_name,
            token_usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
    )


# Register as satisfying the SkillRuntime protocol at runtime.
assert isinstance(TypedToolRuntime(), SkillRuntime)
