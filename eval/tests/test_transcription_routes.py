"""Deterministic route-level tests for the live-transcription API."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import webapp.app as app
from services.ask_service import AskService
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


@pytest.fixture
def ask_client(tmp_path, monkeypatch):
    """Like `client`, but with a controllable `AskService` (mocked job launch,
    fixed API key) so deep/force_deep routing is deterministic — the plain
    `client` fixture leaves whatever real `AskService` `webapp/app.py` wired
    at import time, which isn't controllable enough for these assertions."""
    svc = _svc(tmp_path)
    app.app.state.transcription_service = svc
    job_mock = AsyncMock(return_value=("job789", None))
    app.app.state.ask_service = AskService(
        output_service=app.app.state.ask_service.output_service,
        job_service=type("JS", (), {"launch": job_mock})(),
        api_key=lambda member_id=None: "test-key",
        model_for=lambda use: "claude-sonnet-4-6",
    )
    return TestClient(app.app), job_mock


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


def test_ask_force_deep_field_accepted(ask_client, tmp_path):
    test_client, job_mock = ask_client
    tdir = tmp_path / "customers" / "_transcripts"
    tdir.mkdir(parents=True)
    (tdir / "Acme-07.14.26.txt").write_text("[12:00:00] Gary: hello", encoding="utf-8")

    resp = test_client.post("/api/transcribe/file/ask", json={
        "question": "hello",
        "account": "Acme",
        "transcript_name": "Acme-07.14.26.txt",
        "force_deep": True,
    })

    assert resp.status_code == 200
    assert resp.json() == {"mode": "deep", "job_id": "job789"}
    job_mock.assert_awaited_once()
    assert job_mock.call_args.kwargs["skill"] == "live-ask"


def test_ask_resolves_member_id_from_account_owner(ask_client, tmp_path):
    # Spy returns None (no key for anyone) so the request falls through to the
    # mocked deep path instead of attempting a real Anthropic network call —
    # the point of this test is only to observe which member_id was resolved.
    test_client, job_mock = ask_client
    tdir = tmp_path / "customers" / "_transcripts"
    tdir.mkdir(parents=True)
    (tdir / "Acme-07.14.26.txt").write_text("[12:00:00] Gary: hello", encoding="utf-8")

    seen = []

    def spy_api_key(member_id=None):
        seen.append(member_id)
        return None

    app.app.state.ask_service.api_key = spy_api_key
    app.app.state.account_service = type(
        "AS", (), {"owner_for_account": staticmethod(lambda account: "gary" if account == "Acme" else None)}
    )()

    resp = test_client.post("/api/transcribe/file/ask", json={
        "question": "hello",
        "account": "Acme",
        "transcript_name": "Acme-07.14.26.txt",
    })

    assert resp.status_code == 200
    assert resp.json() == {"mode": "deep", "job_id": "job789"}
    assert "gary" in seen
