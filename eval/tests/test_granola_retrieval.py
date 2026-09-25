"""User-triggered Granola retrieval: relay parsing/command, service outcomes, and HTTP boundary.

Everything here runs against the scripted `FakeGranolaRetrievalTransport`. No Claude Code
process, no Granola MCP, and no credential is touched; the live connection remains a
Gary-local step recorded in the PR.
"""
from __future__ import annotations

import asyncio
import json
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


def test_relay_command_is_restricted_to_one_allowlisted_tool() -> None:
    relay = ClaudeCodeMcpRelay(model="synthetic-model", forbidden_roots=[])
    cmd = relay.command("list_meetings", executable="/usr/bin/claude")
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__granola__list_meetings"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd and "--disable-slash-commands" in cmd
    assert "dontAsk" in cmd and "--output-format" in cmd
    with pytest.raises(GranolaRelayError) as exc:
        relay.command("query_granola_meetings")  # type: ignore[arg-type]
    assert exc.value.code == "relay_wrong_tool"


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


@pytest.mark.asyncio
async def test_relay_without_claude_executable_reports_runtime_unavailable(tmp_path) -> None:
    relay = ClaudeCodeMcpRelay(model="synthetic-model", forbidden_roots=[], executable=str(tmp_path / "missing-claude"))
    with pytest.raises(GranolaRelayError) as exc:
        await relay.call("get_account_info", {})
    assert exc.value.code == "runtime_unavailable"


# ------------------------------------------------------------ connection


@pytest.mark.asyncio
async def test_connection_check_pins_workspace_and_flags_mismatch(tmp_path) -> None:
    service, transport, _ledger, _jobs = _service(tmp_path)
    assert service.connection()["pinned_workspace"] is None
    first = await service.check_connection()
    assert first["connected"] and first["workspace"]["id"] == "ws-synthetic" and not first["workspace_mismatch"]
    pin_file = tmp_path / "customers" / ".command-center" / "granola-connection.json"
    assert pin_file.exists() and (pin_file.stat().st_mode & 0o077) == 0
    assert "token" not in pin_file.read_text(encoding="utf-8").lower()

    transport.account = {"workspace": {"id": "ws-other", "display_name": "Other"}, "note_access_scope": ["personal"]}
    second = await service.check_connection()
    assert second["connected"] and second["workspace_mismatch"]
    with pytest.raises(GranolaRetrievalError) as exc:
        await service.list_meetings(time_range="this_week")
    assert exc.value.code == "wrong_workspace" and exc.value.status == 409
    repinned = await service.check_connection(repin=True)
    assert not repinned["workspace_mismatch"]
    assert service.connection()["pinned_workspace"]["workspace_id"] == "ws-other"


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
async def test_retrieval_wrong_workspace_fails_before_any_import_and_job_is_body_free(tmp_path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_connection()
    _prime(transport, MID)
    transport.account = {"workspace": {"id": "ws-other", "display_name": "Other"}}
    job = await _retrieve(service, jobs, [MID])
    assert job["status"] == "error" and job["error_code"] == "wrong_workspace"
    assert ledger.list_sources()["total"] == 0
    assert SECRET not in json.dumps(job)
    assert set(job.keys()) >= {"kind", "status", "error_code"} and "raw" not in json.dumps(job).lower()


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
