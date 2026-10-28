"""MCP server health + reconnect for the header status chip.

Skills run as `claude -p` subprocesses launched from the workspace folder, so what
matters is which MCP servers *that* folder can see and reach — not what an
interactive session in the repo sees. This service asks the same CLI, from the same
folder (`claude mcp list`), and offers the two fixes the app can safely perform:

* **register** — add a remote server at *user scope* (`claude mcp add --scope user`)
  when it is only registered for another folder (the usual "works in my terminal,
  not from the app" cause) or not registered at all. Only https remote servers, only
  for the tracked names, and the URL comes from the user's own config or a built-in
  default — never from the request.
* **sign in** — OAuth for remote servers can only be completed inside an interactive
  Claude Code session (`/mcp`). The app opens a terminal running `claude` in the
  workspace; the user finishes the browser sign-in, and the UI re-checks.

Credentials are never read or stored here: Claude Code keeps OAuth state itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from integrations.granola_mcp_relay import GranolaRelayError, locate_server_definition

logger = logging.getLogger(__name__)

# The servers skills actually use. `transport` decides which fixes are possible:
# stdio servers (a local command, often with env credentials) can't be re-registered
# safely from here, so they only get guidance.
TRACKED_SERVERS: dict[str, dict[str, str]] = {
    "gong": {"label": "Gong", "transport": "stdio", "purpose": "Recorded-call transcripts for skills"},
    "granola": {"label": "Granola", "transport": "remote", "purpose": "Transcripts for calls not recorded in Gong", "default_url": "https://mcp.granola.ai/mcp"},
    "salesforce": {"label": "Salesforce (MCP)", "transport": "stdio", "purpose": "Opportunity data read by skills"},
    "notion": {"label": "Notion", "transport": "remote", "purpose": "Customer pages and notes for skills", "default_url": "https://mcp.notion.com/mcp"},
}

_STATUS_CACHE_TTL = 60.0
_LIST_TIMEOUT = 90.0     # `claude mcp list` health-checks every server, which can be slow
_ADD_TIMEOUT = 60.0

# "name: target - ✔ Connected". The target may contain " - " or ": ", so the name is the
# text before the first ": " and the status is whatever follows the LAST " - ".
_LINE_RE = re.compile(r"^(?P<name>[^:]+?):\s+(?P<target>.+)\s+-\s+(?P<status>\S.*)$")


def parse_mcp_list(text: str) -> dict[str, str]:
    """Map server name -> "ok" | "needs_auth" | "failed" | "unknown" from `claude mcp list` output.

    Matches on the status words, not the ✔/✗/! glyphs, so a console that mangles them
    still parses.
    """
    states: dict[str, str] = {}
    for raw in (text or "").splitlines():
        match = _LINE_RE.match(raw.strip())
        if not match:
            continue
        status = match.group("status").lower()
        if "fail" in status or "error" in status:
            state = "failed"
        elif "authenticat" in status:
            state = "needs_auth"
        elif "connected" in status:
            state = "ok"
        else:
            state = "unknown"
        states[match.group("name").strip()] = state
    return states


def launch_claude_terminal(executable: str, workspace: Path) -> None:
    """Open a new terminal window running `claude` in `workspace` (Windows/macOS)."""
    if sys.platform == "win32":
        subprocess.Popen([executable], cwd=str(workspace), creationflags=subprocess.CREATE_NEW_CONSOLE)
    elif sys.platform == "darwin":
        command = f"cd {shlex.quote(str(workspace))} && {shlex.quote(executable)}"
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen([
            "osascript",
            "-e", 'tell application "Terminal" to activate',
            "-e", f'tell application "Terminal" to do script "{escaped}"',
        ])
    else:
        raise NotImplementedError(sys.platform)


class McpHealthService:
    def __init__(
        self,
        *,
        workspace: Path,
        repo_root: Path,
        claude_json: Path | None = None,
        launcher: Callable[[str, Path], None] = launch_claude_terminal,
    ) -> None:
        self.workspace = workspace
        self.repo_root = repo_root          # the one other project path whose registrations we trust
        self.claude_json = claude_json
        self.launcher = launcher
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    async def status(self, *, force: bool = False) -> dict[str, Any]:
        if shutil.which("claude") is None:
            return {"available": False, "message": "The `claude` CLI isn't installed or isn't on PATH.", "servers": []}
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if self._cache and not force and (now - self._cache[0]) < _STATUS_CACHE_TTL:
                return self._cache[1]
            result = await self._check()
            if result.get("available"):
                self._cache = (asyncio.get_event_loop().time(), result)
            return result

    async def _check(self) -> dict[str, Any]:
        rc, out, err = await self._run(["mcp", "list"], timeout=_LIST_TIMEOUT)
        if rc != 0:
            logger.info("`claude mcp list` failed (rc=%s): %s", rc, err.strip()[:200])
            return {"available": False, "message": "Couldn't check MCP servers (`claude mcp list` failed).", "servers": []}
        seen = parse_mcp_list(out)
        servers = [self._describe(name, spec, seen.get(name)) for name, spec in TRACKED_SERVERS.items()]
        return {"available": True, "servers": servers, "workspace": str(self.workspace)}

    def _describe(self, name: str, spec: dict[str, str], state: str | None) -> dict[str, Any]:
        label, remote = spec["label"], spec["transport"] == "remote"
        entry: dict[str, Any] = {"name": name, "label": label, "remote": remote, "command": None,
                                 "purpose": spec.get("purpose", "")}
        if state == "ok":
            return {**entry, "state": "ok", "message": "Connected.", "action": None}
        if state == "needs_auth":
            return {**entry, "state": "needs_auth", "action": "signin" if remote else "manual",
                    "message": "Needs sign-in. Claude Code keeps the login, so this is a one-time step."}
        if state in ("failed", "unknown"):
            return {**entry, "state": "failed", "action": "recheck", "command": "claude mcp list",
                    "message": "Registered but not responding — it may be offline, or its credentials changed."}
        # Not listed from the workspace folder.
        elsewhere = self._registered_in_repo_project(name)
        if remote and (elsewhere or spec.get("default_url")):
            return {**entry, "state": "not_visible" if elsewhere else "not_configured", "action": "register",
                    "message": (
                        "Registered only for the repo folder, so skill runs from the app can't see it."
                        if elsewhere else "Not set up on this machine."
                    )}
        return {**entry, "state": "not_configured", "action": "manual",
                "command": f"claude mcp add {name} --scope user -- <command>",
                "message": "Not visible from the app's workspace folder. Add it at user scope with `claude mcp add --scope user` (see the Claude Code docs for this server's command)."}

    def _registered_in_repo_project(self, name: str) -> bool:
        try:
            locate_server_definition(name, project_paths=[self.repo_root], claude_json=self.claude_json)
        except GranolaRelayError:
            return False
        return True

    # ------------------------------------------------------------------
    # fixes
    # ------------------------------------------------------------------
    async def register(self, name: str) -> dict[str, Any]:
        spec = TRACKED_SERVERS.get(name)
        if spec is None or spec["transport"] != "remote":
            return {"ok": False, "message": "This server can't be registered from the app."}
        kind, url = "http", spec.get("default_url", "")
        try:
            _scope, definition = locate_server_definition(
                name, project_paths=[self.repo_root], claude_json=self.claude_json)
            kind, url = definition["type"], definition["url"]
        except GranolaRelayError:
            pass
        if not url.startswith("https://"):
            return {"ok": False, "message": "No https URL is known for this server."}
        rc, _out, err = await self._run(
            ["mcp", "add", "--transport", kind, "--scope", "user", name, url], timeout=_ADD_TIMEOUT)
        self._cache = None
        if rc != 0:
            logger.info("`claude mcp add` for %s failed (rc=%s): %s", name, rc, err.strip()[:200])
            return {"ok": False, "message": f"`claude mcp add` failed: {err.strip()[:200] or 'unknown error'}"}
        return {"ok": True, "message": f"{spec['label']} registered for all folders. Checking it now…"}

    async def signin(self, name: str) -> dict[str, Any]:
        spec = TRACKED_SERVERS.get(name)
        if spec is None or spec["transport"] != "remote":
            return {"started": False, "message": "Sign-in for this server happens outside the app."}
        steps = [f"In the Claude Code window that opens, type /mcp", f"Select {name} → Authenticate",
                 "Finish the sign-in in your browser"]
        exe = shutil.which("claude")
        if exe is None:
            return {"started": False, "message": "The `claude` CLI isn't installed or isn't on PATH.", "steps": steps}
        try:
            self.launcher(exe, self.workspace)
        except NotImplementedError:
            return {"started": False, "steps": steps, "command": "claude",
                    "message": f"Can't open a terminal on this OS. In a terminal, run `claude` from {self.workspace}."}
        except Exception as exc:  # noqa: BLE001
            logger.info("Launching claude for MCP sign-in failed: %s", exc)
            return {"started": False, "steps": steps, "command": "claude",
                    "message": "Couldn't open a terminal. Run `claude` yourself from the workspace folder."}
        self._cache = None
        return {"started": True, "steps": steps, "message": "Opened Claude Code in a new window."}

    # ------------------------------------------------------------------
    async def _run(self, args: list[str], *, timeout: float) -> tuple[int, str, str]:
        """Run `claude <args>` in the workspace folder; never raises (rc -1 on failure)."""
        exe = shutil.which("claude") or "claude"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *args, cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            if proc is not None and proc.returncode is None:
                proc.kill()   # don't leave a hung health check running
            logger.debug("claude %s failed: %s", args[:2], exc)
            return -1, "", str(exc)
