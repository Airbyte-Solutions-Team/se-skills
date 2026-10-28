"""Deterministic tests for MCP connection health + reconnect (header status chip)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import webapp.app as app
from integrations import mcp_health
from integrations.mcp_health import McpHealthService, parse_mcp_list

LIST_OUTPUT = """Checking MCP server health…

notion: https://mcp.notion.com/mcp (HTTP) - ✔ Connected
salesforce: sf-mcp-server --orgs DEFAULT_TARGET_ORG --toolsets data,orgs,users --allow-non-ga-tools - ✔ Connected
gong: npx -y gongio-mcp - ✔ Connected
claude-design: https://api.anthropic.com/v1/design/mcp (HTTP) - ! Needs authentication
playwright: npx -y @playwright/mcp@latest - ✗ Failed to connect
"""


def _svc(tmp_path: Path, list_output: str = LIST_OUTPUT, claude_json: dict | None = None, rc: int = 0):
    workspace, repo = tmp_path / "ws", tmp_path / "repo"
    workspace.mkdir()
    repo.mkdir()
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps(claude_json or {}), encoding="utf-8")
    launched: list[tuple[str, Path]] = []
    svc = McpHealthService(
        workspace=workspace, repo_root=repo, claude_json=cj,
        launcher=lambda exe, ws: launched.append((exe, ws)),
    )
    calls: list[list[str]] = []

    async def fake_run(args, *, timeout):
        calls.append(args)
        return (rc, list_output if args[:2] == ["mcp", "list"] else "", "boom" if rc else "")

    svc._run = fake_run  # type: ignore[method-assign]
    return svc, calls, launched, repo


@pytest.fixture(autouse=True)
def _claude_on_path(monkeypatch):
    monkeypatch.setattr(mcp_health.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)


def _by_name(status):
    return {s["name"]: s for s in status["servers"]}


def test_parse_mcp_list_maps_each_state():
    states = parse_mcp_list(LIST_OUTPUT)
    assert states == {
        "notion": "ok", "salesforce": "ok", "gong": "ok",
        "claude-design": "needs_auth", "playwright": "failed",
    }


def test_status_reports_only_tracked_servers_and_flags_missing_granola(tmp_path):
    svc, *_ = _svc(tmp_path)
    status = asyncio.run(svc.status())
    servers = _by_name(status)
    assert set(servers) == {"gong", "granola", "salesforce", "notion"}
    assert servers["gong"]["state"] == "ok" and servers["gong"]["action"] is None
    # Granola isn't listed from the workspace folder and isn't registered anywhere we trust.
    assert servers["granola"]["state"] == "not_configured"
    assert servers["granola"]["action"] == "register"


def test_every_server_says_what_it_is_used_for(tmp_path):
    """The Status menu shows a purpose line so two Salesforce rows (CLI vs MCP) aren't confusing."""
    servers = _by_name(asyncio.run(_svc(tmp_path)[0].status()))
    assert all(s["purpose"] for s in servers.values())
    assert "skills" in servers["salesforce"]["purpose"]


def test_granola_registered_only_for_repo_folder_is_not_visible(tmp_path):
    repo_key = str(tmp_path / "repo")
    svc, *_ = _svc(tmp_path, claude_json={
        "projects": {repo_key: {"mcpServers": {"granola": {"type": "http", "url": "https://mcp.granola.ai/mcp"}}}},
    })
    granola = _by_name(asyncio.run(svc.status()))["granola"]
    assert granola["state"] == "not_visible"
    assert granola["action"] == "register"
    assert "repo folder" in granola["message"]


def test_needs_auth_remote_offers_signin_and_stdio_only_manual(tmp_path):
    out = LIST_OUTPUT.replace("notion: https://mcp.notion.com/mcp (HTTP) - ✔ Connected",
                              "notion: https://mcp.notion.com/mcp (HTTP) - ! Needs authentication")
    out = out.replace("gong: npx -y gongio-mcp - ✔ Connected", "gong: npx -y gongio-mcp - ✗ Failed to connect")
    servers = _by_name(asyncio.run(_svc(tmp_path, out)[0].status()))
    assert servers["notion"]["state"] == "needs_auth" and servers["notion"]["action"] == "signin"
    assert servers["gong"]["state"] == "failed" and servers["gong"]["action"] == "recheck"


def test_status_is_cached_until_forced(tmp_path):
    svc, calls, *_ = _svc(tmp_path)

    async def go():
        await svc.status()
        await svc.status()
        await svc.status(force=True)

    asyncio.run(go())
    assert [c for c in calls if c[:2] == ["mcp", "list"]] == [["mcp", "list"]] * 2


def test_status_unavailable_when_list_fails(tmp_path):
    status = asyncio.run(_svc(tmp_path, rc=1)[0].status())
    assert status["available"] is False and status["servers"] == []


def test_status_unavailable_without_claude_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_health.shutil, "which", lambda name: None)
    assert asyncio.run(_svc(tmp_path)[0].status())["available"] is False


def test_register_adds_at_user_scope_using_the_users_own_url(tmp_path):
    repo_key = str(tmp_path / "repo")
    svc, calls, *_ = _svc(tmp_path, claude_json={
        "projects": {repo_key: {"mcpServers": {"granola": {"type": "http", "url": "https://custom.example/mcp"}}}},
    })
    result = asyncio.run(svc.register("granola"))
    assert result["ok"] is True
    assert calls[-1] == ["mcp", "add", "--transport", "http", "--scope", "user", "granola", "https://custom.example/mcp"]


def test_register_falls_back_to_builtin_default_url(tmp_path):
    svc, calls, *_ = _svc(tmp_path)
    assert asyncio.run(svc.register("notion"))["ok"] is True
    assert calls[-1][-1] == "https://mcp.notion.com/mcp"


@pytest.mark.parametrize("name", ["gong", "salesforce", "unknown"])
def test_register_refuses_stdio_and_untracked_servers(tmp_path, name):
    svc, calls, *_ = _svc(tmp_path)
    assert asyncio.run(svc.register(name))["ok"] is False
    assert calls == []


def test_register_reports_cli_failure_and_clears_cache(tmp_path):
    svc, calls, *_ = _svc(tmp_path)
    asyncio.run(svc.status())
    assert svc._cache is not None
    svc._run = _failing_run  # type: ignore[method-assign]
    result = asyncio.run(svc.register("granola"))
    assert result["ok"] is False and "already exists" in result["message"]
    assert svc._cache is None


async def _failing_run(args, *, timeout):
    return 1, "", "MCP server granola already exists"


def test_signin_opens_claude_in_the_workspace(tmp_path):
    svc, _calls, launched, _repo = _svc(tmp_path)
    result = asyncio.run(svc.signin("granola"))
    assert result["started"] is True
    assert launched == [("/usr/bin/claude", svc.workspace)]
    assert any("/mcp" in step for step in result["steps"])


def test_signin_degrades_to_instructions_when_no_terminal_can_open(tmp_path):
    svc, *_ = _svc(tmp_path)

    def no_terminal(exe, ws):
        raise NotImplementedError("linux")

    svc.launcher = no_terminal
    result = asyncio.run(svc.signin("granola"))
    assert result["started"] is False and result["command"] == "claude"


def test_signin_refuses_stdio_servers(tmp_path):
    svc, _calls, launched, _ = _svc(tmp_path)
    assert asyncio.run(svc.signin("gong"))["started"] is False
    assert launched == []


@pytest.fixture
def client(tmp_path):
    svc, *_ = _svc(tmp_path)
    app.app.state.mcp_health_service = svc
    return TestClient(app.app)


def test_routes_status_and_unknown_server(client):
    assert client.get("/api/mcp/status").json()["available"] is True
    assert client.post("/api/mcp/not-a-server/register").status_code == 404
    assert client.post("/api/mcp/not-a-server/signin").status_code == 404
    assert client.post("/api/mcp/gong/register").json()["ok"] is False
