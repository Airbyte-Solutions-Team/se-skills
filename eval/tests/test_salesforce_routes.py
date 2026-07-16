"""Route-level tests for Salesforce integration endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.account_service import AccountError
from webapp import app as app_module


class FakeAccountService:
    def __init__(self) -> None:
        self.prefs: dict = {"selected_aes": ["Old AE"]}
        self.saved: dict | None = None

    def safe_name(self, name: str) -> str:
        if not isinstance(name, str) or ".." in name or "/" in name or "\\" in name:
            raise AccountError(400, f"Invalid name: {name!r}")
        return name

    def member_by_id(self, member_id: str) -> dict | None:
        if member_id == "gary":
            return {"id": "gary", "name": "Gary"}
        return None

    def read_member_prefs(self, member_id: str) -> dict:
        return self.prefs

    def save_member_prefs(self, member_id: str, prefs: dict) -> None:
        self.saved = prefs


class FakeSalesforce:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def stage_and_amount_for_accounts(self, names: list[str]) -> dict:
        self.calls.append(("stage_and_amount_for_accounts", (names,), {}))
        return {n: {"stage": "3 - Solution"} for n in names}

    async def list_account_executives(self) -> list[str]:
        self.calls.append(("list_account_executives", (), {}))
        return ["Alice", "Bob"]

    async def accounts_for_member(self, member: dict, aes: list[str]) -> dict:
        self.calls.append(("accounts_for_member", (member, aes), {}))
        return {"new_business": [{"name": "Acme", "account_name": "Acme"}], "renewals": []}


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def fake_services(client):
    fake_account = FakeAccountService()
    fake_salesforce = FakeSalesforce()
    original_account = app_module.app.state.account_service
    original_salesforce = getattr(app_module.app.state, "salesforce_integration", None)
    app_module.app.state.account_service = fake_account
    app_module.app.state.salesforce_integration = fake_salesforce
    yield fake_account, fake_salesforce
    app_module.app.state.account_service = original_account
    app_module.app.state.salesforce_integration = original_salesforce


def test_stage_amount_route_registered_and_delegates(client, fake_services) -> None:
    fake_account, fake_salesforce = fake_services
    response = client.post("/api/sfdc/stage-amount", json={"accounts": ["Acme", "Foo"]})  # noqa: E501
    assert response.status_code == 200
    data = response.json()
    assert "Acme" in data and "Foo" in data
    assert fake_salesforce.calls == [
        ("stage_and_amount_for_accounts", (["Acme", "Foo"],), {}),
    ]


def test_stage_amount_route_rejects_non_list_accounts(client, fake_services) -> None:
    response = client.post("/api/sfdc/stage-amount", json={"accounts": "not-a-list"})
    assert response.status_code == 400
    assert "accounts must be a list" in response.json()["detail"]


def test_stage_amount_route_rejects_invalid_account_name(client, fake_services) -> None:
    response = client.post("/api/sfdc/stage-amount", json={"accounts": ["../etc/passwd"]})
    assert response.status_code == 400


def test_sfdc_aes_route_get_registered_and_delegates(client, fake_services) -> None:
    fake_account, fake_salesforce = fake_services
    response = client.get("/api/members/gary/sfdc-aes")
    assert response.status_code == 200
    data = response.json()
    assert data["aes"] == ["Alice", "Bob"]
    assert data["selected"] == ["Old AE"]


def test_sfdc_aes_route_unknown_member_returns_404(client, fake_services) -> None:
    response = client.get("/api/members/unknown/sfdc-aes")
    assert response.status_code == 404


def test_sfdc_aes_route_post_saves_selection(client, fake_services) -> None:
    fake_account, _ = fake_services
    response = client.post(
        "/api/members/gary/sfdc-aes",
        json={"selected": ["New AE", ""]},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "selected": ["New AE"]}
    assert fake_account.saved == {"selected_aes": ["New AE"]}


def test_sfdc_accounts_route_registered_and_delegates(client, fake_services) -> None:
    _, fake_salesforce = fake_services
    response = client.post(
        "/api/members/gary/sfdc-accounts",
        json={"aes": ["Alice"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["new_business"][0]["name"] == "Acme"
    assert fake_salesforce.calls == [
        ("accounts_for_member", ({"id": "gary", "name": "Gary"}, ["Alice"]), {}),
    ]


def test_sfdc_accounts_route_unknown_member_returns_404(client, fake_services) -> None:
    response = client.post(
        "/api/members/unknown/sfdc-accounts",
        json={"aes": ["Alice"]},
    )
    assert response.status_code == 404


def _collect_routes(app, collected=None):
    if collected is None:
        collected = {}
    for r in app.routes:
        if hasattr(r, "path"):
            collected.setdefault(r.path, set()).update(getattr(r, "methods", set()))
        if hasattr(r, "routes"):
            _collect_routes(r, collected)
        if hasattr(r, "original_router"):
            _collect_routes(r.original_router, collected)
    return collected


def test_sfdc_routes_preserve_existing_urls_methods(client) -> None:
    """The route registrations, methods, and paths are unchanged from app.py."""
    routes = _collect_routes(app_module.app)
    assert "/api/sfdc/stage-amount" in routes
    assert "POST" in routes["/api/sfdc/stage-amount"]
    assert "/api/members/{member_id}/sfdc-aes" in routes
    assert {"GET", "POST"}.issubset(routes["/api/members/{member_id}/sfdc-aes"])
    assert "/api/members/{member_id}/sfdc-accounts" in routes
    assert "POST" in routes["/api/members/{member_id}/sfdc-accounts"]
