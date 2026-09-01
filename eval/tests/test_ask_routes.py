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
        api_key=lambda member_id=None: None,
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
# Manual escalation (force_deep / prior_answer)
# ---------------------------------------------------------------------------
def test_output_ask_force_deep_field_accepted(client, tmp_path: Path) -> None:
    test_client, job_mock = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "hello",
        "force_deep": True,
        "prior_answer": "the quick pass said X",
    })

    assert response.status_code == 200
    assert response.json() == {"mode": "deep", "job_id": "job123"}
    job_mock.assert_awaited_once()
    assert "the quick pass said X" in job_mock.call_args.kwargs["prompt"]


def test_output_ask_prior_answer_over_max_length_422(client, tmp_path: Path) -> None:
    test_client, _ = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "hello",
        "prior_answer": "x" * 8_001,
    })

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# No key fallback
# ---------------------------------------------------------------------------
def test_output_ask_no_key_routes_to_deep_for_non_deep_question(client, tmp_path: Path) -> None:
    # Regression: a question with no DEEP_HINTS keyword ("summary") must still
    # route to claude -p when no API key is configured — it must not return a
    # "needs_deep" dead end that a re-ask can never escape.
    test_client, job_mock = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "summary",
    })

    assert response.status_code == 200
    assert response.json() == {"mode": "deep", "job_id": "job123"}
    job_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Owner-resolution wiring (per-member Anthropic key)
# ---------------------------------------------------------------------------
class _StubAccountService:
    def __init__(self, owners: dict[str, str] | None = None, members: set[str] | None = None):
        self._owners = owners or {}
        self._members = members if members is not None else set(self._owners.values())

    def owner_for_account(self, account):
        return self._owners.get(account)

    def member_by_id(self, member_id):
        return {"id": member_id} if member_id in self._members else None


def test_output_ask_resolves_member_id_from_account_owner(client, tmp_path: Path) -> None:
    # Spy returns None (no key for anyone) so the request falls through to the
    # mocked deep path instead of attempting a real Anthropic network call —
    # the point of this test is only to observe which member_id was resolved.
    from webapp.app import app

    test_client, job_mock = client
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    app.state.account_service = _StubAccountService(owners={"Acme": "gary"})

    seen = []

    def spy_api_key(member_id=None):
        seen.append(member_id)
        return None

    app.state.ask_service.api_key = spy_api_key

    response = test_client.post("/api/output/ask", json={
        "path": "Acme/outputs/deal/deal.md",
        "question": "summary",
        "account": "Acme",
    })

    assert response.status_code == 200
    assert response.json() == {"mode": "deep", "job_id": "job123"}
    job_mock.assert_awaited_once()
    assert "gary" in seen


def test_ai_status_resolves_member_id_from_account_owner(client) -> None:
    from webapp.app import app

    test_client, _ = client
    app.state.account_service = _StubAccountService(owners={"Acme": "gary"})
    app.state.ask_service.api_key = lambda member_id=None: "key" if member_id == "gary" else None

    response = test_client.get("/api/ai-status?account=Acme")
    assert response.json() == {"quick_path": True}

    response = test_client.get("/api/ai-status?account=Unknown")
    assert response.json() == {"quick_path": False}


# ---------------------------------------------------------------------------
# Member key management endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def key_client(client, monkeypatch):
    from webapp.app import app
    import services.ask_service as ask_service_module

    test_client, _ = client
    app.state.account_service = _StubAccountService(members={"gary"})

    store: dict[str, str] = {}
    monkeypatch.setattr(ask_service_module, "_keyring_get", lambda username: store.get(username))
    monkeypatch.setattr(ask_service_module, "_keyring_set", lambda username, value: store.update({username: value}))
    monkeypatch.setattr(ask_service_module, "_keyring_delete", lambda username: store.pop(username, None))
    return test_client, store


def test_member_anthropic_key_404_on_unknown_member(key_client) -> None:
    test_client, _ = key_client
    assert test_client.get("/api/members/nope/anthropic-key").status_code == 404
    assert test_client.post("/api/members/nope/anthropic-key", json={"api_key": "sk-x"}).status_code == 404
    assert test_client.delete("/api/members/nope/anthropic-key").status_code == 404


def test_member_anthropic_key_save_get_clear_round_trip(key_client) -> None:
    test_client, store = key_client

    assert test_client.get("/api/members/gary/anthropic-key").json() == {"configured": False}

    save_resp = test_client.post("/api/members/gary/anthropic-key", json={"api_key": "sk-ant-secret"})
    assert save_resp.status_code == 200
    assert save_resp.json() == {"configured": True}
    assert "sk-ant-secret" not in save_resp.text  # never echoed back

    assert test_client.get("/api/members/gary/anthropic-key").json() == {"configured": True}
    assert store["anthropic_api_key:gary"] == "sk-ant-secret"

    del_resp = test_client.delete("/api/members/gary/anthropic-key")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"configured": False}
    assert test_client.get("/api/members/gary/anthropic-key").json() == {"configured": False}


def test_member_anthropic_key_save_backend_failure_is_503(key_client, monkeypatch) -> None:
    import services.ask_service as ask_service_module

    test_client, _ = key_client

    def raising_set(username, value):
        raise ask_service_module.AskError(503, "no backend")

    monkeypatch.setattr(ask_service_module, "_keyring_set", raising_set)
    response = test_client.post("/api/members/gary/anthropic-key", json={"api_key": "sk-ant-secret"})
    assert response.status_code == 503
