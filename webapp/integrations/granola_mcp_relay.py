"""User-triggered Granola retrieval through the signed-in user's Claude Code MCP connection.

The pilot has exactly one authorized Granola route: the per-user browser OAuth
grant that the user already completed for the ``granola`` MCP server inside
Claude Code (`docs/COMMAND_CENTER.md` §9, capability check 2026-09-24). That
grant is held by Claude Code's own credential store; this app never reads,
copies, or stores it. To reach Granola the app runs the user's own ``claude``
CLI in print mode, restricted to the one Granola MCP tool the step needs, and
extracts the **tool result verbatim** from the ``stream-json`` event stream.
The model's prose is discarded, so meeting content never passes through model
output; the model is used only as the OAuth-holding MCP client.

Boundary, stated plainly:

* Every call is one bounded subprocess started by an explicit user action.
  There is no scheduler, no polling loop, no webhook and no cached listing.
* Only ``mcp__granola__get_account_info``, ``list_meetings``, ``get_meetings``
  and ``get_meeting_transcript`` are allowed; built-in tools are disabled.
* Nothing from the subprocess (stdout, stderr, tool text) is logged or placed
  in job metadata or HTTP error bodies. Failures surface as short codes.
* Whether ``claude -p`` reuses the interactive session's OAuth grant is a
  documented behaviour that has **not** been verified by the app's authors;
  the connection check exists so the user can establish it locally.

``FakeGranolaRetrievalTransport`` scripts responses for tests.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

GranolaTool = Literal["get_account_info", "list_meetings", "get_meetings", "get_meeting_transcript"]
ALLOWED_TOOLS: tuple[GranolaTool, ...] = (
    "get_account_info", "list_meetings", "get_meetings", "get_meeting_transcript"
)
MAX_MEETINGS_PER_GET = 10  # `get_meetings` accepts 1–10 ids (observed schema).
MAX_RESULT_BYTES = 4_000_000

RelayErrorCode = Literal[
    "runtime_unavailable",
    "relay_timeout",
    "relay_output_too_large",
    "relay_exit_error",
    "relay_no_tool_result",
    "relay_unparseable_result",
    "relay_wrong_tool",
    "tool_error",
    "tool_not_found",
    "tool_access_denied",
    "tool_auth_required",
]


class GranolaRelayError(Exception):
    """A retrieval step failed. `code` is safe to persist and display; nothing else is."""

    def __init__(self, code: RelayErrorCode, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class GranolaRetrievalTransport(Protocol):
    """One MCP tool call per invocation; returns the parsed tool result."""

    def describe(self) -> dict[str, Any]: ...

    async def call(self, tool: GranolaTool, arguments: Mapping[str, Any]) -> Any: ...


def classify_tool_error(text: str) -> RelayErrorCode:
    """Map an MCP error message to a safe code without keeping the message."""
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered or "authenticat" in lowered or "log in" in lowered or "login" in lowered:
        return "tool_auth_required"
    if "403" in lowered or "forbidden" in lowered or "permission" in lowered or "access" in lowered:
        return "tool_access_denied"
    if "404" in lowered or "not found" in lowered or "does not exist" in lowered or "no meeting" in lowered:
        return "tool_not_found"
    return "tool_error"


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        return "".join(parts)
    return ""


def extract_tool_result(stream: bytes, *, tool: GranolaTool) -> Any:
    """Return the parsed result of the single allowed tool call in a `stream-json` transcript.

    Accepts the event shapes Claude Code emits with ``--output-format stream-json
    --verbose``: ``assistant`` messages carrying ``tool_use`` blocks and ``user``
    messages carrying ``tool_result`` blocks that reference them. Any text the
    model wrote is ignored. Raises with a safe code when the tool was not
    called, another tool was called, the result is an error, or the result is
    not JSON.
    """
    expected = f"mcp__granola__{tool}"
    uses: dict[str, str] = {}
    results: list[tuple[str, Any, bool]] = []
    for raw in stream.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                uses[block["id"]] = str(block.get("name", ""))
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                results.append((block["tool_use_id"], block.get("content"), bool(block.get("is_error"))))
    for name in uses.values():
        if name != expected:
            raise GranolaRelayError("relay_wrong_tool", retryable=False)
    matched = [(content, is_error) for use_id, content, is_error in results if uses.get(use_id) == expected]
    if not matched:
        raise GranolaRelayError("relay_no_tool_result", retryable=True)
    content, is_error = matched[0]
    text = _result_text(content)
    if is_error:
        raise GranolaRelayError(classify_tool_error(text), retryable=False)
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise GranolaRelayError("relay_unparseable_result", retryable=True) from exc
    if isinstance(parsed, dict) and parsed.get("error") and len(parsed) <= 3:
        raise GranolaRelayError(classify_tool_error(json.dumps(parsed)), retryable=False)
    return parsed


class ClaudeCodeMcpRelay:
    """Run the user's Claude Code once per tool call as a restricted MCP client."""

    def __init__(
        self,
        *,
        model: str,
        forbidden_roots: list[Path],
        executable: str = "claude",
        mcp_server: str = "granola",
        timeout_seconds: float = 180.0,
        max_stderr_bytes: int = 65_536,
    ) -> None:
        self.model = model
        self.executable = executable
        self.mcp_server = mcp_server
        self.timeout_seconds = timeout_seconds
        self.max_stderr_bytes = max_stderr_bytes
        self.forbidden_roots = [Path(root).resolve() for root in forbidden_roots]

    def describe(self) -> dict[str, Any]:
        return {
            "transport": "claude_code_mcp_relay",
            "mode": "user_triggered_retrieval",
            "unattended_discovery": False,
            "requires_credentials": True,
            "credential_holder": "claude_code_user_oauth",
            "mcp_server": self.mcp_server,
            "allowed_tools": [f"mcp__{self.mcp_server}__{tool}" for tool in ALLOWED_TOOLS],
            "label": "Manual Granola check through your Claude Code MCP connection",
        }

    def command(self, tool: GranolaTool, executable: str | None = None) -> list[str]:
        """The restricted CLI contract: no built-in tools, exactly one MCP tool allowed, no persistence."""
        if tool not in ALLOWED_TOOLS:
            raise GranolaRelayError("relay_wrong_tool", retryable=False)
        return [
            executable or shutil.which(self.executable) or self.executable,
            "-p",
            "--max-turns",
            "2",
            "--tools",
            "",
            "--allowedTools",
            f"mcp__{self.mcp_server}__{tool}",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
        ]

    def prompt(self, tool: GranolaTool, arguments: Mapping[str, Any]) -> str:
        return (
            f"Call the tool mcp__{self.mcp_server}__{tool} exactly once with exactly these JSON arguments: "
            f"{json.dumps(dict(arguments), separators=(',', ':'))}. "
            "Do not call any other tool. After the tool returns, reply with the single word: done. "
            "Do not summarize, quote, or describe the tool result."
        )

    def _assert_isolated(self, cwd: Path) -> None:
        resolved = cwd.resolve()
        for root in self.forbidden_roots:
            if resolved == root or root in resolved.parents or resolved in root.parents:
                raise GranolaRelayError("runtime_unavailable", retryable=False)

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(16_384)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise GranolaRelayError("relay_output_too_large", retryable=False)
            chunks.append(chunk)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (ProcessLookupError, TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def call(self, tool: GranolaTool, arguments: Mapping[str, Any]) -> Any:
        command = self.command(tool)
        temp_dir = Path(tempfile.mkdtemp(prefix="se-granola-relay-"))
        proc: asyncio.subprocess.Process | None = None
        try:
            self._assert_isolated(temp_dir)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(temp_dir),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except (FileNotFoundError, PermissionError) as exc:
                raise GranolaRelayError("runtime_unavailable", retryable=False) from exc
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            proc.stdin.write(self.prompt(tool, arguments).encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            stdout_task = asyncio.create_task(self._read_bounded(proc.stdout, MAX_RESULT_BYTES))
            stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, self.max_stderr_bytes))
            try:
                return_code, stdout, _stderr = await asyncio.wait_for(
                    asyncio.gather(proc.wait(), stdout_task, stderr_task), timeout=self.timeout_seconds
                )
            except TimeoutError as exc:
                stdout_task.cancel()
                stderr_task.cancel()
                raise GranolaRelayError("relay_timeout", retryable=True) from exc
            if return_code != 0 and not stdout.strip():
                raise GranolaRelayError("relay_exit_error", retryable=True)
            return extract_tool_result(stdout, tool=tool)
        finally:
            if proc is not None:
                await self._terminate(proc)
            shutil.rmtree(temp_dir, ignore_errors=True)


class FakeGranolaRetrievalTransport:
    """Scripted transport for tests: `responses[(tool, key)]` or callables; records calls."""

    def __init__(self, *, mcp_server: str = "granola") -> None:
        self.mcp_server = mcp_server
        self.account: Any = {"workspace": {"id": "ws-synthetic", "display_name": "Synthetic Workspace"}, "note_access_scope": ["personal"]}
        self.listings: dict[str, Any] = {}
        self.meetings: dict[str, Any] = {}
        self.transcripts: dict[str, Any] = {}
        self.failures: dict[tuple[str, str], GranolaRelayError | list[GranolaRelayError]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe(self) -> dict[str, Any]:
        return {
            "transport": "fake_mcp_relay",
            "mode": "user_triggered_retrieval",
            "unattended_discovery": False,
            "requires_credentials": False,
            "credential_holder": "none",
            "mcp_server": self.mcp_server,
            "allowed_tools": [f"mcp__{self.mcp_server}__{tool}" for tool in ALLOWED_TOOLS],
            "label": "Fake Granola relay (tests only)",
        }

    def fail(self, tool: str, key: str, *errors: GranolaRelayError) -> None:
        self.failures[(tool, key)] = list(errors)

    def _maybe_fail(self, tool: str, key: str) -> None:
        planned = self.failures.get((tool, key))
        if not planned:
            return
        if isinstance(planned, list):
            if not planned:
                return
            raise planned.pop(0)
        raise planned

    async def call(self, tool: GranolaTool, arguments: Mapping[str, Any]) -> Any:
        args = dict(arguments)
        self.calls.append((tool, args))
        if tool == "get_account_info":
            self._maybe_fail(tool, "*")
            return self.account
        if tool == "list_meetings":
            key = args.get("time_range", "*")
            self._maybe_fail(tool, key)
            return self.listings.get(key, self.listings.get("*", {"count": 0, "meetings": []}))
        if tool == "get_meetings":
            found = []
            for meeting_id in args.get("meeting_ids", []):
                self._maybe_fail(tool, meeting_id)
                if meeting_id in self.meetings:
                    found.append(self.meetings[meeting_id])
            return {"meetings": found}
        if tool == "get_meeting_transcript":
            meeting_id = args.get("meeting_id", "")
            self._maybe_fail(tool, meeting_id)
            if meeting_id not in self.transcripts:
                raise GranolaRelayError("tool_not_found", retryable=False)
            return self.transcripts[meeting_id]
        raise GranolaRelayError("relay_wrong_tool", retryable=False)
