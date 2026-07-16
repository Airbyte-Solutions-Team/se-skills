"""Deterministic tests for the Salesforce integration boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from integrations.salesforce import SalesforceIntegration


def _titlecase(name: str) -> str:
    import re
    return "-".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9-]+", "-", (name or "").strip()).strip("-") or "opportunity"


def _make_integration(
    tmp_path: Path,
    sf_config: dict | None = None,
) -> SalesforceIntegration:
    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    return SalesforceIntegration(
        customers_dir=customers,
        workspace=tmp_path / "workspace",
        sf_config=lambda: sf_config or {},
        titlecase=_titlecase,
        slug=_slug,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Configuration and availability
# ---------------------------------------------------------------------------
def test_integration_disabled_returns_empty(tmp_path: Path) -> None:
    """When Salesforce is explicitly disabled, all operations return empty safely."""
    sf = _make_integration(tmp_path, sf_config={"enabled": False})
    assert _run(sf.opportunities_for_account("Acme")) == []
    assert _run(sf.stage_and_amount_for_accounts(["Acme"])) == {}
    assert _run(sf.list_account_executives()) == []
    assert _run(sf.accounts_for_member({"name": "Gary"}, [])) == {
        "new_business": [],
        "renewals": [],
    }


def test_missing_config_is_enabled_by_default(tmp_path: Path) -> None:
    """With no `salesforce` config block, the integration is enabled by default and
    falls back gracefully when the CLI is absent."""
    sf = _make_integration(tmp_path, sf_config={})
    assert sf.is_enabled() is True
    # `_run_query` cannot find `sf` in the test environment; it should return None.
    assert _run(sf._run_query("SELECT Id FROM Account")) is None


# ---------------------------------------------------------------------------
# Opportunity retrieval
# ---------------------------------------------------------------------------
def test_opportunities_for_account_maps_records(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})
    records = [
        {
            "Name": "Big Deal",
            "StageName": "3 - Solution",
            "Stage_Number__c": 3,
            "Amount": 100000.0,
            "CloseDate": "2026-08-01",
            "Type": "New Business",
            "IsClosed": False,
            "Owner": {"Name": "Alice"},
        }
    ]

    async def fake_query(query: str) -> list[dict]:
        return records

    monkeypatch.setattr(sf, "_run_query", fake_query)
    result = _run(sf.opportunities_for_account("Acme"))
    assert len(result) == 1
    assert result[0]["name"] == "Big Deal"
    assert result[0]["slug"] == "Big-Deal"
    assert result[0]["stage"] == "3 - Solution"
    assert result[0]["amount"] == 100000.0
    assert result[0]["ae"] == "Alice"
    assert result[0]["is_closed"] is False


def test_opportunities_for_account_empty_result_returns_empty(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})

    async def fake_query(query: str) -> list:
        return []

    monkeypatch.setattr(sf, "_run_query", fake_query)
    assert _run(sf.opportunities_for_account("Acme")) == []


def test_opportunities_for_account_query_failure_returns_empty(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})

    async def fake_query(query: str) -> None:
        return None

    monkeypatch.setattr(sf, "_run_query", fake_query)
    assert _run(sf.opportunities_for_account("Acme")) == []


# ---------------------------------------------------------------------------
# Stage / amount enrichment
# ---------------------------------------------------------------------------
def test_stage_and_amount_prefers_open_non_renewal(tmp_path: Path, monkeypatch) -> None:
    """For each account, the most relevant open, non-renewal opportunity wins."""
    sf = _make_integration(tmp_path, sf_config={"enabled": True})

    async def fake_query(query: str) -> list[dict]:
        # Note: ORDER BY CloseDate DESC means the closed renewal appears first in raw list.
        return [
            {
                "Account": {"Name": "Acme Corp"},
                "StageName": "Closed Won",
                "Stage_Number__c": 5,
                "Amount": 50.0,
                "CloseDate": "2026-09-01",
                "Type": "Renewal",
                "IsClosed": True,
                "Owner": {"Name": "Old AE"},
            },
            {
                "Account": {"Name": "Acme Corp"},
                "StageName": "3 - Solution",
                "Stage_Number__c": 3,
                "Amount": 200.0,
                "CloseDate": "2026-07-01",
                "Type": "New Business",
                "IsClosed": False,
                "Owner": {"Name": "New AE"},
            },
        ]

    monkeypatch.setattr(sf, "_run_query", fake_query)
    result = _run(sf.stage_and_amount_for_accounts(["Acme-Corp"]))
    assert result == {
        "Acme-Corp": {
            "stage": "3 - Solution",
            "stage_num": 3,
            "amount": 200.0,
            "ae": "New AE",
            "type": "New Business",
            "close_date": "2026-07-01",
            "is_closed": False,
        }
    }


def test_stage_and_amount_uses_sfdc_name_sidecar(tmp_path: Path, monkeypatch) -> None:
    """When a `.sfdc-name` sidecar exists, mapping uses the real Account.Name."""
    sf = _make_integration(tmp_path)
    account_dir = tmp_path / "customers" / "Octus-Fka-Reorg-Research"
    account_dir.mkdir(parents=True)
    (account_dir / ".sfdc-name").write_text("Octus (fka Reorg Research)")

    async def fake_query(query: str) -> list[dict]:
        assert "Octus" in query
        return [
            {
                "Account": {"Name": "Octus (fka Reorg Research)"},
                "StageName": "2 - Discovery",
                "Stage_Number__c": 2,
                "Amount": 75.0,
                "CloseDate": "2026-08-15",
                "Type": "New Business",
                "IsClosed": False,
                "Owner": {"Name": "Bob"},
            }
        ]

    monkeypatch.setattr(sf, "_run_query", fake_query)
    result = _run(sf.stage_and_amount_for_accounts(["Octus-Fka-Reorg-Research"]))
    assert "Octus-Fka-Reorg-Research" in result
    assert result["Octus-Fka-Reorg-Research"]["stage"] == "2 - Discovery"


def test_stage_and_amount_caps_account_count(tmp_path: Path, monkeypatch) -> None:
    """The stage+amount query only considers the first 50 account names."""
    sf = _make_integration(tmp_path, sf_config={"enabled": True})
    captured: list[str] = []

    async def fake_query(query: str) -> list[dict]:
        captured.append(query)
        return []

    monkeypatch.setattr(sf, "_run_query", fake_query)
    names = [f"Account-{i}" for i in range(60)]
    _run(sf.stage_and_amount_for_accounts(names))
    assert len(captured) == 1
    query = captured[0]
    # 50 LIKE clauses should be present, not 60.
    assert query.count("LIKE") == 50


def test_stage_and_amount_empty_names_returns_empty(tmp_path: Path) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})
    assert _run(sf.stage_and_amount_for_accounts([])) == {}


# ---------------------------------------------------------------------------
# AE listing and member account pull
# ---------------------------------------------------------------------------
def test_list_account_executives_returns_sorted_distinct(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})
    captured_queries: list[str] = []

    async def fake_query(query: str) -> list[dict]:
        captured_queries.append(query)
        return [
            {"Owner": {"Name": "Carol"}},
            {"Owner": {"Name": "Bob"}},
            {"Owner": {"Name": "Carol"}},
            {"Owner": {"Name": ""}},
        ]

    monkeypatch.setattr(sf, "_run_query", fake_query)
    assert _run(sf.list_account_executives()) == ["Bob", "Carol"]
    assert captured_queries
    assert "IsClosed = false" in captured_queries[0]


def test_accounts_for_member_splits_and_checks_exists(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})
    (tmp_path / "customers" / "Acme-Corp").mkdir(parents=True)
    captured_queries: list[str] = []

    async def fake_query(query: str) -> list[dict]:
        captured_queries.append(query)
        return [
            {
                "Account": {"Name": "Acme Corp"},
                "Amount": 100.0,
                "StageName": "3 - Solution",
                "Stage_Number__c": 3,
                "CloseDate": "2026-08-01",
                "Type": "New Business",
                "Owner": {"Name": "Owner1"},
                "SE_Name__c": "Gary",
            },
            {
                "Account": {"Name": "Old Customer"},
                "Amount": 50.0,
                "StageName": "Closed Won",
                "Stage_Number__c": 5,
                "CloseDate": "2026-07-01",
                "Type": "Renewal",
                "Owner": {"Name": "Owner2"},
                "SE_Name__c": "",
            },
        ]

    monkeypatch.setattr(sf, "_run_query", fake_query)
    result = _run(sf.accounts_for_member({"name": "Gary"}, ["Owner1"]))
    assert len(result["new_business"]) == 1
    assert result["new_business"][0]["name"] == "Acme-Corp"
    assert result["new_business"][0]["exists"] is True
    assert len(result["renewals"]) == 1
    assert result["renewals"][0]["name"] == _titlecase("Old Customer")
    assert result["renewals"][0]["renewal"] is True
    assert captured_queries
    assert "Gary" in captured_queries[0] and "Owner1" in captured_queries[0]


# ---------------------------------------------------------------------------
# Query construction and safety
# ---------------------------------------------------------------------------
def test_quote_escapes_soql_metacharacters() -> None:
    sf = SalesforceIntegration(
        customers_dir=Path("/tmp"),
        workspace=Path("/tmp"),
        sf_config=lambda: {},
        titlecase=_titlecase,
        slug=_slug,
    )
    escaped = sf._quote("O'Brien \"Jr\"")
    assert "\\'" in escaped
    assert '\\"' in escaped


def test_sfdc_name_reading_rejects_path_traversal(tmp_path: Path) -> None:
    """`_read_sfdc_name` must not follow traversal attempts."""
    sf = _make_integration(tmp_path)
    assert sf._read_sfdc_name("../etc/passwd") is None
    assert sf._read_sfdc_name("/absolute/path") is None


def test_run_query_returns_none_on_failure(tmp_path: Path, monkeypatch) -> None:
    """Any CLI or JSON failure must produce a safe `None`, not an exception."""
    sf = _make_integration(tmp_path, sf_config={"enabled": True})

    async def boom(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    assert _run(sf._run_query("SELECT Id FROM Account")) is None


def test_run_query_decodes_json_records(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True, "org_alias": "test"})

    class FakeProc:
        returncode = 0
        async def communicate(self):
            payload = json.dumps({"result": {"records": [{"Name": "X"}]}})
            return payload.encode(), b""

    called = {}

    async def fake_exec(*args, **kwargs) -> FakeProc:
        called["args"] = args
        called["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    result = _run(sf._run_query("SELECT Name FROM Account"))
    assert result == [{"Name": "X"}]
    assert called["args"][:3] == ("sf", "data", "query")
    assert "--target-org" in called["args"]
    assert "test" in called["args"]
    assert str(tmp_path / "workspace") == called["cwd"]


def test_run_query_returns_none_on_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    sf = _make_integration(tmp_path, sf_config={"enabled": True})

    class FakeProc:
        returncode = 1
        async def communicate(self):
            return b"", b"auth error"

    async def fake_exec(*args, **kwargs) -> FakeProc:
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    assert _run(sf._run_query("SELECT Id FROM Account")) is None
