"""Deterministic tests for the member/account/opportunity HTTP routes."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.account_service import AccountError, AccountService
from services.job_service import JobService
from services.output_service import OutputService
from webapp.app import _safe, _slug, _titlecase_folder, app


def _test_account_svc(tmp_path: Path) -> AccountService:
    customers_dir = tmp_path / "customers"
    customers_dir.mkdir()
    webapp_dir = tmp_path / "webapp"
    webapp_dir.mkdir()
    output_svc = OutputService(
        customers_dir=customers_dir,
        workspace=tmp_path,
        repo_root=tmp_path,
        se_config=lambda: {},
        safe_name=_safe,
        slug=_slug,
        run_cmd=None,
        internal_repo=None,
    )
    job_svc = JobService(
        workspace=tmp_path,
        model_for=lambda x: "claude-sonnet-4-20250514",
        persist_run=None,
    )
    return AccountService(
        customers_dir=customers_dir,
        webapp_dir=webapp_dir,
        output_service=output_svc,
        job_service=job_svc,
        safe_name=_safe,
        titlecase=_titlecase_folder,
        slug=_slug,
        team_file=webapp_dir / "team-members.yaml",
        member_prefs_dir=webapp_dir / ".member-prefs",
        se_config_file=tmp_path / ".se-config.yaml",
        sfdc_opportunities=None,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    account_svc = _test_account_svc(tmp_path)
    app.state.account_service = account_svc
    with TestClient(app) as c:
        yield c


def test_get_members(client: TestClient) -> None:
    app.state.account_service.create_member("Alice")
    resp = client.get("/api/members")
    assert resp.status_code == 200
    data = resp.json()
    assert any(m["name"] == "Alice" for m in data)


def test_create_member(client: TestClient) -> None:
    resp = client.post("/api/members", json={"name": "Alice", "role": "SE", "email": "a@example.com"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "alice"
    assert data["role"] == "SE"


def test_member_accounts(client: TestClient) -> None:
    svc = app.state.account_service
    svc.create_member("Alice")
    svc.create_account("Acme", owner="alice")
    resp = client.get("/api/members/alice/accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"][0]["name"] == "Acme"


def test_get_account(client: TestClient) -> None:
    app.state.account_service.create_account("Acme", owner="alice")
    resp = client.get("/api/accounts/Acme")
    assert resp.status_code == 200
    assert resp.json() == {"name": "Acme", "owner": "alice"}


def test_create_account(client: TestClient) -> None:
    resp = client.post("/api/accounts", json={"name": "Acme", "owner": "alice", "sfdc_name": "Acme, Inc."})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Acme"
    assert data["created"] is True
    assert (app.state.account_service.customers_dir / "Acme" / ".owner").exists()


def test_delete_account(client: TestClient) -> None:
    svc = app.state.account_service
    svc.create_account("Acme")
    resp = client.delete("/api/accounts/Acme")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True
    assert not (svc.customers_dir / "Acme").exists()


def test_outputs_route_delegates_to_output_service(client: TestClient) -> None:
    svc = app.state.account_service
    svc.create_account("Acme")
    # Override output_service with a fake for this route.
    class FakeOutputService:
        def list_outputs(self, account: str, opp: str | None = None) -> list:
            return [{"account": account, "opp": opp, "fake": True}]
    app.state.output_service = FakeOutputService()
    resp = client.get("/api/accounts/Acme/outputs?opp=intro")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["account"] == "Acme"
    assert data[0]["opp"] == "intro"


def test_opportunities_route(client: TestClient) -> None:
    svc = app.state.account_service
    svc.create_account("Acme")

    async def fake_sfdc(account: str) -> list[dict]:
        return [{"name": "Intro", "slug": "intro", "stage": "open", "stage_num": 1, "amount": None, "close_date": None, "type": None, "is_closed": False, "ae": None}]

    svc._sfdc_opportunities = fake_sfdc
    resp = client.get("/api/accounts/Acme/opportunities")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["name"] == "Intro"
    assert "output_count" in data[0]


def test_outputs_query_traversal_rejected(client: TestClient) -> None:
    svc = app.state.account_service
    svc.create_account("Acme")
    resp = client.get("/api/accounts/Acme/outputs?opp=../escape")
    assert resp.status_code == 400


def test_opportunities_query_not_used(client: TestClient) -> None:
    # The opportunities route uses the account path parameter only; query params are ignored.
    svc = app.state.account_service
    svc.create_account("Acme")
    resp = client.get("/api/accounts/Acme/opportunities?extra=ignored")
    assert resp.status_code == 200


def test_create_account_invalid_name(client: TestClient) -> None:
    resp = client.post("/api/accounts", json={"name": "../escape"})
    assert resp.status_code == 400


def test_bulk_action_traversal_rejected(client: TestClient) -> None:
    resp = client.post("/api/bulk/archive", json={"accounts": ["../escape"]})
    assert resp.status_code == 400


def test_restore_trash_invalid_id(client: TestClient) -> None:
    resp = client.post("/api/trash/bad-id/restore")
    assert resp.status_code == 400


def test_restore_trash_not_in_trash(client: TestClient) -> None:
    resp = client.post("/api/trash/Acme__20250714-000000/restore")
    assert resp.status_code == 404


def test_restore_trash_existing_dest(client: TestClient) -> None:
    svc = app.state.account_service
    svc.create_account("Acme")
    svc.create_account("Acme__20250714-000000")
    trash = svc.customers_dir / "_trash" / "Acme__20250714-000000"
    trash.mkdir(parents=True)
    resp = client.post("/api/trash/Acme__20250714-000000/restore")
    assert resp.status_code == 409
