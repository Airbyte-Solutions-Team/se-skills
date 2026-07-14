"""Deterministic tests for durable job/live-session state and live-transcribe speaker labels.

These tests call the persistence layer and FastAPI route functions directly so
they do not need an HTTP client or a running event loop. They verify that:
- job state is persisted and running jobs are marked lost on restart;
- live sessions are persisted, loaded as recovered, and deleted after stop;
- saved transcripts carry mic/call labels and can be parsed back correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import webapp.app as app
from webapp import persistence
from webapp.app import (
    LiveSession,
    StartLive,
    _parse_saved_transcript,
    api_load_transcript,
    api_transcribe_active,
    api_transcribe_stop,
)


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


def test_livesession_roundtrip_and_transcript_labels(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "WORKSPACE", tmp_path)
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


def test_api_transcribe_active_returns_labels_and_recovered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "WORKSPACE", tmp_path)
    monkeypatch.setattr(app, "CUSTOMERS_DIR", tmp_path / "customers")
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=None,
        mic_label="Gary", call_label="Call", opportunity="Big Deal",
        recovered=True,
        segments=[{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
        session_id="recovered1",
    )
    monkeypatch.setattr(app, "SESSIONS", {"recovered1": sess})
    resp = api_transcribe_active(account="Acme", opp_slug="op1")
    assert resp["mic_label"] == "Gary"
    assert resp["call_label"] == "Call"
    assert resp["recovered"] is True
    assert resp["segments"] == [{"t": "12:00:00", "speaker": "Gary", "text": "hello"}]


def test_api_transcribe_stop_saves_transcript_and_deletes_session_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "WORKSPACE", tmp_path)
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    started = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    sess = LiveSession(
        account="Acme", opp_slug="op1", mic_device=0, call_device=1,
        mic_label="Gary", call_label="Customer", opportunity="Big Deal",
        recovered=True,
        segments=[{"t": "12:00:00", "speaker": "Gary", "text": "hello"}],
        started_at=started,
        session_id="stop1",
    )
    monkeypatch.setattr(app, "SESSIONS", {"stop1": sess})
    persistence.save_session(sess.to_dict(), tmp_path)
    assert (tmp_path / ".state" / "sessions" / "stop1.json").exists()

    resp = api_transcribe_stop("stop1")
    assert resp["segments"] == 1

    transcript_files = list((customers / "_transcripts").glob("Acme-*.txt"))
    assert len(transcript_files) == 1
    content = transcript_files[0].read_text()
    assert "# mic-label: Gary" in content
    assert "[12:00:00] Gary: hello" in content
    assert not (tmp_path / ".state" / "sessions" / "stop1.json").exists()


def test_api_load_transcript_returns_labels(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "CUSTOMERS_DIR", tmp_path)
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

    resp = api_load_transcript(name="Acme-07.14.26.txt", account="Acme")
    assert resp["mic_label"] == "Gary"
    assert resp["call_label"] == "Customer"
    assert len(resp["segments"]) == 1
