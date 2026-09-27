"""Synthetic active-opportunity coverage; no real CRM records or credentials."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from integrations.salesforce import SalesforceIntegration
from services.salesforce_portfolio_service import SalesforcePortfolioService
from eval.tests.test_command_center_read_service import ReadHarness

ID1 = "006000000000001"
ID2 = "006000000000002"
ID3 = "006000000000003"
ACCOUNT_ID = "001000000000001"
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def crm_row(opp_id: str, *, name: str = "Pilot") -> dict:
    return {"sfdc_id": opp_id, "name": name, "account_id": ACCOUNT_ID,
            "account_name": "Synthetic Account", "stage": "Discovery",
            "owner": "Synthetic AE", "close_date": "2026-01-01",
            "sfdc_url": f"https://example.my.salesforce.com/lightning/r/Opportunity/{opp_id}/view",
            "token": "MUST_NOT_PERSIST"}


class FakeAccounts:
    def __init__(self, customers: Path) -> None:
        self.customers = customers

    def member_by_id(self, member_id: str):
        return {"id": "se", "name": "Synthetic SE"} if member_id == "se" else None

    def titlecase(self, name: str) -> str:
        return name

    def _resolve_account_dir(self, account: str, must_exist=True) -> Path:
        return self.customers / account

    def _resolve_opportunity_dir(self, account: str, slug: str, must_exist=True) -> Path:
        return self.customers / account / "opportunities" / slug

    def create_account(self, name: str, owner=None, sfdc_name=None):
        (self.customers / name).mkdir(parents=True)
        return {"name": name, "created": True}


class FakeSalesforce:
    def __init__(self) -> None:
        self.calls = []
        self.result = {"state": "ok", "records": [], "truncated": False}

    async def active_opportunity_portfolio(self, se, aes, *, limit):
        self.calls.append((se, aes, limit))
        return self.result


def setup(tmp_path):
    h = ReadHarness(tmp_path)
    sf = FakeSalesforce()
    service = SalesforcePortfolioService(customers_dir=h.customers,
                accounts=FakeAccounts(h.customers), salesforce=sf, clock=lambda: NOW)
    h.reads._salesforce_portfolio = service
    h.app.state.salesforce_portfolio_service = service
    return h, sf, service


def test_refresh_preserves_ids_and_page_navigation_is_offline(tmp_path) -> None:
    h, sf, service = setup(tmp_path)
    (h.customers / "Local" / "opportunities" / "only-local").mkdir(parents=True)
    (h.customers / "Local" / "opportunities" / "mapped").mkdir(parents=True)
    row1 = crm_row(ID1)
    row1["sfdc_url"] = f"https://MUST_NOT_PERSIST@example.my.salesforce.com/lightning/r/Opportunity/{ID1}/view?token=MUST_NOT_PERSIST"
    sf.result = {"state": "ok", "records": [row1, crm_row(ID2), crm_row(ID3, name="Other")],
                 "truncated": False}
    response = h.client.post("/api/command-center/salesforce/refresh",
                             json={"member_id": "se", "ae_names": ["Synthetic AE"]})
    assert response.status_code == 200
    assert response.json()["last_success"]["count"] == 3
    assert "records" not in response.json()["last_success"]
    assert len(sf.calls) == 1
    assert sf.calls[0] == ("Synthetic SE", ["Synthetic AE"], 200)
    assert "MUST_NOT_PERSIST" not in (h.customers / ".command-center" / "salesforce-portfolio.json").read_text()
    portfolio = h.get("/api/command-center/portfolio")
    assert portfolio["total"] == 5
    assert {r["crm"]["sfdc_id"] for r in portfolio["opportunities"] if r["origin"] == "crm_only"} == {ID1, ID2, ID3}
    assert len([r for r in portfolio["opportunities"] if r["origin"] == "local_only"]) == 2
    assert h.get("/api/command-center/today")["crm_only_count"] == 3
    assert h.get("/api/command-center/portfolio", offset=2)["total"] == 5
    assert h.get("/api/command-center/portfolio", account="Synthetic Account")["total"] == 3
    assert h.get("/api/command-center/portfolio", attention_only=True)["total"] == 3
    assert len(sf.calls) == 1  # Today and Portfolio use saved JSON only.
    mapped = h.client.post("/api/command-center/salesforce/mappings",
                           json={"sfdc_id": ID1, "account": "Local", "opportunity_slug": "mapped"})
    assert mapped.status_code == 200
    rows = h.get("/api/command-center/portfolio")["opportunities"]
    assert len(rows) == 4
    assert next(r for r in rows if r["origin"] == "crm_and_local")["crm"]["sfdc_id"] == ID1
    assert {r["crm"]["sfdc_id"] for r in rows if r["origin"] == "crm_only"} == {ID2, ID3}
    assert len(sf.calls) == 1


def test_failure_preserves_success_and_truncation_is_explicit(tmp_path) -> None:
    h, sf, service = setup(tmp_path)
    sf.result = {"state": "ok", "records": [crm_row(ID1)], "truncated": True}
    asyncio.run(service.refresh("se", []))
    first = service.snapshot()["last_success"]
    assert first["truncated"] is True and first["complete"] is False
    assert h.get("/api/command-center/portfolio")["coverage"]["snapshot"]["truncated"] is True
    sf.result = {"state": "auth_error", "records": [], "truncated": False}
    asyncio.run(service.refresh("se", ["Synthetic AE"]))
    saved = service.snapshot()
    assert saved["last_success"] == first
    assert saved["last_attempt"]["state"] == "auth_error"
    coverage = h.get("/api/command-center/today")["coverage"]
    assert coverage["snapshot"]["stale"] is True
    assert "failed" in coverage["label"].lower()
    assert h.get("/api/command-center/portfolio")["total"] == 1
    assert len(sf.calls) == 2


def test_zero_success_differs_from_query_failure(tmp_path) -> None:
    h, sf, service = setup(tmp_path)
    asyncio.run(service.refresh("se", []))
    assert service.snapshot()["last_success"]["count"] == 0
    assert service.snapshot()["last_attempt"]["state"] == "ok"
    sf.result = {"state": "error"}
    asyncio.run(service.refresh("se", []))
    assert service.snapshot()["last_success"]["count"] == 0
    assert service.snapshot()["last_attempt"]["state"] == "error"


def test_establish_is_explicit_and_mapping_rejects_double_assignment(tmp_path) -> None:
    h, sf, service = setup(tmp_path)
    sf.result = {"state": "ok", "records": [crm_row(ID1), crm_row(ID2)], "truncated": False}
    asyncio.run(service.refresh("se", []))
    assert not any(p.name.startswith("CRM-") for p in h.customers.iterdir())
    created = h.client.post(f"/api/command-center/salesforce/{ID1}/establish")
    assert created.status_code == 200
    mapping = created.json()
    assert (h.customers / mapping["account"] / "opportunities" / mapping["opportunity_slug"]).is_dir()
    assert h.get("/api/command-center/portfolio")["total"] == 2
    conflict = h.client.post("/api/command-center/salesforce/mappings",
        json={"sfdc_id": ID2, "account": mapping["account"], "opportunity_slug": mapping["opportunity_slug"]})
    assert conflict.status_code == 409


def test_confirmed_source_id_joins_without_name_matching(tmp_path) -> None:
    h, sf, service = setup(tmp_path)
    h.workspace.opportunity["sfdc_id"] = ID1
    source_id = h.import_note("note_synthetic_v1.json")
    asyncio.run(h.confirm(source_id))
    sf.result = {"state": "ok", "records": [crm_row(ID1, name="Completely different name"),
                                            crm_row(ID2, name="Completely different name")],
                 "truncated": False}
    asyncio.run(service.refresh("se", []))
    rows = h.get("/api/command-center/portfolio")["opportunities"]
    assert len(rows) == 2
    assert next(r for r in rows if r["origin"] == "crm_and_local")["crm"]["sfdc_id"] == ID1
    assert next(r for r in rows if r["origin"] == "crm_only")["crm"]["sfdc_id"] == ID2
    rejected = h.client.post("/api/command-center/salesforce/mappings", json={
        "sfdc_id": ID2, "account": "Acme", "opportunity_slug": "synthetic-opportunity"})
    assert rejected.status_code == 409


def test_cli_query_is_bounded_keeps_overdue_open_and_distinct_ids(tmp_path, monkeypatch) -> None:
    sf = SalesforceIntegration(customers_dir=tmp_path, workspace=tmp_path,
        sf_config=lambda: {"enabled": True, "instance_url": "https://example.my.salesforce.com"},
        titlecase=lambda value: value, slug=lambda value: value)
    called = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            records = [{"Id": ID1, "Name": "Pilot", "Account": {"Id": ACCOUNT_ID, "Name": "Synthetic"},
                        "Owner": {"Name": "AE"}, "StageName": "Discovery", "CloseDate": "2026-01-01"},
                       {"Id": ID2, "Name": "Pilot", "Account": {"Id": ACCOUNT_ID, "Name": "Synthetic"},
                        "Owner": {"Name": "AE"}, "StageName": "Discovery", "CloseDate": "2026-01-01"}]
            return json.dumps({"status": 0, "result": {"records": records, "done": True}}).encode(), b""

    async def fake_exec(*args, **kwargs):
        called["args"] = args
        return Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    result = asyncio.run(sf.active_opportunity_portfolio("Synthetic SE", ["AE"], limit=2))
    query = called["args"][called["args"].index("--query") + 1]
    assert "IsClosed = false" in query and "CloseDate >=" not in query
    assert "SE_Name__c = 'Synthetic SE'" in query and "Owner.Name IN ('AE')" in query
    assert "LIMIT 3" in query
    assert result["state"] == "ok" and result["truncated"] is False
    assert [r["sfdc_id"] for r in result["records"]] == [ID1, ID2]


def test_cli_zero_truncation_and_auth_failure_are_distinct(tmp_path, monkeypatch) -> None:
    sf = SalesforceIntegration(customers_dir=tmp_path, workspace=tmp_path,
        sf_config=lambda: {"enabled": True, "instance_url": "https://example.my.salesforce.com"},
        titlecase=lambda value: value, slug=lambda value: value)
    response = {"code": 0, "records": [], "stderr": b""}

    class Proc:
        @property
        def returncode(self):
            return response["code"]

        async def communicate(self):
            payload = {"status": 0, "result": {"records": response["records"], "done": True}}
            return json.dumps(payload).encode(), response["stderr"]

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    zero = asyncio.run(sf.active_opportunity_portfolio("Synthetic SE", [], limit=2))
    assert zero == {"state": "ok", "records": [], "truncated": False}
    response["records"] = [{"Id": ID1}, {"Id": ID2}, {"Id": ID3}]
    truncated = asyncio.run(sf.active_opportunity_portfolio("Synthetic SE", [], limit=2))
    assert truncated["state"] == "ok" and truncated["truncated"] is True
    assert [r["sfdc_id"] for r in truncated["records"]] == [ID1, ID2]
    response["code"] = 1
    response["stderr"] = b"expired access/refresh token"
    failed = asyncio.run(sf.active_opportunity_portfolio("Synthetic SE", [], limit=2))
    assert failed == {"state": "auth_error", "records": [], "truncated": False}
