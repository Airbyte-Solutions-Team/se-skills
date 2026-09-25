"""User-triggered Granola retrieval: relay parsing/command, service outcomes, and HTTP boundary.

Everything here runs against the scripted `FakeGranolaRetrievalTransport`. No Claude Code
process, no Granola MCP, and no credential is touched; the live connection remains a
Gary-local step recorded in the PR.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date as _date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from integrations.granola import ManualGranolaImportAdapter
from integrations.granola_mcp_relay import (
    ClaudeCodeMcpRelay,
    FakeGranolaRetrievalTransport,
    GranolaRelayError,
    extract_tool_result,
    result_shape_report,
)
from routes.command_center import router
from services.command_center_operations_service import CommandCenterOperationsService
from services.command_center_read_service import CommandCenterReadService
from services.evidence_ledger_service import EvidenceLedgerService
from services.granola_retrieval_service import (
    MAX_LISTED,
    MAX_SELECTION,
    GranolaRetrievalError,
    GranolaRetrievalService,
    account_shape_report,
)
from services.job_service import JobService

from eval.tests.test_command_center_routes import (
    RejectingStateService,
    ResolvedWorkspace,
    UnusedExecutor,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "command_center" / "granola"
MEETING = json.loads((FIXTURES / "mcp_meeting_synthetic.json").read_text(encoding="utf-8"))
MID = MEETING["id"]
SECRET = "SYNTHETIC-TRANSCRIPT-SENTINEL"


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def _listed(meeting_id: str, title: str = "Synthetic listed meeting") -> dict:
    return {
        "id": meeting_id,
        "title": title,
        "date": "Sep 24, 2026 12:30 PM EDT",
        "url": f"https://notes.granola.ai/d/{meeting_id}",
        "known_participants": [{"name": "Synthetic SE", "email": "se@example.invalid"}],
        "summary": SECRET + " summary must not be listed",
        "captured_by_me": True,
        "is_workspace_visible": False,
    }


def _detail(meeting_id: str, **overrides) -> dict:
    row = {**MEETING, "id": meeting_id, "url": f"https://notes.granola.ai/d/{meeting_id}"}
    row.update(overrides)
    return row


def _service(tmp_path: Path) -> tuple[GranolaRetrievalService, FakeGranolaRetrievalTransport, EvidenceLedgerService, JobService]:
    customers = tmp_path / "customers"
    customers.mkdir(exist_ok=True)
    transport = FakeGranolaRetrievalTransport()
    ledger = EvidenceLedgerService(customers)
    jobs = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    service = GranolaRetrievalService(transport=transport, adapter=ManualGranolaImportAdapter(), ledger=ledger, job_service=jobs)
    return service, transport, ledger, jobs


async def _wait(jobs: JobService, job_id: str) -> dict:
    for _ in range(200):
        job = jobs.get_job(job_id)
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("retrieval job did not finish")


async def _retrieve(service: GranolaRetrievalService, jobs: JobService, ids: list[str]) -> dict:
    started = await service.start_retrieval(ids)
    return await _wait(jobs, started["job_id"])


def _prime(transport: FakeGranolaRetrievalTransport, *ids: str, transcript: bool = True) -> None:
    for meeting_id in ids:
        transport.meetings[meeting_id] = _detail(meeting_id)
        if transcript:
            transport.transcripts[meeting_id] = {"transcript": SECRET + " transcript text", "created_at": "2026-09-24T16:30:00Z"}


# ------------------------------------------------------------------ relay


def _event(kind: str, block: dict) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": [block]}})


def _definition(_name: str) -> dict[str, str]:
    return {"type": "http", "url": "https://mcp.granola.ai/mcp"}


_FAKE_CLAUDE = r'''
import json, os, sys, time
args = sys.argv[1:]
sys.stdin.read()
if "--sleep" in os.environ.get("FAKE_CLAUDE_MODE", ""):
    time.sleep(8)
mcp_config = args[args.index("--mcp-config") + 1]
allowed = args[args.index("--allowedTools") + 1]
def ev(kind, block):
    return json.dumps({"type": kind, "message": {"role": kind, "content": [block]}})
payload = {"workspace": {"id": "ws-fake-exec", "display_name": "Fake"}, "seen": {
    "mcp_config": mcp_config, "cwd": os.getcwd(), "config_exists": os.path.exists(mcp_config),
    "config": json.load(open(mcp_config, encoding="utf-8")), "tools_flag": args[args.index("--tools") + 1],
    "strict": "--strict-mcp-config" in args, "permission_prompts": args[args.index("--permission-prompts") + 1]}}
print(ev("assistant", {"type": "text", "text": "calling"}))
print(ev("assistant", {"type": "tool_use", "id": "t1", "name": allowed, "input": {}}))
print(ev("user", {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(payload)}))
print(ev("assistant", {"type": "text", "text": "done"}))
'''


def _fake_claude(tmp_path: Path) -> Path:
    """A stand-in `claude` on PATH-style: `claude.cmd` (npm-shim shape) on Windows, a sh script elsewhere."""
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = tmp_path / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(0o700)
    return shim


def test_relay_launches_real_subprocess_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    shim = _fake_claude(tmp_path)
    relay = ClaudeCodeMcpRelay(
        model="synthetic-model", forbidden_roots=[Path.cwd()], executable=str(shim), server_definition=_definition
    )
    result = asyncio.run(relay.call("get_account_info", {}))
    seen = result["seen"]
    assert result["workspace"]["id"] == "ws-fake-exec"
    assert seen["config_exists"] is True and seen["strict"] is True
    assert seen["config"] == {"mcpServers": {"granola": {"type": "http", "url": "https://mcp.granola.ai/mcp"}}}
    assert seen["tools_flag"] == "" and seen["permission_prompts"] == "none"
    config_path = Path(seen["mcp_config"])
    assert config_path.name == "mcp.json" and not config_path.exists() and not config_path.parent.exists()
    assert Path(seen["cwd"]).resolve() == config_path.parent.resolve()


def test_relay_times_out_and_terminates_subprocess_portably(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "--sleep")
    shim = _fake_claude(tmp_path)
    relay = ClaudeCodeMcpRelay(
        model="synthetic-model", forbidden_roots=[], executable=str(shim), server_definition=_definition, timeout_seconds=1.0
    )
    started = time.monotonic()
    with pytest.raises(GranolaRelayError) as exc:
        asyncio.run(relay.call("get_account_info", {}))
    assert exc.value.code == "relay_timeout" and exc.value.retryable
    assert time.monotonic() - started < 15


def test_relay_command_restricts_tools_before_execution() -> None:
    relay = ClaudeCodeMcpRelay(model="synthetic-model", forbidden_roots=[], server_definition=_definition)
    cmd = relay.command("list_meetings", executable="/usr/bin/claude", mcp_config_path="/tmp/x/mcp.json")
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--mcp-config") + 1] == "/tmp/x/mcp.json"
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__granola__list_meetings"
    denied_start = cmd.index("--disallowedTools") + 1
    denied = cmd[denied_start:cmd.index("--permission-mode")]
    assert set(denied) == {
        "mcp__granola__get_account_info", "mcp__granola__list_meeting_folders", "mcp__granola__get_meetings",
        "mcp__granola__get_meeting_transcript", "mcp__granola__query_granola_meetings",
    }
    assert "mcp__granola__list_meetings" not in denied
    assert "--no-session-persistence" in cmd and "--disable-slash-commands" in cmd
    assert "dontAsk" in cmd and "--output-format" in cmd
    assert relay.mcp_config() == {"mcpServers": {"granola": {"type": "http", "url": "https://mcp.granola.ai/mcp"}}}
    with pytest.raises(GranolaRelayError) as exc:
        relay.command("query_granola_meetings")  # type: ignore[arg-type]
    assert exc.value.code == "relay_wrong_tool"


def test_relay_mcp_config_copies_only_type_and_url_from_user_scope(tmp_path) -> None:
    from integrations.granola_mcp_relay import user_scope_server_definition

    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"granola": {
        "type": "http", "url": "https://mcp.granola.ai/mcp", "headers": {"Authorization": "Bearer SECRET-TOKEN"},
    }}}))
    assert user_scope_server_definition("granola", claude_json=claude_json) == {"type": "http", "url": "https://mcp.granola.ai/mcp"}
    claude_json.write_text(json.dumps({"mcpServers": {"other": {"type": "http", "url": "https://x.invalid/"}}}))
    with pytest.raises(GranolaRelayError) as exc:
        user_scope_server_definition("granola", claude_json=claude_json)
    assert exc.value.code == "mcp_server_not_configured"
    claude_json.write_text(json.dumps({"mcpServers": {"granola": {"type": "stdio", "command": "evil"}}}))
    with pytest.raises(GranolaRelayError) as exc:
        user_scope_server_definition("granola", claude_json=claude_json)
    assert exc.value.code == "mcp_server_not_configured"
    with pytest.raises(GranolaRelayError) as exc:
        user_scope_server_definition("granola", claude_json=tmp_path / "absent.json")
    assert exc.value.code == "mcp_server_not_configured"


def _claude_json(tmp_path: Path, **payload) -> Path:
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_GRANOLA = {"type": "http", "url": "https://mcp.granola.ai/mcp"}


def test_relay_finds_local_scope_server_only_for_trusted_projects(tmp_path: Path) -> None:
    from integrations.granola_mcp_relay import (
        locate_server_definition,
        probe_server_scope,
    )

    original = tmp_path / "original-checkout"
    sibling = tmp_path / "sibling-worktree"
    original.mkdir()
    sibling.mkdir()
    claude_json = _claude_json(
        tmp_path,
        projects={
            str(original): {"mcpServers": {"granola": {**_GRANOLA, "headers": {"Authorization": "Bearer " + SECRET}}}},
            str(tmp_path / "unrelated"): {"mcpServers": {"granola": {"type": "http", "url": "https://other.invalid/mcp"}}},
        },
    )
    # The app's own checkout (the sibling) has no local entry and there is no user-scope entry: fail closed.
    with pytest.raises(GranolaRelayError) as exc:
        locate_server_definition("granola", project_paths=[sibling], claude_json=claude_json)
    assert exc.value.code == "mcp_server_not_configured"
    probe = probe_server_scope("granola", project_paths=[sibling], claude_json=claude_json)
    assert probe == {"configured": False, "scope": None, "type": None, "error_code": "mcp_server_not_configured"}

    # Naming the original checkout explicitly picks up its local-scope entry (type+url only, no headers).
    scope, definition = locate_server_definition("granola", project_paths=[sibling, original], claude_json=claude_json)
    assert (scope, definition) == ("local", _GRANOLA)
    probe = probe_server_scope("granola", project_paths=[sibling, original], claude_json=claude_json)
    assert probe == {"configured": True, "scope": "local", "type": "http", "error_code": None}
    assert SECRET not in json.dumps(probe) and "mcp.granola.ai" not in json.dumps(probe)

    # Path spelling differences (trailing separator, `..`, forward slashes on Windows) still match.
    spelled = Path(str(original) + os.sep + "sub" + os.sep + "..")
    assert locate_server_definition("granola", project_paths=[spelled], claude_json=claude_json)[0] == "local"
    if os.name == "nt":
        forward = Path(str(original).replace("\\", "/").upper())
        assert locate_server_definition("granola", project_paths=[forward], claude_json=claude_json)[0] == "local"

    # Local scope wins over user scope for a trusted project; user scope is the fallback otherwise.
    claude_json = _claude_json(
        tmp_path,
        mcpServers={"granola": {"type": "sse", "url": "https://user.granola.ai/mcp"}},
        projects={str(original): {"mcpServers": {"granola": _GRANOLA}}},
    )
    assert locate_server_definition("granola", project_paths=[original], claude_json=claude_json) == ("local", _GRANOLA)
    assert locate_server_definition("granola", project_paths=[sibling], claude_json=claude_json) == (
        "user", {"type": "sse", "url": "https://user.granola.ai/mcp"},
    )

    # Two trusted projects with different definitions is ambiguous; a non-remote local entry is refused.
    claude_json = _claude_json(
        tmp_path,
        projects={
            str(original): {"mcpServers": {"granola": _GRANOLA}},
            str(sibling): {"mcpServers": {"granola": {"type": "http", "url": "https://other.granola.ai/mcp"}}},
        },
    )
    with pytest.raises(GranolaRelayError):
        locate_server_definition("granola", project_paths=[original, sibling], claude_json=claude_json)
    claude_json = _claude_json(
        tmp_path, mcpServers={"granola": _GRANOLA},
        projects={str(original): {"mcpServers": {"granola": {"type": "stdio", "command": "evil"}}}},
    )
    with pytest.raises(GranolaRelayError):
        locate_server_definition("granola", project_paths=[original], claude_json=claude_json)


def test_relay_scoped_definition_feeds_strict_single_server_config(tmp_path: Path, monkeypatch) -> None:
    from integrations.granola_mcp_relay import scoped_server_definition

    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.setenv(
        "CLAUDE_USER_CONFIG_JSON",
        str(_claude_json(tmp_path, projects={str(project): {"mcpServers": {"granola": _GRANOLA, "other": _GRANOLA}}})),
    )
    relay = ClaudeCodeMcpRelay(
        model="synthetic-model", forbidden_roots=[], server_definition=scoped_server_definition([project])
    )
    assert relay.mcp_config() == {"mcpServers": {"granola": _GRANOLA}}


def test_relay_rejects_argument_mismatch_and_duplicate_calls() -> None:
    wanted = {"time_range": "custom", "custom_start": "2026-09-24", "custom_end": "2026-09-24"}
    ok = "\n".join([
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__list_meetings",
                             "input": {"custom_end": "2026-09-24", "custom_start": "2026-09-24", "time_range": "custom"}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"meetings": []})}),
    ]).encode()
    assert extract_tool_result(ok, tool="list_meetings", arguments=wanted) == {"meetings": []}
    broadened = "\n".join([
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__list_meetings",
                             "input": {"time_range": "custom", "custom_start": "2025-01-01", "custom_end": "2026-09-24"}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"meetings": [{"id": SECRET}]})}),
    ]).encode()
    with pytest.raises(GranolaRelayError) as exc:
        extract_tool_result(broadened, tool="list_meetings", arguments=wanted)
    assert exc.value.code == "relay_wrong_arguments" and SECRET not in str(exc.value)
    duplicated = "\n".join([
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__get_meetings", "input": {"meeting_ids": [MID]}}),
        _event("assistant", {"type": "tool_use", "id": "t2", "name": "mcp__granola__get_meetings", "input": {"meeting_ids": [MID]}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"meetings": []})}),
        _event("user", {"type": "tool_result", "tool_use_id": "t2", "content": json.dumps({"meetings": [{"id": SECRET}]})}),
    ]).encode()
    with pytest.raises(GranolaRelayError) as exc:
        extract_tool_result(duplicated, tool="get_meetings", arguments={"meeting_ids": [MID]})
    assert exc.value.code == "relay_unexpected_call"
    unselected = "\n".join([
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__get_meetings", "input": {"meeting_ids": [MID, _uuid(9)]}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"meetings": []})}),
    ]).encode()
    with pytest.raises(GranolaRelayError) as exc:
        extract_tool_result(unselected, tool="get_meetings", arguments={"meeting_ids": [MID]})
    assert exc.value.code == "relay_wrong_arguments"


def test_relay_extracts_only_the_tool_result_and_ignores_prose() -> None:
    stream = "\n".join([
        _event("assistant", {"type": "text", "text": "Here is what I found: " + SECRET}),
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__get_account_info", "input": {}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": json.dumps({"workspace": {"id": "ws-1"}})}]}),
        _event("assistant", {"type": "text", "text": "Done."}),
        json.dumps({"type": "result", "result": SECRET}),
    ]).encode()
    assert extract_tool_result(stream, tool="get_account_info") == {"workspace": {"id": "ws-1"}}


def test_relay_rejects_wrong_tool_missing_result_and_tool_errors() -> None:
    wrong = _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__query_granola_meetings", "input": {}}).encode()
    with pytest.raises(GranolaRelayError) as exc:
        extract_tool_result(wrong, tool="list_meetings")
    assert exc.value.code == "relay_wrong_tool"
    with pytest.raises(GranolaRelayError) as exc:
        extract_tool_result(_event("assistant", {"type": "text", "text": "no call"}).encode(), tool="list_meetings")
    assert exc.value.code == "relay_no_tool_result" and exc.value.retryable
    errored = "\n".join([
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__get_meeting_transcript", "input": {}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "401 Unauthorized: token expired"}),
    ]).encode()
    with pytest.raises(GranolaRelayError) as exc:
        extract_tool_result(errored, tool="get_meeting_transcript")
    assert exc.value.code == "tool_auth_required"
    assert "token" not in str(exc.value)


def _result_stream(content) -> bytes:
    return "\n".join([
        _event("assistant", {"type": "tool_use", "id": "t1", "name": "mcp__granola__list_meetings", "input": {}}),
        _event("user", {"type": "tool_result", "tool_use_id": "t1", "content": content}),
    ]).encode()


def test_relay_parses_only_single_block_json_or_single_json_fence() -> None:
    payload = {"meetings": [{"id": _uuid(1), "title": "Solo " + SECRET}], "count": 1}
    assert extract_tool_result(_result_stream(json.dumps(payload)), tool="list_meetings") == payload
    assert extract_tool_result(_result_stream([{"type": "text", "text": json.dumps(payload)}]), tool="list_meetings") == payload
    fenced = "```json\n" + json.dumps(payload, indent=2) + "\n```\n"
    assert extract_tool_result(_result_stream([{"type": "text", "text": fenced}]), tool="list_meetings") == payload
    for bad in (
        [{"type": "text", "text": json.dumps(payload)}, {"type": "text", "text": json.dumps(payload)}],  # two blocks
        [{"type": "text", "text": "Here are the meetings " + SECRET + ": " + json.dumps(payload)}],  # prose-wrapped
        [{"type": "text", "text": "```json\n" + json.dumps(payload) + "\n```\nand a note " + SECRET}],  # fence + trailer
        [{"type": "text", "text": f'Here you go: <meetings count="1"><meeting id="{_uuid(1)}"><title>{SECRET}</title></meeting></meetings>'}],  # prose + XML
        [{"type": "text", "text": f'<meetings><meeting id="{_uuid(1)}"><title>{SECRET}</title></meeting></meetings> trailing {SECRET}'}],  # XML + trailer
        [{"type": "text", "text": "# Meetings\n- " + SECRET + " (Sep 24)\n"}],
        [{"type": "image", "data": SECRET}],
        [],
    ):
        with pytest.raises(GranolaRelayError) as exc:
            extract_tool_result(_result_stream(bad), tool="list_meetings")
        assert exc.value.code == "relay_unparseable_result" and exc.value.retryable
        printed = json.dumps(exc.value.shape)
        assert SECRET not in printed and _uuid(1) not in printed and "2026-09-24" not in printed
        assert "Solo" not in printed and "Sep 24" not in printed and "Here are" not in printed


def test_relay_result_shape_report_is_value_free_and_classifies_shapes() -> None:
    xml = f'<meetings count="65" from="2025-01-01" to="{SECRET}"><meeting id="{_uuid(3)}"><title>{SECRET}</title><secretTag>{SECRET}</secretTag></meeting></meetings>'
    shape = result_shape_report([{"type": "text", "text": xml}, {"type": "resource", "uri": SECRET}])
    assert shape["container"] == "list" and shape["block_count"] == 2 and shape["block_types"] == ["text", "resource"]
    (block,) = shape["text_blocks"]
    assert block["classification"] == "xml_like" and block["root_tag"] == "meetings"
    assert block["root_attribute_names"] == ["count", "from", "to"]
    assert block["inner_tag_counts"] == {"meeting": 1, "other": 1, "title": 1}
    assert block["length_bucket"] == "101-1k"
    printed = json.dumps(shape)
    assert SECRET not in printed and _uuid(3) not in printed and "secretTag" not in printed and "65" not in printed

    prose = result_shape_report("Found these " + SECRET + ' {"meetings": []} thanks')
    assert prose["text_blocks"][0]["classification"] == "prose_wrapped_json_object"
    assert prose["text_blocks"][0]["prefix_length_bucket"] == "1-100"
    md = result_shape_report([{"type": "text", "text": "# Meetings\n- one " + SECRET + "\n- two\n| a | b |"}])
    b = md["text_blocks"][0]
    assert b["classification"] == "markdown_like" and b["bullet_line_count_bucket"] == "1-100"
    assert result_shape_report(None) == {"container": "null", "block_count": 0, "block_types": [], "text_blocks": []}
    assert result_shape_report([{"type": "text", "text": "```json\n{}\n```"}])["text_blocks"][0]["classification"] == "fenced_json_object"
    assert result_shape_report([{"type": "text", "text": "   "}])["text_blocks"][0]["classification"] == "empty"
    for report in (shape, prose, md):
        assert SECRET not in json.dumps(report)


@pytest.mark.asyncio
async def test_relay_without_claude_executable_reports_runtime_unavailable(tmp_path) -> None:
    relay = ClaudeCodeMcpRelay(
        model="synthetic-model", forbidden_roots=[], executable=str(tmp_path / "missing-claude"), server_definition=_definition
    )
    with pytest.raises(GranolaRelayError) as exc:
        await relay.call("get_account_info", {})
    assert exc.value.code == "runtime_unavailable"


@pytest.mark.asyncio
async def test_relay_fails_closed_without_user_scope_server(tmp_path) -> None:
    shim = _fake_claude(tmp_path)
    relay = ClaudeCodeMcpRelay(
        model="synthetic-model", forbidden_roots=[], executable=str(shim),
        server_definition=lambda name: (_ for _ in ()).throw(GranolaRelayError("mcp_server_not_configured", retryable=False)),
    )
    with pytest.raises(GranolaRelayError) as exc:
        await relay.call("get_account_info", {})
    assert exc.value.code == "mcp_server_not_configured"


# ------------------------------------------------------------ connection


@pytest.mark.asyncio
async def test_connection_check_records_marker_only_and_claims_no_workspace_guarantee(tmp_path) -> None:
    service, transport, _ledger, _jobs = _service(tmp_path)
    assert service.connection()["last_check"] is None and service.connection()["workspace_guarantee"] is False
    # The observed live shape: email present, no documented workspace id anywhere.
    transport.account = {"email": "person@example.invalid", "name": "Person " + SECRET, "note_access_scope": ["personal"]}
    first = await service.check_connection()
    assert first["connected"] and first["workspace_guarantee"] is False
    assert first["account_source"] == "claude_code_active_granola_account"
    assert first["note_access_scope"] == ["personal"] and first["note_access_scope_hidden"] == 0

    # Scope entries are arbitrary provider strings: only fixed labels surface, the rest are counted.
    transport.account = {"note_access_scope": ["Workspace", "token " + SECRET, "person@example.invalid"]}
    scoped = await service.check_connection()
    assert scoped["note_access_scope"] == ["workspace"] and scoped["note_access_scope_hidden"] == 2
    assert SECRET not in json.dumps(scoped) and "example" not in json.dumps(scoped)
    transport.account = {"email": "person@example.invalid", "name": "Person " + SECRET, "note_access_scope": ["personal"]}
    first = await service.check_connection()
    assert "workspace" not in first and "email" not in json.dumps(first) and SECRET not in json.dumps(first)
    marker = tmp_path / "customers" / ".command-center" / "granola-connection.json"
    assert marker.exists() and set(json.loads(marker.read_text(encoding="utf-8"))) == {"checked_at"}
    if os.name != "nt":
        assert (marker.stat().st_mode & 0o077) == 0
    assert service.connection()["last_check"] == first["last_check"]

    # A different account/workspace between checks is neither detected nor claimed to be.
    transport.account = {"workspace": {"id": "ws-other", "display_name": "Other"}}
    second = await service.check_connection()
    assert second["connected"] and second["workspace_guarantee"] is False and "workspace" not in second
    listing = await service.list_meetings(time_range="this_week")
    assert listing["workspace_guarantee"] is False and listing["account_source"] == "claude_code_active_granola_account"

    # Non-object success is still an unexpected shape.
    transport.account = ["not", "an", "object"]
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.check_connection()
    assert exc.value.code == "unexpected_shape"


@pytest.mark.asyncio
async def test_connection_check_reports_auth_and_runtime_failures_safely(tmp_path) -> None:
    service, transport, _ledger, _jobs = _service(tmp_path)
    transport.fail("get_account_info", "*", GranolaRelayError("tool_auth_required", retryable=False))
    result = await service.check_connection()
    assert result == {**result, "connected": False, "error_code": "tool_auth_required", "retryable": False}
    assert SECRET not in json.dumps(result)
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.list_meetings(time_range="this_week")
    assert exc.value.code == "not_checked"


# --------------------------------------------------------------- listing


@pytest.mark.asyncio
async def test_listing_is_metadata_only_bounded_and_annotated_with_ledger_state(tmp_path) -> None:
    service, transport, ledger, _jobs = _service(tmp_path)
    await service.check_connection()
    known = _uuid(1)
    ledger.import_meetings([ManualGranolaImportAdapter().normalize(_detail(known), connection_id="claude-code-mcp")])
    transport.listings["this_week"] = {"count": 3, "meetings": [_listed(known, "Known"), _listed(_uuid(2), "New"), {"id": "not-a-uuid"}]}
    listing = await service.list_meetings(time_range="this_week")
    assert listing["shown"] == 2 and listing["rejected"] == 1 and not listing["truncated"]
    assert listing["max_selection"] == MAX_SELECTION and listing["unattended_discovery"] is False
    dumped = json.dumps(listing)
    assert SECRET not in dumped and "summary" not in dumped and "transcript" not in dumped
    by_id = {row["meeting_id"]: row for row in listing["meetings"]}
    assert by_id[known]["ledger"]["latest_revision"] == 1 and by_id[known]["ledger"]["source_id"]
    assert by_id[_uuid(2)]["ledger"] is None
    assert by_id[known]["participant_count"] == 1
    assert transport.calls[-1] == ("list_meetings", {"time_range": "this_week"})

    transport.listings["last_30_days"] = {"meetings": [_listed(_uuid(100 + n)) for n in range(MAX_LISTED + 5)]}
    big = await service.list_meetings(time_range="last_30_days")
    assert big["truncated"] and big["shown"] == MAX_LISTED and big["returned"] == MAX_LISTED + 5


@pytest.mark.asyncio
async def test_custom_range_is_validated_and_forwarded(tmp_path) -> None:
    from datetime import date

    service, transport, _ledger, _jobs = _service(tmp_path)
    await service.check_connection()
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.list_meetings(time_range="custom", custom_start=date(2026, 9, 2), custom_end=date(2026, 9, 1))
    assert exc.value.code == "invalid_range"
    transport.listings["custom"] = {"meetings": []}
    await service.list_meetings(time_range="custom", custom_start=date(2026, 9, 1), custom_end=date(2026, 9, 2))
    assert transport.calls[-1][1] == {"time_range": "custom", "custom_start": "2026-09-01", "custom_end": "2026-09-02"}


# ------------------------------------------------------------- retrieval


@pytest.mark.asyncio
async def test_retrieval_imports_then_reports_duplicate_and_edited(tmp_path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_connection()
    _prime(transport, MID)
    first = await _retrieve(service, jobs, [MID])
    assert first["ok"] and first["counts"] == {"imported": 1}
    row = first["results"][0]
    assert row["outcome"] == "imported" and row["revision"] == 1 and row["source_id"]
    assert SECRET not in json.dumps(first) and SECRET not in json.dumps(jobs.get_job(first["job_id"] if "job_id" in first else next(iter(jobs.jobs))))
    assert first["trigger"] == "user_triggered_retrieval"
    assert SECRET in json.dumps(ledger.read_content(row["source_id"], revision=1))

    second = await _retrieve(service, jobs, [MID])
    assert second["counts"] == {"already_known": 1}

    transport.transcripts[MID]["transcript"] = SECRET + " edited transcript"
    third = await _retrieve(service, jobs, [MID])
    assert third["counts"] == {"edited": 1} and third["results"][0]["revision"] == 2
    assert ledger.get_source(row["source_id"])["latest_revision"] == 2
    assert ledger.get_source(row["source_id"])["revisions"][-1]["trigger"] == "user_triggered_retrieval"


@pytest.mark.asyncio
async def test_retrieval_missing_transcript_is_pending_content_then_completes_later(tmp_path) -> None:
    service, transport, _ledger, jobs = _service(tmp_path)
    await service.check_connection()
    transport.meetings[MID] = _detail(MID)
    transport.meetings[MID].pop("transcript", None)
    transport.meetings[MID].pop("summary", None)
    job = await _retrieve(service, jobs, [MID])
    assert job["counts"] == {"pending_content": 1} and job["results"][0]["availability"] == "metadata_only"
    transport.transcripts[MID] = {"transcript": SECRET}
    later = await _retrieve(service, jobs, [MID])
    assert later["counts"] == {"edited": 1} and later["results"][0]["availability"] == "content_available"


@pytest.mark.asyncio
async def test_retrieval_ambiguous_404_lost_access_and_bounds(tmp_path) -> None:
    service, transport, _ledger, jobs = _service(tmp_path)
    await service.check_connection()
    ghost = _uuid(7)
    _prime(transport, MID)
    job = await _retrieve(service, jobs, [MID, ghost])
    by_id = {row["meeting_id"]: row for row in job["results"]}
    assert by_id[MID]["outcome"] == "imported"
    assert by_id[ghost]["outcome"] == "pending_content" and by_id[ghost]["availability"] == "pending_unknown"

    transport.fail("get_meeting_transcript", MID, GranolaRelayError("tool_access_denied", retryable=False))
    lost = await _retrieve(service, jobs, [MID])
    assert lost["counts"] == {"inaccessible": 1} and lost["results"][0]["availability"] == "access_lost"

    with pytest.raises(GranolaRetrievalError) as exc:
        await service.start_retrieval([_uuid(n) for n in range(MAX_SELECTION + 1)])
    assert exc.value.code == "selection_too_large"
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.start_retrieval(["not_1234567890abcd"])
    assert exc.value.code == "invalid_meeting_id"
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.start_retrieval([MID, MID.upper(), " "])
    assert exc.value.code == "invalid_meeting_id"
    dedup = await service.start_retrieval([MID, MID.upper()])
    assert dedup["requested"] == 1
    await _wait(jobs, dedup["job_id"])


@pytest.mark.asyncio
async def test_retrieval_transient_failure_is_retryable_and_second_attempt_imports(tmp_path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_connection()
    _prime(transport, MID)
    transport.fail("get_meetings", MID, GranolaRelayError("relay_timeout", retryable=True))
    first = await _retrieve(service, jobs, [MID])
    assert first["ok"] and first["counts"] == {"failed_retryable": 1}
    assert first["results"][0]["error_code"] == "relay_timeout" and first["import_id"] is None
    assert ledger.list_sources()["total"] == 0

    second = await _retrieve(service, jobs, [MID])
    assert second["counts"] == {"imported": 1}

    transport.fail("get_meeting_transcript", MID, GranolaRelayError("relay_no_tool_result", retryable=True))
    third = await _retrieve(service, jobs, [MID])
    assert third["counts"] == {"failed_retryable": 1}
    assert ledger.list_sources()["total"] == 1 and ledger.list_sources()["sources"][0]["latest_revision"] == 1


@pytest.mark.asyncio
async def test_retrieval_auth_loss_before_batch_fails_closed_and_job_is_body_free(tmp_path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_connection()
    _prime(transport, MID)
    transport.fail("get_account_info", "*", GranolaRelayError("tool_auth_required", retryable=False))
    job = await _retrieve(service, jobs, [MID])
    assert job["status"] == "error" and job["error_code"] == "tool_auth_required"
    assert ledger.list_sources()["total"] == 0
    assert SECRET not in json.dumps(job)
    assert set(job.keys()) >= {"kind", "status", "error_code"} and "raw" not in json.dumps(job).lower()


@pytest.mark.asyncio
async def test_account_change_between_list_and_retrieval_is_not_a_claimed_guarantee(tmp_path) -> None:
    service, transport, _ledger, jobs = _service(tmp_path)
    await service.check_connection()
    _prime(transport, MID)
    listing = await service.list_meetings(time_range="this_week")
    assert listing["workspace_guarantee"] is False
    # Sign-in changes after listing: retrieval proceeds for the ids the user explicitly selected,
    # and the result says so — no wrong_workspace error and no guarantee is asserted.
    transport.account = {"email": "other@example.invalid"}
    result = await _retrieve(service, jobs, [MID])
    assert result["counts"] == {"imported": 1}
    assert result["workspace_guarantee"] is False
    assert result["account_source"] == "claude_code_active_granola_account"
    assert [tool for tool, _ in transport.calls[-3:]] == ["get_account_info", "get_meetings", "get_meeting_transcript"]
    assert "other@" not in json.dumps(result) and SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_retrieval_imports_only_explicitly_selected_ids(tmp_path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_connection()
    other = _uuid(42)
    _prime(transport, MID, other)
    # get_meetings returns the requested meeting plus an unrequested one and a duplicate row; the
    # transcript for the requested meeting is stamped with the other meeting's id.
    requested_row = transport.meetings[MID]
    transport.batch_extra = [transport.meetings[other], dict(requested_row)]
    transport.transcripts[MID] = {**transport.transcripts[MID], "id": other}
    result = await _retrieve(service, jobs, [MID])
    assert result["counts"] == {"imported": 1} and result["unrequested_dropped"] == 2
    assert [row["meeting_id"] for row in result["results"]] == [MID]
    sources = ledger.list_sources()["sources"]
    assert [row["provider_object_id"] for row in sources] == [MID]
    content = ledger.read_content(sources[0]["source_id"], revision=1)
    assert transport.transcripts[MID]["transcript"] not in json.dumps(content)  # mismatched transcript discarded
    assert transport.calls.count(("get_meeting_transcript", {"meeting_id": other})) == 0
    assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_only_one_retrieval_runs_at_a_time(tmp_path) -> None:
    service, transport, _ledger, jobs = _service(tmp_path)
    await service.check_connection()
    gate = asyncio.Event()
    original = transport.call

    async def slow(tool, arguments):
        if tool == "get_meetings":
            await gate.wait()
        return await original(tool, arguments)

    transport.call = slow  # type: ignore[method-assign]
    _prime(transport, MID)
    started = await service.start_retrieval([MID])
    await asyncio.sleep(0.01)
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.start_retrieval([MID])
    assert exc.value.code == "retrieval_in_progress"
    gate.set()
    assert (await _wait(jobs, started["job_id"]))["counts"] == {"imported": 1}


# ---------------------------------------------------------------- routes


def _client(tmp_path: Path) -> tuple[TestClient, FakeGranolaRetrievalTransport]:
    (tmp_path / "customers").mkdir()
    app = FastAPI()
    workspace = ResolvedWorkspace()
    ledger = EvidenceLedgerService(tmp_path / "customers")
    transport = FakeGranolaRetrievalTransport()
    jobs = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    app.state.opportunity_workspace_service = workspace
    app.state.evidence_ledger_service = ledger
    app.state.granola_adapter = ManualGranolaImportAdapter()
    app.state.job_service = jobs
    app.state.command_center_operations_service = CommandCenterOperationsService(
        ledger=ledger, workspace_service=workspace, state_service=RejectingStateService(), job_service=jobs, executor=UnusedExecutor(),
    )
    app.state.command_center_read_service = CommandCenterReadService(
        customers_dir=tmp_path / "customers", ledger=ledger, operations=app.state.command_center_operations_service,
        state_service=RejectingStateService(),
    )
    app.state.granola_retrieval_service = GranolaRetrievalService(
        transport=transport, adapter=ManualGranolaImportAdapter(), ledger=ledger, job_service=jobs,
    )
    app.include_router(router)
    return TestClient(app), transport


def test_routes_check_list_retrieve_then_review_and_associate(tmp_path) -> None:
    client, transport = _client(tmp_path)
    adapters = client.get("/api/command-center/adapters").json()["adapters"]
    assert all(a["unattended_discovery"] is False for a in adapters)
    assert client.get("/api/command-center/granola/connection").json()["checked"] is False

    listed = client.post("/api/command-center/granola/meetings/list", json={})
    assert listed.status_code == 409 and listed.json()["detail"]["code"] == "not_checked"
    assert client.post("/api/command-center/granola/connection/check", json={}).json()["connected"] is True

    transport.listings["this_week"] = {"meetings": [_listed(MID, "Synthetic listed")]}
    listed = client.post("/api/command-center/granola/meetings/list", json={"time_range": "this_week"})
    assert listed.status_code == 200 and SECRET not in listed.text and listed.json()["meetings"][0]["title"] == "Synthetic listed"
    assert client.post("/api/command-center/granola/meetings/list", json={"time_range": "this_week", "extra": 1}).status_code == 422

    too_many = client.post("/api/command-center/granola/retrievals", json={"meeting_ids": [_uuid(n) for n in range(MAX_SELECTION + 1)]})
    assert too_many.status_code == 422

    _prime(transport, MID)
    started = client.post("/api/command-center/granola/retrievals", json={"meeting_ids": [MID]})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/command-center/granola/retrievals/{job_id}").json()
        if job["status"] != "running":
            break
    assert job["ok"] and job["counts"] == {"imported": 1} and SECRET not in json.dumps(job)
    assert client.get("/api/command-center/granola/retrievals/nope").status_code == 404

    source_id = job["results"][0]["source_id"]
    queue = client.get("/api/command-center/sources/unprocessed").json()
    assert [s["source_id"] for s in queue["sources"]] == [source_id]
    assert SECRET not in json.dumps(queue)
    review = client.get(f"/api/command-center/sources/{source_id}/review")
    assert review.status_code == 200 and SECRET not in review.text
    assert review.json()["source"]["revisions"][-1]["trigger"] == "user_triggered_retrieval"
    associated = client.put(
        f"/api/command-center/sources/{source_id}/association",
        json={"account": "Acme", "opportunity_slug": "expansion", "reason": "Confirmed by SE"},
    )
    assert associated.status_code == 200, associated.text
    assert associated.json()["association"]["state"] == "associated"


# ------------------------------------------------------------------ live-check script (fake-backed)


def _live_check_module():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "granola_live_check.py"
    spec = importlib.util.spec_from_file_location("granola_live_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_check_args(module, *extra: str):
    return module._args(["--date", "2026-09-24", "--marker", MARKER, *extra])


@pytest.fixture(autouse=True)
def _fake_claude_json(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("claude") / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"granola": _GRANOLA}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_USER_CONFIG_JSON", str(path))
    return path


MARKER = "Title-Word-" + SECRET  # a real title fragment: must never be printed


def _live_check_transport(marked: int = 1) -> FakeGranolaRetrievalTransport:
    transport = FakeGranolaRetrievalTransport()
    rows = [_listed(_uuid(1), title="Other synthetic " + SECRET)]
    for n in range(marked):
        rows.append(_listed(_uuid(10 + n), title=f"Solo {MARKER} {n}"))
    transport.listings["custom"] = rows
    _prime(transport, *[_uuid(10 + n) for n in range(marked)])
    return transport


def test_live_check_script_reads_top_level_job_fields_and_passes_on_imported_then_known(capsys) -> None:
    module = _live_check_module()
    transport = _live_check_transport()
    report = asyncio.run(
        module.run(_live_check_args(module), transport_factory=lambda _a: transport, version_probe=lambda: "2.1.259 (Claude Code)")
    )
    assert report["verdict"] == {"ok": True, "failures": []}
    assert report["steps"]["claude_version"]["sufficient"] is True
    assert report["steps"]["mcp_scope"] == {"configured": True, "scope": "user", "type": "http", "error_code": None}
    assert report["steps"]["connection_check"]["connected"] is True
    assert report["steps"]["list"]["marked_matches"] == 1
    assert report["steps"]["first_retrieval"]["counts"] == {"imported": 1}
    assert [row["outcome"] for row in report["steps"]["first_retrieval"]["outcomes"]] == ["imported"]
    assert report["steps"]["second_retrieval"]["counts"] == {"already_known": 1}
    assert [row["outcome"] for row in report["steps"]["second_retrieval"]["outcomes"]] == ["already_known"]
    printed = json.dumps(report)
    assert SECRET not in printed and "ws-synthetic" not in printed and _uuid(10) not in printed
    assert "Other synthetic" not in printed and "transcript text" not in printed
    assert "Title-Word" not in printed and "marker" not in [k for k in report if k != "marker_given"]
    assert report["marker_given"] is True


def test_live_check_script_fails_verdict_on_old_claude_no_marker_or_unexpected_outcome() -> None:
    module = _live_check_module()
    args = _live_check_args(module)

    old = asyncio.run(module.run(args, transport_factory=lambda _a: _live_check_transport(), version_probe=lambda: "2.1.100"))
    assert old["verdict"]["ok"] is False and "older than minimum" in old["verdict"]["failures"][0]
    assert "connection_check" not in old["steps"]

    missing = asyncio.run(module.run(args, transport_factory=lambda _a: _live_check_transport(), version_probe=lambda: None))
    assert missing["verdict"]["ok"] is False and missing["steps"]["claude_version"]["found"] is False

    none_marked = asyncio.run(
        module.run(args, transport_factory=lambda _a: _live_check_transport(marked=0), version_probe=lambda: "2.1.300")
    )
    assert none_marked["verdict"]["ok"] is False and "found 0" in none_marked["verdict"]["failures"][0]

    two_marked = asyncio.run(
        module.run(args, transport_factory=lambda _a: _live_check_transport(marked=2), version_probe=lambda: "2.1.300")
    )
    assert two_marked["verdict"]["ok"] is False and "found 2" in two_marked["verdict"]["failures"][0]

    pending = _live_check_transport()
    pending.transcripts.clear()
    pending.meetings[_uuid(10)] = _detail(_uuid(10), summary=None)
    report = asyncio.run(module.run(args, transport_factory=lambda _a: pending, version_probe=lambda: "2.1.300"))
    assert report["verdict"]["ok"] is False
    assert report["steps"]["first_retrieval"]["outcomes"][0]["outcome"] == "pending_content"
    assert "expected outcomes ['imported']" in report["verdict"]["failures"][0]

    unauth = _live_check_transport()
    unauth.fail("get_account_info", "*", GranolaRelayError("tool_auth_required", retryable=False))
    report = asyncio.run(module.run(args, transport_factory=lambda _a: unauth, version_probe=lambda: "2.1.300"))
    assert report["verdict"]["ok"] is False
    assert report["steps"]["connection_check"] == {
        "connected": False,
        "error_code": "tool_auth_required",
        "retryable": False,
        "workspace_guarantee": None,
        "scope_labels_recognized": 0,
        "scope_labels_hidden": None,
    }


def test_live_check_script_requires_marker_with_date_and_prints_scope_counts_only() -> None:
    module = _live_check_module()
    with pytest.raises(SystemExit):
        module._args(["--date", "2026-09-24"])
    module._args(["--connection-only"])

    transport = FakeGranolaRetrievalTransport()
    transport.account = {
        "email": "person@example.invalid",
        "note_access_scope": ["personal", "Bearer " + SECRET, "person@example.invalid", "https://granola.example/" + SECRET],
    }
    report = asyncio.run(
        module.run(module._args(["--connection-only"]), transport_factory=lambda _a: transport, version_probe=lambda: "2.1.300")
    )
    assert report["verdict"]["ok"] is True
    assert report["steps"]["connection_check"]["scope_labels_recognized"] == 1
    assert report["steps"]["connection_check"]["scope_labels_hidden"] == 3
    printed = json.dumps(report)
    for needle in (SECRET, "person", "example", "Bearer", "personal", "note_access_scope"):
        assert needle not in printed


def test_live_check_script_probes_scope_before_spawning_and_accepts_trusted_project(
    tmp_path: Path, monkeypatch, _fake_claude_json: Path, capsys
) -> None:
    module = _live_check_module()
    original = tmp_path / "original-checkout"
    original.mkdir()
    _fake_claude_json.write_text(
        json.dumps({"projects": {str(original): {"mcpServers": {"granola": _GRANOLA}}}}), encoding="utf-8"
    )

    def must_not_spawn(_args):
        raise AssertionError("transport must not be created when the server is not configured")

    # Sibling worktree without --claude-project: fail closed before any subprocess, with a hint.
    report = asyncio.run(
        module.run(_live_check_args(module), transport_factory=must_not_spawn, version_probe=lambda: "2.1.282")
    )
    assert report["verdict"]["ok"] is False
    assert report["steps"]["mcp_scope"]["configured"] is False
    assert "connection_check" not in report["steps"]
    assert "--claude-project" in report["verdict"]["failures"][0]

    # Naming the original checkout finds the local-scope server and the full check proceeds.
    args = _live_check_args(module, "--claude-project", str(original))
    transport = _live_check_transport()
    report = asyncio.run(module.run(args, transport_factory=lambda _a: transport, version_probe=lambda: "2.1.282"))
    assert report["verdict"] == {"ok": True, "failures": []}
    assert report["steps"]["mcp_scope"] == {"configured": True, "scope": "local", "type": "http", "error_code": None}
    assert report["trusted_project_count"] == 2
    printed = json.dumps(report)
    assert str(original) not in printed and "mcp.granola.ai" not in printed

    # --probe-only prints only the preflight steps and exits 0 when configured.
    probe_args = module._args(["--probe-only", "--claude-project", str(original)])
    report = asyncio.run(module.run(probe_args, transport_factory=must_not_spawn, version_probe=lambda: "2.1.282"))
    assert report["verdict"]["ok"] is True
    assert set(report["steps"]) == {"claude_version", "mcp_scope"}
    with pytest.raises(SystemExit):
        module._args([])


def test_account_shape_report_is_value_free_and_bounded() -> None:
    email = "person@example.invalid"
    raw = {
        "workspaces": [
            {"id": _uuid(7), "display_name": "Acme " + SECRET, "url": "https://granola.example/" + SECRET},
            {"id": _uuid(8), "display_name": SECRET + "-two"},
        ],
        "user": {"email": email, "name": SECRET, SECRET + "-freeform-key": {"token": "Bearer " + SECRET}},
        "note_access_scope": ["personal"],
        "created_at": "2026-09-24T10:00:00Z",
        "count": 2,
        "active": True,
        "nested": {"a": {"b": {"c": {"d": {"e": {"f": SECRET}}}}}},
    }
    report = account_shape_report(raw)
    printed = json.dumps(report)
    for needle in (SECRET, email, "person", "granola.example", _uuid(7), _uuid(8), "Acme", "Bearer", "freeform"):
        assert needle not in printed
    assert report["top_kind"] == "object" and report["looks_like_error_envelope"] is False
    assert "identity_source" not in report
    assert report["candidate_id_paths"] == ["workspaces[0].id", "workspaces[1].id"]
    shape = report["shape"]
    assert shape["key_count"] == 7 and shape["other_keys"] == 1 and "nested" not in shape["keys"]
    assert shape["keys"]["workspaces"]["length"] == 2
    ws = shape["keys"]["workspaces"]["items"][0]["keys"]
    assert ws["id"] == {"kind": "string", "class": "uuid", "length": "<=64"}
    assert ws["display_name"]["class"] == "text" and ws["url"]["class"] == "url"
    user = shape["keys"]["user"]
    assert user["keys"]["email"]["class"] == "email" and user["other_keys"] == 1 and len(user["keys"]) == 2
    assert shape["keys"]["created_at"]["class"] == "datetime"
    assert shape["keys"]["count"] == {"kind": "number"} and shape["keys"]["active"] == {"kind": "bool"}

    # Error envelopes and non-objects are flagged, never echoed.
    envelope = account_shape_report({"error": SECRET, "message": SECRET, "code": 7})
    assert envelope["looks_like_error_envelope"] is True and SECRET not in json.dumps(envelope)
    assert account_shape_report([{"id": _uuid(1)}])["top_kind"] == "array"
    assert account_shape_report(SECRET)["shape"]["class"] == "token" and SECRET not in json.dumps(account_shape_report(SECRET))
    deep = {"data": {"data": {"data": {"data": {"data": {"data": {"id": SECRET}}}}}}}
    assert SECRET not in json.dumps(account_shape_report(deep))


def test_live_check_account_shape_and_connection_only_modes(capsys) -> None:
    module = _live_check_module()
    with pytest.raises(SystemExit):
        module._args(["--account-shape", "--date", "2026-09-24"])
    with pytest.raises(SystemExit):
        module._args(["--account-shape", "--probe-only"])

    # The observed live shape (no documented workspace id): probe reports the shape without values.
    transport = _live_check_transport()
    transport.account = {"workspaces": [{"id": _uuid(7), "display_name": SECRET}], "user": {"email": "a@b.invalid"}}
    report = asyncio.run(
        module.run(module._args(["--account-shape"]), transport_factory=lambda _a: transport, version_probe=lambda: "2.1.282")
    )
    assert report["verdict"] == {"ok": True, "failures": []}
    step = report["steps"]["account_shape"]
    assert step["ok"] is True and step["candidate_id_paths"] == ["workspaces[0].id"]
    assert transport.calls == [("get_account_info", {})]
    printed = json.dumps(report)
    assert SECRET not in printed and _uuid(7) not in printed and "a@b" not in printed
    assert "connection_check" not in report["steps"]

    # The same observed shape passes the connection-only gate: no identity is pinned or claimed.
    transport = _live_check_transport()
    transport.account = {"email": "a@b.invalid", "name": SECRET}
    report = asyncio.run(
        module.run(module._args(["--connection-only"]), transport_factory=lambda _a: transport, version_probe=lambda: "2.1.282")
    )
    assert report["verdict"] == {"ok": True, "failures": []}
    assert report["steps"]["connection_check"]["connected"] is True
    assert report["steps"]["connection_check"]["workspace_guarantee"] is False
    printed = json.dumps(report)
    assert "a@b" not in printed and SECRET not in printed and "workspace_digest" not in printed

    # Tool error → safe code, no shape; fixture shape → connection-only passes without listing.
    failing = _live_check_transport()
    failing.fail("get_account_info", "*", GranolaRelayError("tool_auth_required", retryable=False))
    report = asyncio.run(
        module.run(module._args(["--account-shape"]), transport_factory=lambda _a: failing, version_probe=lambda: "2.1.282")
    )
    assert report["verdict"]["ok"] is False
    assert report["steps"]["account_shape"] == {"ok": False, "error_code": "tool_auth_required", "retryable": False}

    good = _live_check_transport()
    report = asyncio.run(
        module.run(module._args(["--connection-only"]), transport_factory=lambda _a: good, version_probe=lambda: "2.1.282")
    )
    assert report["verdict"] == {"ok": True, "failures": []}
    assert report["steps"]["connection_check"]["connected"] is True and "list" not in report["steps"]
    assert [call[0] for call in good.calls] == ["get_account_info"]


def test_live_check_list_shape_mode_reports_value_free_shape_only(capsys) -> None:
    module = _live_check_module()
    with pytest.raises(SystemExit):
        module._args(["--list-shape"])
    with pytest.raises(SystemExit):
        module._args(["--list-shape", "--date", "2026-09-24", "--marker", "x"])
    args = module._args(["--list-shape", "--date", "2026-09-24"])
    xml = f'<meetings count="2" from="2026-09-24" to="2026-09-24"><meeting id="{_uuid(7)}"><title>{SECRET}</title></meeting></meetings>'
    transport = FakeGranolaRetrievalTransport()
    transport.fail(
        "list_meetings",
        "custom",
        GranolaRelayError("relay_unparseable_result", retryable=True, shape=result_shape_report([{"type": "text", "text": xml}])),
    )
    report = asyncio.run(module.run(args, transport_factory=lambda _a: transport, version_probe=lambda: "2.1.282 (Claude Code)"))
    step = report["steps"]["list_shape"]
    assert step["parsed"] is False and step["error_code"] == "relay_unparseable_result"
    assert step["raw_result_shape"]["text_blocks"][0]["classification"] == "xml_like"
    assert step["raw_result_shape"]["text_blocks"][0]["root_attribute_names"] == ["count", "from", "to"]
    assert report["verdict"]["ok"] is False and report["marker_given"] is False
    printed = json.dumps(report)
    assert SECRET not in printed and _uuid(7) not in printed and '"2"' not in printed
    assert "2026-09-24" in printed  # only the user-supplied --date itself
    assert transport.calls == [("list_meetings", {"time_range": "custom", "custom_start": "2026-09-24", "custom_end": "2026-09-24"})]

    parsed_transport = FakeGranolaRetrievalTransport()
    parsed_transport.listings["custom"] = [_listed(_uuid(8), title="Solo " + SECRET)]
    ok = asyncio.run(module.run(args, transport_factory=lambda _a: parsed_transport, version_probe=lambda: "2.1.282 (Claude Code)"))
    assert ok["steps"]["list_shape"]["parsed"] is True and ok["verdict"]["ok"] is True
    assert SECRET not in json.dumps(ok) and _uuid(8) not in json.dumps(ok)


# ---------------------------------------------------------------- XML tool results (live-observed list shape)

_UUID_A = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
_UUID_B = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"

_LIST_XML = f"""<some_wrapper count="3" from="Sep 24, 2026" to="Sep 24, 2026">
  <meeting id="{_UUID_A}" title="Synthetic &amp; friends" date="Sep 24, 2026 9:00 AM" url="https://notes.granola.ai/d/{_UUID_A}" captured_by_me="true" listed_as_participant="true" is_workspace_visible="false">
    <known_participants>Alice Example, bob@example.test</known_participants>
  </meeting>
  <meeting id="{_UUID_B}" title="Second">
    <known_participants><participant name="Carol" email="carol@example.test"/></known_participants>
  </meeting>
  <meeting title="no id at all"><known_participants/></meeting>
  <query_echo>ignored sibling</query_echo>
</some_wrapper>"""


def test_relay_reads_observed_xml_list_shape_into_json_equivalent_rows() -> None:
    parsed = extract_tool_result(_result_stream([{"type": "text", "text": _LIST_XML}]), tool="list_meetings")
    assert parsed["count"] == 3 and parsed["from"] == "Sep 24, 2026" and parsed["to"] == "Sep 24, 2026"
    assert parsed["rejected_rows"] == 1 and parsed["ignored_nodes"] == 1
    assert [m["id"] for m in parsed["meetings"]] == [_UUID_A, _UUID_B]
    first = parsed["meetings"][0]
    assert first["title"] == "Synthetic & friends" and first["captured_by_me"] == "true"
    assert first["known_participants"] == [{"name": "Alice Example"}, {"name": "bob@example.test"}]
    assert parsed["meetings"][1]["known_participants"] == [{"name": "Carol", "email": "carol@example.test"}]
    # the same shape report that diagnosed the live failure now also names the record attributes
    shape = result_shape_report([{"type": "text", "text": _LIST_XML}])["text_blocks"][0]
    assert shape["classification"] == "xml_like" and shape["root_tag"] == "other"
    assert shape["inner_tag_counts"] == {"known_participants": 3, "meeting": 3, "other": 1, "participant": 1}
    assert "id" in shape["inner_attribute_names"] and "title" in shape["inner_attribute_names"]
    assert "Synthetic" not in json.dumps(shape) and _UUID_A not in json.dumps(shape)


def test_relay_reads_xml_detail_and_transcript_records_with_child_elements() -> None:
    detail = f"""<results><meeting><id>{_UUID_A}</id><title>Synthetic</title><date>Sep 24, 2026</date>
      <url>https://notes.granola.ai/d/{_UUID_A}</url><summary>Two lines
      of notes</summary><known_participants><participant><name>Alice</name></participant></known_participants>
    </meeting></results>"""
    rows = extract_tool_result(_result_stream([{"type": "text", "text": detail}]), tool="list_meetings")["meetings"]
    assert rows == [{
        "id": _UUID_A, "title": "Synthetic", "date": "Sep 24, 2026", "url": f"https://notes.granola.ai/d/{_UUID_A}",
        "summary": "Two lines\n      of notes", "known_participants": [{"name": "Alice"}],
    }]
    transcript = f"""<transcript_result id="{_UUID_A}" title="Synthetic" created_at="2026-09-24T16:00:00Z">
      <transcript>Hello &lt;world&gt;</transcript>
      <recording_context><description>desk test</description><recorder><name>Granola</name></recorder>
        <microphone_sharing>unknown</microphone_sharing></recording_context>
    </transcript_result>"""
    parsed = extract_tool_result(_result_stream([{"type": "text", "text": transcript}]), tool="list_meetings")
    assert parsed["id"] == _UUID_A and parsed["transcript"] == "Hello <world>"
    assert parsed["recording_context"] == {"description": "desk test", "recorder": {"name": "Granola"}, "microphone_sharing": "unknown"}
    assert parsed["ignored_nodes"] == 0


@pytest.mark.parametrize(
    ("xml", "issue"),
    [
        ('<!DOCTYPE x [<!ENTITY e "boom">]><r><meeting id="x">&e;</meeting></r>', "dtd_rejected"),
        ("<r><meeting id='x'></r>", "malformed_xml"),
        ("<a><b><c><d><e><f><g/></f></e></d></c></b></a>", "too_deep"),
        (f'<r><meeting id="{_UUID_A}"><id>{_UUID_A}</id></meeting></r>', "ambiguous_field"),
        (f'<r><meeting id="{_UUID_A}">stray text<known_participants/></meeting></r>', "mixed_content"),
    ],
)
def test_relay_rejects_unsafe_or_ambiguous_xml_with_value_free_issue(xml: str, issue: str) -> None:
    with pytest.raises(GranolaRelayError) as info:
        extract_tool_result(_result_stream([{"type": "text", "text": xml}]), tool="list_meetings")
    assert info.value.code == "relay_unparseable_result" and info.value.shape["xml_issue"] == issue
    assert "boom" not in json.dumps(info.value.shape) and "stray" not in json.dumps(info.value.shape)


def test_service_lists_and_retrieves_from_xml_results_exactly_like_json(tmp_path: Path) -> None:
    class XmlTransport(FakeGranolaRetrievalTransport):
        async def call(self, tool, arguments):
            self.calls.append((tool, dict(arguments)))
            if tool == "get_account_info":
                return self.account
            if tool == "list_meetings":
                return extract_tool_result(_result_stream([{"type": "text", "text": _LIST_XML}]), tool="list_meetings")
            if tool == "get_meetings":
                text = f'<r><meeting id="{_UUID_A}" title="Synthetic" date="Sep 24, 2026"><summary>notes</summary></meeting></r>'
                return extract_tool_result(_result_stream([{"type": "text", "text": text}]), tool="list_meetings")
            text = f'<t id="{_UUID_A}" created_at="2026-09-24T16:00:00Z"><transcript>hello</transcript></t>'
            return extract_tool_result(_result_stream([{"type": "text", "text": text}]), tool="list_meetings")

    service, fake, _ledger, _jobs = _service(tmp_path)
    transport = XmlTransport()
    transport.account = fake.account
    service._transport = transport
    asyncio.run(service.check_connection())
    listing = asyncio.run(service.list_meetings(time_range="custom", custom_start=_date(2026, 9, 24), custom_end=_date(2026, 9, 24)))
    assert listing["shown"] == 2 and listing["rejected"] == 1
    assert listing["meetings"][0] == {**listing["meetings"][0], "participant_count": 2, "captured_by_me": True}
    result = asyncio.run(service._run([_UUID_A]))
    assert result["counts"] == {"imported": 1}
    assert asyncio.run(service._run([_UUID_A]))["counts"] == {"already_known": 1}
