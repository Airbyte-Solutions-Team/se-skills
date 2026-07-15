"""Deterministic tests for `webapp/pov_gsheet_bridge.py`.

The bridge turns raw MCP/tool output into `ExternalEvidence` JSON that
`pov_gsheet_context.py` already consumes. These tests do not call any MCPs;
they exercise normalization, the CLI, and hand-off to `PovContext`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pov_gsheet_bridge import (
    _classify_in_text,
    _sf_field_kind,
    normalize_gong,
    normalize_salesforce,
)
from pov_gsheet_context import (
    PovContext,
    Prospect,
    _merge_external_evidence,
    _source_coverage_from_evidence,
)


def _sf_raw() -> dict:
    return {
        "result": {
            "records": [
                {
                    "Id": "006ABC",
                    "Name": "Acme Expansion",
                    "StageName": "Value Validation",
                    "Amount": 120000,
                    "CloseDate": "2026-08-15",
                    "Type": "New Business",
                    "Owner": {"Name": "Alex Example"},
                    "SE_Name__c": "Gary Yang",
                    "Next_Step__c": "POV kickoff",
                    "Account": {"Name": "Acme Corp", "Id": "001XYZ"},
                    "Required_features_functionality__c": "CDC + schema drift handling",
                    "Most_important_sources__c": "Salesforce, Postgres",
                    "Most_Important_Destinations__c": "Snowflake",
                },
                {
                    "Id": "CONTACT1",
                    "FirstName": "Jane",
                    "LastName": "Customer",
                    "Email": "jane@acme.com",
                    "Title": "VP of Data",
                    "Account": {"Name": "Acme Corp"},
                },
            ]
        }
    }


def _gong_search() -> dict:
    return {
        "calls": [
            {
                "id": "call-1",
                "title": "Acme tech deep-dive",
                "started": "2026-07-10T14:00:00Z",
                "url": "https://app.gong.io/call/call-1",
                "parties": [
                    {"name": "Jane Customer", "email": "jane@acme.com"},
                    {"name": "Gary Yang", "email": "gary@airbyte.io"},
                ],
            }
        ]
    }


def _gong_transcripts() -> dict:
    return {
        "call-1": {
            "transcript": {
                "monologues": [
                    {
                        "speakerId": "Jane Customer",
                        "text": "We need Salesforce and Postgres into Snowflake with CDC.",
                    },
                    {"speakerId": "Gary Yang", "text": "We can do that with Airbyte."},
                    {
                        "speakerId": "Jane Customer",
                        "text": "Action item: schedule a follow-up with the data team.",
                    },
                ]
            }
        }
    }


def test_normalize_salesforce_extracts_prospect_and_fields() -> None:
    evidence = normalize_salesforce(_sf_raw(), account_name="Acme")
    sources = {e.source for e in evidence}
    assert sources == {"salesforce"}

    prospect = next(e for e in evidence if e.fact_type == "prospect")
    assert prospect.fact["opportunity_name"] == "Acme Expansion"
    assert prospect.fact["stage"] == "Value Validation"
    assert prospect.fact["owner"] == "Alex Example"
    assert prospect.source_id == "006ABC"
    assert not prospect.direct_customer

    obj = next(e for e in evidence if e.fact_type == "business_objective")
    assert obj.fact["description"] == "CDC + schema drift handling"

    source_sys = next(
        e for e in evidence if e.fact_type == "technical_system" and e.fact["title"] == "most important sources"
    )
    assert source_sys.fact["kind"] == "source"

    dest_sys = next(
        e for e in evidence if e.fact_type == "technical_system" and e.fact["title"] == "most important destinations"
    )
    assert dest_sys.fact["kind"] == "destination"

    contact = next(e for e in evidence if e.fact_type == "contact")
    assert contact.fact["name"] == "Jane Customer"
    assert contact.fact["role"] == "VP of Data"
    assert contact.fact["email"] == "jane@acme.com"


def test_normalize_gong_classifies_sources_and_destinations() -> None:
    evidence = normalize_gong(
        _gong_search(),
        transcripts=_gong_transcripts(),
        account_name="Acme",
        opportunity_name="Acme Expansion",
    )
    systems = [e for e in evidence if e.fact_type == "technical_system"]
    by_name = {e.fact["name"]: e.fact["kind"] for e in systems}
    assert by_name.get("Salesforce") == "source"
    assert by_name.get("Postgres") == "source"
    assert by_name.get("Snowflake") == "destination"

    reqs = [e for e in evidence if e.fact_type == "requirement"]
    assert len(reqs) == 1
    assert "Salesforce and Postgres into Snowflake" in reqs[0].fact["description"]

    actions = [e for e in evidence if e.fact_type == "action_item"]
    assert len(actions) == 1


def test_bridge_cli_normalizes_salesforce(tmp_path: Path) -> None:
    raw = tmp_path / "sf.json"
    raw.write_text(json.dumps(_sf_raw()), encoding="utf-8")
    out = tmp_path / "evidence.json"
    subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--python",
            "3.11",
            "--script",
            "webapp/pov_gsheet_bridge.py",
            "--source",
            "salesforce",
            "--account",
            "Acme",
            "--opportunity",
            "Acme Expansion",
            "--raw-input",
            str(raw),
            "--out",
            str(out),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert any(e["fact_type"] == "prospect" for e in evidence)
    assert any(e["fact_type"] == "business_objective" for e in evidence)


def test_bridge_cli_returns_unavailable_on_missing_mcp(tmp_path: Path) -> None:
    out = tmp_path / "unavailable.json"
    subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--python",
            "3.11",
            "--script",
            "webapp/pov_gsheet_bridge.py",
            "--source",
            "gong",
            "--account",
            "Acme",
            "--status",
            "unavailable",
            "--note",
            "Gong MCP not configured",
            "--out",
            str(out),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert len(evidence) == 1
    assert evidence[0]["status"] == "unavailable"
    assert evidence[0]["source"] == "gong"
    assert evidence[0]["note"] == "Gong MCP not configured"


def test_bridge_cli_returns_unavailable_on_malformed_input(tmp_path: Path) -> None:
    raw = tmp_path / "bad.json"
    raw.write_text("not valid json", encoding="utf-8")
    out = tmp_path / "evidence.json"
    subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--python",
            "3.11",
            "--script",
            "webapp/pov_gsheet_bridge.py",
            "--source",
            "salesforce",
            "--account",
            "Acme",
            "--raw-input",
            str(raw),
            "--out",
            str(out),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert evidence[0]["status"] == "unavailable"
    assert "Could not read raw input" in evidence[0]["note"]


def test_bridge_feeds_pov_context_with_deduplicated_facts() -> None:
    sf = normalize_salesforce(_sf_raw(), account_name="Acme", opportunity_name="Acme Expansion")
    gong = normalize_gong(
        _gong_search(),
        transcripts=_gong_transcripts(),
        account_name="Acme",
        opportunity_name="Acme Expansion",
    )
    ctx = PovContext(prospect=Prospect(account_name="Acme"))
    _merge_external_evidence(ctx, sf + gong)

    # Salesforce opportunity details should override the minimal prospect.
    assert ctx.prospect.opportunity_name == "Acme Expansion"
    assert ctx.prospect.stage == "Value Validation"
    sources = {s.name for s in ctx.technical_scope["sources"]}
    dests = {d.name for d in ctx.technical_scope["destinations"]}
    assert "Postgres" in sources
    assert "Snowflake" in dests

    coverage = _source_coverage_from_evidence(sf + gong)
    assert any(c.source == "salesforce" and c.status == "searched" for c in coverage)
    assert any(c.source == "gong" and c.status == "searched" for c in coverage)


def test_classify_in_text_respects_into_marker() -> None:
    text = "We need Postgres and Salesforce into Snowflake."
    assert _classify_in_text("Postgres", text) == "source"
    assert _classify_in_text("Salesforce", text) == "source"
    assert _classify_in_text("Snowflake", text) == "destination"


def test_sf_field_kind_infers_direction() -> None:
    assert _sf_field_kind("Most_Important_Destinations__c") == "destination"
    assert _sf_field_kind("Most_important_sources__c") == "source"
    assert _sf_field_kind("Required_features_functionality__c") is None
