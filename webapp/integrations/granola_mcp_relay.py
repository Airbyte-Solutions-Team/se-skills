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
* **Pre-execution restriction** (Claude Code CLI reference, checked 2026-09-25):

  - ``--strict-mcp-config --mcp-config <tmp>/mcp.json`` loads *only* the
    ``granola`` server. The file is generated per call from the user's own
    definition in ``~/.claude.json`` — local scope (``claude mcp add``'s
    default, keyed by the project path the user ran it from) for an explicitly
    trusted project path, else user scope; ``type`` and ``url`` only, never
    ``headers``. Claude Code stores MCP OAuth sign-ins per endpoint, so
    the same name + URL reuses the grant without this app touching it. No
    definition → fail closed (``mcp_server_not_configured``).
  - ``--tools ""`` removes every built-in tool (the flag does not affect MCP
    tools, which is why the two lines above and below exist).
  - ``--disallowedTools`` names every *other* Granola tool observed in the
    capability check; a bare deny rule removes the tool from the model's
    context before any turn runs, and deny beats any allow in user settings.
  - ``--permission-mode dontAsk`` denies anything not pre-approved, and only
    ``mcp__granola__<tool>`` is pre-approved via ``--allowedTools``.
  - ``--max-turns 1``: the model gets one turn, whose only input is this app's
    prompt. The tool result (untrusted meeting text) is never shown to a model
    turn that could issue another call.

  Residual gap, stated honestly: the CLI cannot constrain MCP *arguments*
  before execution. The arguments come from a trusted prompt on turn 1, and
  ``extract_tool_result`` refuses the whole call unless exactly one
  ``tool_use`` occurred, for the expected tool, with byte-identical
  arguments. A Granola tool this app does not know about, combined with a
  user-level ``mcp__granola`` allow rule, would not be removed from context;
  its result would still be rejected here.
* Nothing from the subprocess (stdout, stderr, tool text) is logged or placed
  in job metadata or HTTP error bodies. Failures surface as short codes.
* Whether ``claude -p`` reuses the interactive session's OAuth grant under
  ``--strict-mcp-config`` is documented but has **not** been verified by the
  app's authors; ``scripts/granola_live_check.py`` exercises exactly this code
  path so the user can verify it locally with a synthetic meeting.

``FakeGranolaRetrievalTransport`` scripts responses for tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

GranolaTool = Literal["get_account_info", "list_meetings", "get_meetings", "get_meeting_transcript"]
ALLOWED_TOOLS: tuple[GranolaTool, ...] = (
    "get_account_info", "list_meetings", "get_meetings", "get_meeting_transcript"
)
# Every Granola MCP tool observed in the 2026-09-24 capability check. Tools not needed by
# a step are denied by name so they are removed from the model's context before it runs.
KNOWN_GRANOLA_TOOLS: tuple[str, ...] = (
    "get_account_info",
    "list_meeting_folders",
    "list_meetings",
    "get_meetings",
    "get_meeting_transcript",
    "query_granola_meetings",
)
MCP_CONFIG_FILE = "mcp.json"
_REMOTE_SERVER_TYPES = {"http", "sse", "streamable-http"}
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
    "relay_wrong_arguments",
    "relay_unexpected_call",
    "mcp_server_not_configured",
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


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))


def extract_tool_result(
    stream: bytes,
    *,
    tool: GranolaTool,
    arguments: Mapping[str, Any] | None = None,
    mcp_server: str = "granola",
) -> Any:
    """Return the parsed result of the single allowed tool call in a `stream-json` transcript.

    Accepts the event shapes Claude Code emits with ``--output-format stream-json
    --verbose``: ``assistant`` messages carrying ``tool_use`` blocks and ``user``
    messages carrying ``tool_result`` blocks that reference them. Any text the
    model wrote is ignored. Raises with a safe code when the tool was not
    called, another tool was called, the tool was called more than once, the
    call's ``input`` differs from the requested ``arguments``, the result is an
    error, or the result is not JSON. Rejection discards the entire call:
    nothing from a broadened or duplicated fetch reaches the caller.
    """
    expected = f"mcp__{mcp_server}__{tool}"
    uses: dict[str, tuple[str, Any]] = {}
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
                uses[block["id"]] = (str(block.get("name", "")), block.get("input"))
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                results.append((block["tool_use_id"], block.get("content"), bool(block.get("is_error"))))
    for name, _input in uses.values():
        if name != expected:
            raise GranolaRelayError("relay_wrong_tool", retryable=False)
    if len(uses) > 1:
        raise GranolaRelayError("relay_unexpected_call", retryable=False)
    if arguments is not None:
        for _name, actual in uses.values():
            if not isinstance(actual, Mapping) or canonical_arguments(actual) != canonical_arguments(arguments):
                raise GranolaRelayError("relay_wrong_arguments", retryable=False)
    matched = [(content, is_error) for use_id, content, is_error in results if use_id in uses]
    if not matched:
        raise GranolaRelayError("relay_no_tool_result", retryable=True)
    if len(matched) > 1:
        raise GranolaRelayError("relay_unexpected_call", retryable=False)
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


def claude_json_path(claude_json: Path | None = None) -> Path:
    return claude_json or Path(os.environ.get("CLAUDE_USER_CONFIG_JSON") or Path.home() / ".claude.json")


def _project_key(path: str | os.PathLike[str]) -> str:
    """Normalize a project path the way two spellings of one directory should compare.

    Claude Code keys local-scope servers by the absolute project path as the
    OS spells it (``C:\\Users\\…`` on Windows, ``/home/…`` on POSIX). Case
    folding and separator/`..` normalization follow the host OS, so a trusted
    path given with forward slashes or a trailing separator still matches.
    """
    text = os.fspath(path)
    if os.name == "nt":
        text = text.replace("/", "\\")
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def _validated_definition(entry: Any) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    url = entry.get("url")
    if kind not in _REMOTE_SERVER_TYPES or not isinstance(url, str) or not url.startswith("https://"):
        return None
    return {"type": str(kind), "url": url}


def locate_server_definition(
    name: str,
    *,
    project_paths: list[Path] | None = None,
    claude_json: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Find the user's own remote MCP server definition and return ``(scope, {type, url})``.

    Claude Code stores ``claude mcp add`` servers in ``~/.claude.json`` at
    **local scope** by default (``projects[<abs project path>].mcpServers``)
    and at **user scope** with ``--scope user`` (top-level ``mcpServers``).
    Lookup follows Claude Code's precedence — local before user — but only for
    the explicitly trusted ``project_paths`` (the app's own checkout, or an
    original checkout named on the live-check command line); other projects'
    entries are never read. Two trusted projects defining the server
    differently is ambiguous and fails closed. Headers, env, and anything else
    that could carry a secret are never copied; OAuth state stays in Claude Code.
    """
    try:
        raw = json.loads(claude_json_path(claude_json).read_bytes()[:2_000_000])
    except (OSError, ValueError) as exc:
        raise GranolaRelayError("mcp_server_not_configured", retryable=False) from exc
    if not isinstance(raw, dict):
        raise GranolaRelayError("mcp_server_not_configured", retryable=False)

    wanted = {_project_key(p) for p in project_paths or []}
    projects = raw.get("projects")
    local_matches: list[dict[str, str]] = []
    if wanted and isinstance(projects, dict):
        for key, project in projects.items():
            if not isinstance(key, str) or _project_key(key) not in wanted or not isinstance(project, dict):
                continue
            servers = project.get("mcpServers")
            entry = servers.get(name) if isinstance(servers, dict) else None
            if entry is None:
                continue
            definition = _validated_definition(entry)
            if definition is None:
                raise GranolaRelayError("mcp_server_not_configured", retryable=False)
            local_matches.append(definition)
    if local_matches:
        if any(match != local_matches[0] for match in local_matches[1:]):
            raise GranolaRelayError("mcp_server_not_configured", retryable=False)
        return "local", local_matches[0]

    servers = raw.get("mcpServers")
    entry = servers.get(name) if isinstance(servers, dict) else None
    definition = _validated_definition(entry) if entry is not None else None
    if definition is None:
        raise GranolaRelayError("mcp_server_not_configured", retryable=False)
    return "user", definition


def user_scope_server_definition(name: str, *, claude_json: Path | None = None) -> dict[str, str]:
    """User-scope-only lookup (top-level ``mcpServers`` of ``~/.claude.json``)."""
    return locate_server_definition(name, project_paths=None, claude_json=claude_json)[1]


def scoped_server_definition(project_paths: list[Path]) -> Callable[[str], dict[str, str]]:
    """Lookup bound to explicit trusted project paths: local scope there, then user scope."""

    def lookup(name: str) -> dict[str, str]:
        return locate_server_definition(name, project_paths=project_paths)[1]

    return lookup


def probe_server_scope(name: str, *, project_paths: list[Path], claude_json: Path | None = None) -> dict[str, Any]:
    """Metadata-only description of where the server is configured — no URL, headers or raw config."""
    try:
        scope, definition = locate_server_definition(name, project_paths=project_paths, claude_json=claude_json)
    except GranolaRelayError as exc:
        return {"configured": False, "scope": None, "type": None, "error_code": exc.code}
    return {"configured": True, "scope": scope, "type": definition["type"], "error_code": None}


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
        max_turns: int = 1,
        server_definition: Callable[[str], Mapping[str, str]] = user_scope_server_definition,
    ) -> None:
        self.model = model
        self.executable = executable
        self.mcp_server = mcp_server
        self.timeout_seconds = timeout_seconds
        self.max_stderr_bytes = max_stderr_bytes
        self.max_turns = max_turns
        self.server_definition = server_definition
        self.forbidden_roots = [Path(root).resolve() for root in forbidden_roots]

    def denied_tools(self, tool: GranolaTool) -> list[str]:
        return [f"mcp__{self.mcp_server}__{other}" for other in KNOWN_GRANOLA_TOOLS if other != tool]

    def mcp_config(self) -> dict[str, Any]:
        """The exclusive MCP config for one call: the user's granola endpoint and nothing else."""
        definition = dict(self.server_definition(self.mcp_server))
        if set(definition) - {"type", "url"}:
            raise GranolaRelayError("mcp_server_not_configured", retryable=False)
        return {"mcpServers": {self.mcp_server: definition}}

    def describe(self) -> dict[str, Any]:
        return {
            "transport": "claude_code_mcp_relay",
            "mode": "user_triggered_retrieval",
            "unattended_discovery": False,
            "requires_credentials": True,
            "credential_holder": "claude_code_user_oauth",
            "mcp_server": self.mcp_server,
            "allowed_tools": [f"mcp__{self.mcp_server}__{tool}" for tool in ALLOWED_TOOLS],
            "denied_tools": [f"mcp__{self.mcp_server}__{tool}" for tool in KNOWN_GRANOLA_TOOLS if tool not in ALLOWED_TOOLS],
            "strict_mcp_config": True,
            "max_turns": self.max_turns,
            "label": "Manual Granola check through your Claude Code MCP connection",
        }

    def command(
        self, tool: GranolaTool, executable: str | None = None, *, mcp_config_path: Path | str = MCP_CONFIG_FILE
    ) -> list[str]:
        """The restricted CLI contract, enforced before the model's first turn.

        Only the generated MCP config is loaded, built-in tools are removed, every
        other known Granola tool is denied by name, only the one tool is
        pre-approved, nothing else can be approved (`dontAsk`), and the model
        gets a single turn.
        """
        if tool not in ALLOWED_TOOLS:
            raise GranolaRelayError("relay_wrong_tool", retryable=False)
        return [
            executable or shutil.which(self.executable) or self.executable,
            "-p",
            "--max-turns",
            str(self.max_turns),
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config_path),
            "--setting-sources",
            "user",
            "--tools",
            "",
            "--allowedTools",
            f"mcp__{self.mcp_server}__{tool}",
            "--disallowedTools",
            *self.denied_tools(tool),
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
    def spawn_kwargs() -> dict[str, Any]:
        """Detach the CLI from the app's terminal/session on each platform.

        `start_new_session` is POSIX-only; Windows gets its own process group so
        console signals aimed at the app never reach the relay and vice versa.
        """
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        return {"start_new_session": True}

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        # terminate() is SIGTERM on POSIX and TerminateProcess on Windows;
        # kill() is the hard fallback on both. Every step tolerates an
        # already-exited child.
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (ProcessLookupError, OSError, TimeoutError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def call(self, tool: GranolaTool, arguments: Mapping[str, Any]) -> Any:
        mcp_config = self.mcp_config()
        temp_dir = Path(tempfile.mkdtemp(prefix="se-granola-relay-"))
        proc: asyncio.subprocess.Process | None = None
        try:
            self._assert_isolated(temp_dir)
            config_path = temp_dir / MCP_CONFIG_FILE
            config_path.write_text(json.dumps(mcp_config, separators=(",", ":")), encoding="utf-8")
            config_path.chmod(0o600)
            command = self.command(tool, mcp_config_path=config_path)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(temp_dir),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **self.spawn_kwargs(),
                )
            except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
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
            return extract_tool_result(stdout, tool=tool, arguments=arguments, mcp_server=self.mcp_server)
        finally:
            if proc is not None:
                await self._terminate(proc)
            shutil.rmtree(temp_dir, ignore_errors=True)


class FakeGranolaRetrievalTransport:
    """Scripted transport for tests: `responses[(tool, key)]` or callables; records calls."""

    def __init__(self, *, mcp_server: str = "granola") -> None:
        self.mcp_server = mcp_server
        self.account: Any = {"workspace": {"id": "ws-synthetic", "display_name": "Synthetic Workspace"}, "note_access_scope": ["personal"]}
        self.account_sequence: list[Any] = []  # scripted per-call accounts, e.g. a mid-batch sign-in switch
        self.listings: dict[str, Any] = {}
        self.meetings: dict[str, Any] = {}
        self.batch_extra: list[Any] = []  # rows get_meetings returns beyond the requested ids
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
            if self.account_sequence:
                self.account = self.account_sequence.pop(0)
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
            return {"meetings": found + list(self.batch_extra)}
        if tool == "get_meeting_transcript":
            meeting_id = args.get("meeting_id", "")
            self._maybe_fail(tool, meeting_id)
            if meeting_id not in self.transcripts:
                raise GranolaRelayError("tool_not_found", retryable=False)
            return self.transcripts[meeting_id]
        raise GranolaRelayError("relay_wrong_tool", retryable=False)
