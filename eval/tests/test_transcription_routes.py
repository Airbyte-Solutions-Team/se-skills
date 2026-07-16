"""Deterministic route-level tests for the live-transcription API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import webapp.app as app
from services.transcription_service import TranscriptionService


def _svc(tmp_path):
    customers = tmp_path / "customers"
    customers.mkdir(parents=True)
    return TranscriptionService(
        customers_dir=customers,
        workspace=tmp_path,
        safe_name=lambda n: n,
        titlecase=lambda n: n,
        whisper_model="tiny",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    app.app.state.transcription_service = svc
    return TestClient(app.app)


def test_list_transcripts_empty(client):
    resp = client.get("/api/transcripts?account=Acme")
    assert resp.status_code == 200
    assert resp.json() == {"transcripts": []}


def test_load_transcript_returns_labels(client, tmp_path):
    tdir = tmp_path / "customers" / "_transcripts"
    tdir.mkdir(parents=True)
    text = (
        "# Live transcript — Acme — July 14, 2026 12:00\n"
        "# mic-label: Gary\n"
        "# call-label: Customer\n"
        "\n"
        "[12:00:00] Gary: hello\n"
    )
    (tdir / "Acme-07.14.26.txt").write_text(text)
    resp = client.get("/api/transcripts/Acme-07.14.26.txt?account=Acme")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mic_label"] == "Gary"
    assert data["call_label"] == "Customer"
    assert len(data["segments"]) == 1


def test_load_transcript_rejects_cross_account(client, tmp_path):
    tdir = tmp_path / "customers" / "_transcripts"
    tdir.mkdir(parents=True)
    (tdir / "Other-07.14.26.txt").write_text("x")
    resp = client.get("/api/transcripts/Other-07.14.26.txt?account=Acme")
    assert resp.status_code == 403


def test_load_transcript_not_found(client):
    resp = client.get("/api/transcripts/Acme-99.99.99.txt?account=Acme")
    assert resp.status_code == 404


def test_active_session_204_when_none(client):
    resp = client.get("/api/transcribe/active?account=Acme")
    assert resp.status_code == 204
    assert resp.content == b""


def test_stop_unknown_session(client):
    resp = client.post("/api/transcribe/nope/stop")
    assert resp.status_code == 404


def test_ask_empty_question(client):
    resp = client.post("/api/transcribe/file/ask", json={"question": "   "})
    assert resp.status_code == 400


def test_ask_file_requires_account_and_name(client):
    resp = client.post(
        "/api/transcribe/file/ask",
        json={"question": "What did they say?", "account": "Acme"},
    )
    assert resp.status_code == 400
