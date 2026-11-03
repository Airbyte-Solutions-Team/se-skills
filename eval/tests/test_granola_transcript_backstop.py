"""Deterministic tests for the post-run Granola transcript backstop.

A skill run that reads a Granola transcript must leave it saved in `_transcripts/`
(playbook → "Every pulled transcript is saved"). The backstop makes that hold when
the run didn't: it finds Granola meeting ids the run cited, skips ones already
saved, and fetches + saves the rest through the (fake) relay. No real Claude Code,
MCP server or Granola account is touched.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date
from pathlib import Path

import pytest

from integrations.granola_mcp_relay import FakeGranolaRetrievalTransport, GranolaRelayError
from services.granola_transcript_backstop import (
    GranolaTranscriptBackstop,
    call_date_for,
    cited_granola_meeting_ids,
)
from services.job_service import JobService
from services.transcription_service import TranscriptionService

MEETING = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER = "11111111-2222-3333-4444-555555555555"
META = {"account": "Acme", "opp_slug": "acme-poc", "skill": "follow-up-email", "opportunity": "Acme POC"}


def _transcripts(tmp_path: Path) -> TranscriptionService:
    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    return TranscriptionService(
        customers_dir=customers, workspace=tmp_path, safe_name=lambda n: n, titlecase=lambda n: n,
    )


def _transport(meeting_id: str = MEETING, *, transcript: str | None = "[00:00:01] Speaker A: hello", **meeting) -> FakeGranolaRetrievalTransport:
    fake = FakeGranolaRetrievalTransport()
    fake.meetings[meeting_id] = {"id": meeting_id, "title": "POC Pre-Reqs | Timelines", "date": "Nov 3, 2026", **meeting}
    if transcript is not None:
        fake.transcripts[meeting_id] = {"id": meeting_id, "created_at": "2026-11-03T10:00:00", "transcript": transcript}
    return fake


def _backstop(tmp_path: Path, transport, *, outputs: list[Path] | None = None, enabled: bool = True, **kwargs) -> GranolaTranscriptBackstop:
    return GranolaTranscriptBackstop(
        transport=transport,
        transcripts=_transcripts(tmp_path),
        outputs_since=lambda account, opp, skill, since: list(outputs or []),
        enabled=lambda: enabled,
        today=lambda: date(2026, 11, 3),
        **kwargs,
    )


def _run(backstop: GranolaTranscriptBackstop, stdout: str) -> dict | None:
    return asyncio.run(backstop.after_run(META, 0.0, stdout))


def _tdir(tmp_path: Path) -> Path:
    return tmp_path / "customers" / "_transcripts"


def test_cited_ids_come_only_from_granola_lines() -> None:
    texts = [
        f"- **Granola transcript:** \"POC Pre-Reqs\" (Nov 3, 2026, meeting ID {MEETING.upper()})\n"
        f"- Job {OTHER} finished\n"
        f"- Transcript source: Granola (Granola meeting ID: `{MEETING}`)",
        "Transcript source: Gong (Call ID: 1269453332476333264)",
    ]
    assert cited_granola_meeting_ids(texts) == [MEETING]


def test_saves_cited_unsaved_granola_transcript_with_provenance_header(tmp_path: Path) -> None:
    transport = _transport()
    result = _run(_backstop(tmp_path, transport), f"Transcript source: Granola (Granola meeting ID: {MEETING})")

    assert result == {
        "kind": "granola_transcripts",
        "cited": 1,
        "results": [{"meeting_id": MEETING, "outcome": "saved", "name": "Acme-11.03.26.txt", "date_source": "transcript_created_at"}],
    }
    lines = (_tdir(tmp_path) / "Acme-11.03.26.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"# Source: Granola | {MEETING} | POC Pre-Reqs / Timelines | 2026-11-03"
    assert lines[2] == "[00:00:01] Speaker A: hello"
    assert [tool for tool, _ in transport.calls] == ["get_meetings", "get_meeting_transcript"]


def test_already_saved_meeting_is_not_fetched_again(tmp_path: Path) -> None:
    transport = _transport()
    backstop = _backstop(tmp_path, transport)
    stdout = f"Granola meeting ID: {MEETING}"
    assert _run(backstop, stdout)["results"][0]["outcome"] == "saved"
    calls_after_first = len(transport.calls)

    second = _run(backstop, stdout)
    assert second["results"] == [{"meeting_id": MEETING, "outcome": "already_saved"}]
    assert len(transport.calls) == calls_after_first
    assert sorted(p.name for p in _tdir(tmp_path).iterdir()) == ["Acme-11.03.26.txt"]


def test_meeting_saved_by_the_skill_itself_is_recognized(tmp_path: Path) -> None:
    tdir = _tdir(tmp_path)
    tdir.mkdir(parents=True)
    (tdir / "Acme-11.03.26.txt").write_text(f"# Source: Granola | {MEETING} | POC | 2026-11-03\n\nbody\n", encoding="utf-8")
    transport = _transport()
    result = _run(_backstop(tmp_path, transport), f"Granola meeting ID: {MEETING}")
    assert result["results"] == [{"meeting_id": MEETING, "outcome": "already_saved"}]
    assert transport.calls == []


def test_same_day_transcript_already_saved_is_not_duplicated(tmp_path: Path) -> None:
    """The Gong copy (or a header-less save by the run) means this call is already local."""
    tdir = _tdir(tmp_path)
    tdir.mkdir(parents=True)
    (tdir / "Acme-11.03.26.txt").write_text("[00:00:01] Rep: Gong copy of the same call\n", encoding="utf-8")
    result = _run(
        _backstop(tmp_path, _transport()),
        f"Transcript source: Gong (Granola also had this meeting, Granola meeting ID: {MEETING} — not used)",
    )
    assert result["results"] == [{"meeting_id": MEETING, "outcome": "same_day_exists", "name": "Acme-11.03.26.txt"}]
    assert sorted(p.name for p in tdir.iterdir()) == ["Acme-11.03.26.txt"]
    assert (tdir / "Acme-11.03.26.txt").read_text(encoding="utf-8") == "[00:00:01] Rep: Gong copy of the same call\n"


def test_save_granola_transcript_never_overwrites(tmp_path: Path) -> None:
    svc = _transcripts(tmp_path)
    first = svc.save_granola_transcript("Acme", meeting_id=MEETING, title="Call", call_date=date(2026, 11, 3), text="one")
    second = svc.save_granola_transcript("Acme", meeting_id=OTHER, title="Call", call_date=date(2026, 11, 3), text="two")
    assert (first["name"], second["name"]) == ("Acme-11.03.26.txt", "Acme-11.03.26-v2.txt")
    assert svc.saved_granola_meeting_ids() == {MEETING, OTHER}
    assert svc.transcripts_for_call_date("Acme", date(2026, 11, 3)) == ["Acme-11.03.26-v2.txt", "Acme-11.03.26.txt"]
    assert svc.transcripts_for_call_date("Acme", date(2026, 11, 4)) == []


def test_reads_ids_from_the_runs_output_documents(tmp_path: Path) -> None:
    doc = tmp_path / "email-2026-11-03-Recap.md"
    doc.write_text(f"## Sources\n- **Granola transcript:** \"POC\" (meeting ID {MEETING})\n", encoding="utf-8")
    result = _run(_backstop(tmp_path, _transport(), outputs=[doc]), "Draft saved.")
    assert result["results"][0]["outcome"] == "saved"


def test_nothing_cited_or_disabled_does_nothing(tmp_path: Path) -> None:
    transport = _transport()
    assert _run(_backstop(tmp_path, transport), "Transcript source: Gong (Call ID: 42)") is None
    assert _run(_backstop(tmp_path, transport, enabled=False), f"Granola meeting ID: {MEETING}") is None
    assert transport.calls == []
    assert not _tdir(tmp_path).exists()


def test_relay_failure_is_reported_and_writes_nothing(tmp_path: Path) -> None:
    transport = _transport()
    transport.fail("get_meetings", MEETING, GranolaRelayError("tool_auth_required", retryable=False))
    result = _run(_backstop(tmp_path, transport), f"Granola meeting ID: {MEETING}")
    assert result["results"] == [{"meeting_id": MEETING, "outcome": "failed", "error_code": "tool_auth_required"}]
    assert not _tdir(tmp_path).exists()


@pytest.mark.parametrize(
    ("transport_kwargs", "outcome"),
    [({"transcript": ""}, "pending_content"), ({"transcript": None}, "pending_content")],
)
def test_missing_transcript_text_never_saves_a_summary(tmp_path: Path, transport_kwargs: dict, outcome: str) -> None:
    transport = _transport(**transport_kwargs, summary="AI summary that must not be saved as a transcript")
    result = _run(_backstop(tmp_path, transport), f"Granola meeting ID: {MEETING}")
    assert result["results"] == [{"meeting_id": MEETING, "outcome": outcome}]
    assert not _tdir(tmp_path).exists()


def test_unknown_meeting_and_mismatched_transcript_are_rejected(tmp_path: Path) -> None:
    transport = _transport()
    transport.transcripts[MEETING]["id"] = OTHER
    result = _run(_backstop(tmp_path, transport), f"Granola meeting ID: {MEETING}; Granola meeting ID: {OTHER}")
    assert result["results"] == [
        {"meeting_id": MEETING, "outcome": "rejected", "error_code": "transcript_id_mismatch"},
        {"meeting_id": OTHER, "outcome": "not_found"},
    ]
    assert not _tdir(tmp_path).exists()


def test_per_run_cap_skips_extra_meetings(tmp_path: Path) -> None:
    ids = [f"{n:08d}-0000-0000-0000-000000000000" for n in range(3)]
    transport = FakeGranolaRetrievalTransport()
    for meeting_id in ids:
        transport.meetings[meeting_id] = {"id": meeting_id, "title": "Call"}
        transport.transcripts[meeting_id] = {"id": meeting_id, "created_at": "2026-11-0%dT09:00:00" % (int(meeting_id[7]) + 1), "transcript": "text"}
    result = _run(_backstop(tmp_path, transport, max_meetings=2), " ".join(f"Granola {i}" for i in ids))
    assert [r["outcome"] for r in result["results"]] == ["saved", "saved", "skipped"]
    assert result["results"][2]["error_code"] == "over_run_cap"


def test_call_date_prefers_granola_dates_and_falls_back_to_today() -> None:
    today = date(2026, 11, 3)
    assert call_date_for({"created_at": "2026-10-27T15:00:00"}, {}, today=today) == (date(2026, 10, 27), "transcript_created_at")
    assert call_date_for({}, {"created_at": "2026-10-22T09:00:00"}, today=today) == (date(2026, 10, 22), "meeting_created_at")
    assert call_date_for({}, {"date": "Oct 8, 2026 at 2:00 PM"}, today=today) == (date(2026, 10, 8), "meeting_date")
    assert call_date_for({}, {"date": "sometime"}, today=today) == (today, "save_date")


# --- JobService integration -----------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.pid = 1
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode("utf-8"), b""


def _job_service(tmp_path: Path, records: list[dict]) -> JobService:
    svc = JobService(
        tmp_path, model_for=lambda use: "dummy-model",
        persist_run=lambda account, opp, skill, record: records.append(record),
    )
    svc.jobs = {"j1": {"status": "running", "started_at": time.time(), **META}}
    return svc


@pytest.mark.parametrize("returncode", [0, 1])
def test_job_service_runs_backstop_only_after_successful_runs(tmp_path: Path, monkeypatch, returncode: int) -> None:
    records: list[dict] = []
    svc = _job_service(tmp_path, records)
    calls: list[str] = []

    async def after_run(meta, started_at, stdout):
        calls.append(stdout)
        return {"kind": "granola_transcripts", "cited": 1, "results": [{"meeting_id": MEETING, "outcome": "saved", "name": "Acme-11.03.26.txt"}]}

    svc.after_run = after_run

    async def fake_exec(*args, **kwargs):
        return _FakeProc(returncode, f"Granola meeting ID: {MEETING}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(svc._run_job("j1", "p", META))

    job = svc.get_job("j1")
    assert job["status"] == "done"
    if returncode == 0:
        assert calls == [f"Granola meeting ID: {MEETING}"]
        assert job["transcript_backstop"]["results"][0]["outcome"] == "saved"
        assert records[-1]["transcript_backstop"] == job["transcript_backstop"]
    else:
        assert calls == []
        assert "transcript_backstop" not in job
        assert "transcript_backstop" not in records[-1]


def test_job_service_survives_a_crashing_backstop(tmp_path: Path, monkeypatch) -> None:
    records: list[dict] = []
    svc = _job_service(tmp_path, records)

    async def broken(meta, started_at, stdout):
        raise RuntimeError("boom")

    svc.after_run = broken

    async def fake_exec(*args, **kwargs):
        return _FakeProc(0, "done")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(svc._run_job("j1", "p", META))
    job = svc.get_job("j1")
    assert job["status"] == "done" and job["ok"] is True
    assert "transcript_backstop" not in job
