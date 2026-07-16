"""Deterministic tests for durable job/live-session state and live-transcribe speaker labels.

These tests call the persistence layer and service methods directly so they do
not need an HTTP client or a running event loop. They verify that:
- job state is persisted and running jobs are marked lost on restart;
- live sessions are persisted, loaded as recovered, and deleted after stop;
- saved transcripts carry mic/call labels and can be parsed back correctly;
- persistence write failures return False, log safely, and surface UI warnings.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timezone

import pytest

import persistence
import webapp.app as app
from services.transcription_service import (
    LiveSession,
    TranscriptionService,
    _parse_saved_transcript,
)
from webapp.routes.transcription import AskLive, StartLive


def test_persistence_roundtrips_jobs_and_marks_running_as_lost(tmp_path) -> None:
    jobs = {
        "abc123": {
            "status": "done", "ok": True, "stdout": "hi", "stderr": "",
            "skill": "next-move", "account": "Acme",
            "sig": ("a", "b", "c", 1),
        },
        "def456": {
            "status": "running", "ok": None, "stdout": "", "stderr": "",
            "skill": "poc-plan", "account": "Acme",
            "started_at": 1_700_000_000.0,  # genuinely started → recoverable as error
            "sig": ("a", "b", "c", 2),
        },
    }
    persistence.save_jobs(jobs, tmp_path)
    loaded = persistence.load_jobs(tmp_path)
    assert loaded["abc123"]["status"] == "done"
    assert loaded["def456"]["status"] == "error"
    assert loaded["def456"]["ok"] is False
    assert "re-run" in loaded["def456"]["stderr"].lower()
    assert loaded["def456"]["sig"] == ("a", "b", "c", 2)
    assert (tmp_path / ".state" / "jobs.json").exists()


def test_persistence_drops_never_started_running_jobs(tmp_path) -> None:
    # An orphaned record marked "running" but with no started_at never actually
    # launched (crash/half-write). It must be dropped, not resurrected as a phantom
    # failure that pollutes the "Recent failures" dashboard.
    jobs = {
        "good": {
            "status": "running", "ok": None, "stdout": "", "stderr": "",
            "skill": "deal-assessment", "account": "Acme",
            "started_at": 1_700_000_000.0, "sig": ("a", 1),
        },
        "orphan": {
            "status": "running", "ok": None, "stdout": "", "stderr": "",
            "skill": "deal-assessment", "account": "11880",
            "sig": ("a", 2),  # no started_at
        },
    }
    persistence.save_jobs(jobs, tmp_path)
    loaded = persistence.load_jobs(tmp_path)
    assert "orphan" not in loaded
    assert loaded["good"]["status"] == "error"  # genuinely-started job still recovered


def test_persistence_filters_transient_fields_from_jobs_snapshot(tmp_path) -> None:
    jobs = {
        "j1": {
            "status": "running", "ok": None, "stdout": "", "stderr": "",
            "skill": "next-move", "account": "Acme", "sig": ("a", "b"),
            "persistence_warning": "should not be saved",
            "task": "<asyncio.Task>",
            "proc": "<subprocess.Popen>",
        },
    }
    persistence.save_jobs(jobs, tmp_path)
    raw = (tmp_path / ".state" / "jobs.json").read_text()
    assert "persistence_warning" not in raw
    assert "<asyncio.Task>" not in raw
    assert "<subprocess.Popen>" not in raw


def test_save_jobs_returns_false_on_write_failure(tmp_path) -> None:
    # A file where a directory is expected causes mkdir to fail with an OSError.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    assert persistence.save_jobs({"j1": {"status": "running"}}, blocked) is False


def test_save_session_returns_false_on_write_failure(tmp_path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    data = {
        "session_id": "s1", "account": "Acme", "mic_device": 0, "call_device": None,
        "labeled": False, "recovered": False, "ended": False,
        "started_at": datetime.now(timezone.utc).timestamp(), "segments": [],
    }
    assert persistence.save_session(data, blocked) is False


def test_delete_session_returns_false_on_failure(tmp_path) -> None:
    d = tmp_path / ".state" / "sessions"
    d.mkdir(parents=True)
    f = d / "s1.json"
    f.write_text("{}")
    # Remove write permission from the directory so unlink fails.
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert persistence.delete_session("s1", tmp_path) is False
    finally:
        os.chmod(d, stat.S_IRWXU)


def test_persistence_roundtrips_sessions(tmp_path) -> None:
    data = {
        "session_id": "s1",
        "account": "Acme",
        "opp_slug": "op1",
        "opportunity": "Big Deal",
        "mic_device": 0,
        "call_device": 1,
        "mic_label": "Gary",
        "call_label": "Customer",
        "labeled": True,
        "recovered": False,
        "ended": False,
        "started_at": datetime.now(timezone.utc).timestamp(),
        "segments": [{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
    }
    persistence.save_session(data, tmp_path)
    loaded = persistence.load_sessions(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["recovered"] is True
    assert loaded[0]["ended"] is True
    assert loaded[0]["mic_label"] == "Gary"
    assert not (tmp_path / ".state" / "sessions" / "s1.json.tmp").exists()


def _svc(tmp_path: object) -> TranscriptionService:
    return TranscriptionService(
        customers_dir=tmp_path / "customers",
        workspace=tmp_path,
        safe_name=lambda n: n,
        titlecase=lambda n: n,
        whisper_model="tiny",
    )


def test_livesession_roundtrip_and_transcript_labels(tmp_path) -> None:
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=1,
        mic_label="Gary", call_label="Customer", opportunity="Big Deal",
        recovered=True,
        segments=[{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
        session_id="sess1",
    )
    text = sess.transcript_text()
    assert "# mic-label: Gary" in text
    assert "# call-label: Customer" in text
    assert "[12:00:00] Gary: hello" in text

    parsed = _parse_saved_transcript(text)
    assert parsed["mic_label"] == "Gary"
    assert parsed["call_label"] == "Customer"
    assert parsed["segments"] == [{"t": "12:00:00", "speaker": "Gary", "text": "hello"}]


def test_livesession_persist_sets_warning_on_failure() -> None:
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=None,
        mic_label="You", call_label="Call", opportunity="Big Deal",
        recovered=True, session_id="s1", persist_fn=lambda _data: False,
    )
    sess._persist()
    assert sess.persistence_warning is not None
    assert "will not survive" in sess.persistence_warning.lower()


def test_parse_saved_transcript_protects_unknown_colon_prefixes() -> None:
    text = (
        "# Live transcript — Acme — July 14, 2026 12:00\n"
        "# mic-label: Gary\n"
        "# call-label: Customer\n"
        "\n"
        "[12:00:00] Gary: hello\n"
        "[12:00:01] Customer: we use: lots of colons\n"
        "[12:00:02] Random prefix: not a speaker\n"
    )
    parsed = _parse_saved_transcript(text)
    assert parsed["mic_label"] == "Gary"
    assert parsed["call_label"] == "Customer"
    segs = {s["speaker"]: s["text"] for s in parsed["segments"]}
    assert segs["Gary"] == "hello"
    assert segs["Customer"] == "we use: lots of colons"
    # "Random prefix" is not a known label, so the colon stays in the body.
    unknown = [s for s in parsed["segments"] if s["speaker"] == ""][0]
    assert unknown["text"] == "Random prefix: not a speaker"


def test_start_live_model_accepts_and_defaults_labels() -> None:
    body = StartLive(account="Acme", mic_device=0)
    assert body.mic_label == "You"
    assert body.call_label == "Call"
    body2 = StartLive(account="Acme", mic_device=0, mic_label="Gary", call_label="Customer")
    assert body2.mic_label == "Gary"
    assert body2.call_label == "Customer"


def test_start_live_model_rejects_long_labels() -> None:
    with pytest.raises(Exception):
        StartLive(account="Acme", mic_device=0, mic_label="x" * 81)


def test_api_job_returns_persistence_warning() -> None:
    app.app.state.job_service.jobs = {"j1": {"status": "running", "persistence_warning": "warn text"}}
    try:
        resp = app.app.state.job_service.get_job("j1")
        assert resp["persistence_warning"] == "warn text"
    finally:
        app.app.state.job_service.jobs = {}


def test_save_jobs_snapshot_warns_and_clears(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app.app.state.job_service, "workspace", tmp_path)
    app.app.state.job_service.jobs = {"j1": {"status": "running"}}
    try:
        # Failure path: returns warning and attaches it to the job.
        monkeypatch.setattr(persistence, "save_jobs", lambda jobs, ws: False)
        warn = asyncio.run(app.app.state.job_service.save_snapshot("j1"))
        assert warn is not None
        assert app.app.state.job_service.jobs["j1"].get("persistence_warning") == warn

        # Success path: clears the warning and returns None.
        monkeypatch.setattr(persistence, "save_jobs", lambda jobs, ws: True)
        cleared = asyncio.run(app.app.state.job_service.save_snapshot("j1"))
        assert cleared is None
        assert "persistence_warning" not in app.app.state.job_service.jobs["j1"]
    finally:
        app.app.state.job_service.jobs = {}


def test_transcription_service_active_returns_labels_and_recovered(tmp_path) -> None:
    svc = _svc(tmp_path)
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=None,
        mic_label="Gary", call_label="Call", opportunity="Big Deal",
        recovered=True,
        segments=[{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
        session_id="recovered1",
    )
    sess.persistence_warning = "transcript persistence failed"
    svc.sessions["recovered1"] = sess
    resp = svc.active_session("Acme", "op1")
    assert resp is not None
    assert resp["mic_label"] == "Gary"
    assert resp["call_label"] == "Call"
    assert resp["recovered"] is True
    assert resp["segments"] == [{"t": "12:00:00", "speaker": "Gary", "text": "hello"}]
    assert resp["persistence_warning"] == "transcript persistence failed"


def test_transcription_service_returns_none_when_no_active_session(tmp_path) -> None:
    svc = _svc(tmp_path)
    assert svc.active_session("Acme") is None


def test_transcription_service_start_returns_persistence_warning(monkeypatch, tmp_path) -> None:
    svc = _svc(tmp_path)
    monkeypatch.setattr(persistence, "save_session", lambda data, ws: False)

    class FakeLiveSession:
        def __init__(self, *args, **kwargs):
            self.session_id = None
            self.labeled = False
            self.mic_label = "You"
            self.call_label = "Call"
            self.persistence_warning = None

        def start(self):
            pass

        def to_dict(self):
            return {"session_id": self.session_id}

    monkeypatch.setattr("services.transcription_service.LiveSession", FakeLiveSession)
    resp = asyncio.run(svc.start_session(account="Acme", opp_slug=None, mic_device=0))
    assert resp["persistence_warning"] == "Live transcript will not survive a server restart because state could not be saved."


def test_transcription_service_stop_saves_transcript_and_deletes_session_file(tmp_path) -> None:
    svc = _svc(tmp_path)
    customers = tmp_path / "customers"
    svc.customers_dir = customers
    started = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=1,
        mic_label="Gary", call_label="Customer", opportunity="Big Deal",
        recovered=True,
        segments=[{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
        started_at=started,
        session_id="stop1",
        persist_fn=lambda data: persistence.save_session(data, tmp_path),
    )
    svc.sessions["stop1"] = sess
    persistence.save_session(sess.to_dict(), tmp_path)
    assert (tmp_path / ".state" / "sessions" / "stop1.json").exists()

    resp = svc.stop_session("stop1")
    assert resp["segments"] == 1

    transcript_files = list((customers / "_transcripts").glob("Acme-*.txt"))
    assert len(transcript_files) == 1
    content = transcript_files[0].read_text()
    assert "# mic-label: Gary" in content
    assert "[12:00:00] Gary: hello" in content
    assert not (tmp_path / ".state" / "sessions" / "stop1.json").exists()


def test_transcription_service_stop_returns_warning_when_delete_fails(monkeypatch, tmp_path) -> None:
    svc = _svc(tmp_path)
    customers = tmp_path / "customers"
    svc.customers_dir = customers
    started = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=None,
        mic_label="You", call_label="Call", opportunity="Big Deal",
        recovered=True,
        segments=[{"t": "12:00:00", "speaker": "You", "text": "hello"}],
        started_at=started,
        session_id="stopfail",
    )
    svc.sessions["stopfail"] = sess
    monkeypatch.setattr(persistence, "delete_session", lambda sid, ws: False)
    resp = svc.stop_session("stopfail")
    assert "persistence_warning" in resp
    assert "could not be removed" in resp["persistence_warning"]


def test_transcription_service_load_transcript_returns_labels(tmp_path) -> None:
    svc = _svc(tmp_path)
    svc.customers_dir = tmp_path
    text = (
        "# Live transcript — Acme — July 14, 2026 12:00\n"
        "# mic-label: Gary\n"
        "# call-label: Customer\n"
        "\n"
        "[12:00:00] Gary: hello\n"
    )
    f = tmp_path / "_transcripts" / "Acme-07.14.26.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)

    resp = svc.load_transcript(account="Acme", name="Acme-07.14.26.txt")
    assert resp["mic_label"] == "Gary"
    assert resp["call_label"] == "Customer"
    assert len(resp["segments"]) == 1


def test_transcription_service_ask_context_rejects_missing_fields(tmp_path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(Exception):
        svc.ask_context(session_id="file", account=None, transcript_name="foo.txt")


def test_transcription_service_recovers_sessions_at_init(tmp_path, monkeypatch) -> None:
    data = {
        "session_id": "rec1",
        "account": "Acme",
        "opp_slug": "op1",
        "opportunity": "Big Deal",
        "mic_device": 0,
        "call_device": None,
        "mic_label": "Gary",
        "call_label": "Call",
        "labeled": False,
        "recovered": False,
        "ended": False,
        "started_at": datetime.now(timezone.utc).timestamp(),
        "segments": [{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
    }
    persistence.save_session(data, tmp_path)
    svc = _svc(tmp_path)
    assert "rec1" in svc.sessions
    assert svc.sessions["rec1"].recovered is True
