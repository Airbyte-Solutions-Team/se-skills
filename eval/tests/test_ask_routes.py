"""Deterministic route tests for the Ask endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.ask_service import AskService
from services.output_service import OutputService


def _output_service(tmp_path: Path) -> OutputService:
    return OutputService(
        customers_dir=tmp_path,
        workspace=tmp_path,
        repo_root=tmp_path,
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n,
        run_cmd=None,
        internal_repo=None,
    )


def _make_file(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """A TestClient whose AskService uses a temporary output directory."""
    from webapp.app import app

    output_svc = _output_service(tmp_path)
    job_mock = AsyncMock(return_value=("job123", None))
    job_service = type("JS", (), {"launch": job_mock})()
    ask_svc = AskService(
        output_service=output_svc,
        job_service=job_service,
        api_key=lambda: None,
        model_for=lambda use: "claude-sonnet-4-6",
    )
    app.state.ask_service = ask_svc
    return TestClient(app), job_mock


# ---------------------------------------------------------------------------
# Route registration and methods
# ---------------------------------------------------------------------------
def test_output_ask_route_exists_and_accepts_post(client) -> None:
    test_client, _ = client
    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "hello",
    })
    # Missing file → 404 (route is registered and accepted POST).
    assert response.status_code == 404


def test_ai_status_route_get(client) -> None:
    test_client, _ = client
    response = test_client.get("/api/ai-status")
    assert response.status_code == 200
    assert response.json() == {"quick_path": False}


# ---------------------------------------------------------------------------
# Validation and path safety
# ---------------------------------------------------------------------------
def test_output_ask_empty_question_400(client) -> None:
    test_client, _ = client
    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "",
    })
    assert response.status_code == 400


def test_output_ask_missing_output_404(client) -> None:
    test_client, _ = client
    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/missing.md",
        "question": "hello",
    })
    assert response.status_code == 404


def test_output_ask_outside_path_404(client, tmp_path: Path) -> None:
    test_client, _ = client
    response = test_client.post("/api/output/ask", json={
        "path": "../outside.md",
        "question": "hello",
    })
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Deep path
# ---------------------------------------------------------------------------
def test_output_ask_deep_returns_job(client, tmp_path: Path) -> None:
    test_client, job_mock = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "which connector should we use?",
    })

    assert response.status_code == 200
    assert response.json() == {"mode": "deep", "job_id": "job123"}
    job_mock.assert_awaited_once()
    assert job_mock.call_args.kwargs["skill"] == "output-ask"


def test_output_ask_deep_persistence_warning(client, tmp_path: Path) -> None:
    test_client, _ = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    from webapp.app import app
    app.state.ask_service.job_service = type("JS", (), {
        "launch": AsyncMock(return_value=("job456", "disk full"))
    })()

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "deployment",
    })

    assert response.status_code == 200
    assert response.json() == {"mode": "deep", "job_id": "job456", "persistence_warning": "disk full"}


# ---------------------------------------------------------------------------
# No key fallback
# ---------------------------------------------------------------------------
def test_output_ask_no_key_returns_needs_deep(client, tmp_path: Path) -> None:
    test_client, _ = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "summary",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "needs_deep"
    assert "No ANTHROPIC_API_KEY" in data["reason"]
