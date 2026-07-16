"""Deterministic tests for the `pov-gsheet` port.

These tests prove the skill can be loaded from the repository, builds a structured
`PovContext` from local workspace files and optional Salesforce, and produces a
plausible dry-run plan for the Google Sheets helper. They do not require a Google
sign-in or live browser.
"""
from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from pov_gsheet_context import (
    PovContext,
    Prospect,
    Contact,
    BusinessObjective,
    SuccessCriterion,
    TechnicalSystem,
    _classify_system,
    _dedupe_by,
    _dedupe_contacts,
    _derive_paths,
    _find_connectors,
    _load_external_evidence,
    _merge_external_evidence,
    _resolve_se_config,
    build_context,
)

EXPECTED_TAB_NAMES = [
    "Contacts",
    "POV Milestones",
    "Business Objectives",
    "POV - Success Criteria",
    "In-scope Apps",
    "Feature Requests",
    "Architecture Diagrams",
]

DROPDOWN_VALUES = {
    "Status": ["Not Started", "Needs Scheduling", "In Progress", "Completed", "Blocked"],
    "In-scope for POV": ["Yes", "No"],
    "Priority Level": ["Must Have", "Nice to Have", "Out of Scope"],
    "Source or Destination": ["Source", "Destination"],
    "App / System Role - Use Cases": ["Use Case #1", "Use Case #2", "Use Case #3"],
    "Integration Type": ["API", "OAuth", "Other means of integration"],
    "Airbyte Product Area": ["Connectors", "Platform", "Cloud", "Enterprise"],
}

LEGACY_FORBIDDEN_REFS = [
    "se-assistant",
    "se_assistant",
    "DuckDB",
    "duckdb",
    "~/Documents/Claude",
    "db_sales_data",
    "generate_pov.py",
]

# Legacy Granola references from the old se-assistant skill (not the source name itself).
LEGACY_GRANOLA_REFS = [
    "query_granola_meetings",
    "get_meetings",
    "list_meetings",
    "get_meeting_transcript",
]


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def skill_md(repo_root: Path) -> str:
    return (repo_root / "skills" / "pov-gsheet" / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture
def minimal_context() -> dict:
    return {
        "prospect": {
            "account_name": "TestCo",
            "opportunity_name": "default",
            "stage": None,
            "owner": "AE Example",
            "se_name": "SE Example",
            "se_title": "Solutions Engineer",
            "next_step": None,
            "pov_start_date": "2026-07-15",
            "target_completion_date": None,
        },
        "contacts": {"internal": [], "prospect": []},
        "business_objectives": [
            {"objective": "Reduce pipeline maintenance", "sources": ["biz-qual"]}
        ],
        "technical_scope": {
            "sources": [{"name": "Postgres", "kind": "source", "sources": ["tech-qual"]}],
            "destinations": [{"name": "Snowflake", "kind": "destination", "sources": ["tech-qual"]}],
            "use_cases": [],
            "requirements": [],
            "dependencies": [],
        },
        "success_criteria": [
            {
                "feature_or_capability": "CDC end-to-end",
                "validation_method": "Customer validation during POV",
                "in_scope": "Yes",
                "priority": "Must Have",
                "evidence": "tech-qual",
                "sources": ["tech-qual"],
            }
        ],
        "milestones": [],
        "feature_requests": [],
        "architecture_notes": [],
        "unknowns": [],
        "source_coverage": [
            {"source": "prior skill outputs", "available": True, "material": True, "note": "2 outputs"}
        ],
        "status": "complete",
        "warnings": [],
    }


def test_skill_frontmatter_and_tabs(skill_md: str) -> None:
    """The skill declares the correct name and the seven expected template sheets."""
    m = re.search(r"^---\n(.*?)\n---", skill_md, re.DOTALL)
    assert m, "SKILL.md must start with YAML frontmatter"
    front = yaml.safe_load(m.group(1))
    assert front["name"] == "pov-gsheet"
    assert "POV" in front.get("description", "")

    for tab in EXPECTED_TAB_NAMES:
        assert tab in skill_md, f"Tab '{tab}' must be mentioned in SKILL.md"


def test_skill_md_has_no_legacy_dependencies(skill_md: str) -> None:
    """The ported skill no longer references se-assistant, DuckDB, or personal paths."""
    lower = skill_md.lower()
    for ref in LEGACY_FORBIDDEN_REFS:
        assert ref.lower() not in lower, f"SKILL.md still references legacy dependency '{ref}'"
    for ref in LEGACY_GRANOLA_REFS:
        assert ref not in lower, f"SKILL.md still references legacy Granola tool '{ref}'"


def test_skill_md_references_shared_playbook_and_config(skill_md: str) -> None:
    """The skill points at the shared playbook and the .se-config.yaml pov_gsheet block."""
    assert "~/.claude/skills/_se-playbook.md" in skill_md
    assert "pov_gsheet" in skill_md
    assert "template_url" in skill_md
    assert "drive_target_folder_url" in skill_md


def test_skill_md_contains_all_dropdown_values(skill_md: str) -> None:
    """The skill preserves the exact dropdown vocabulary required by the template."""
    for values in DROPDOWN_VALUES.values():
        for v in values:
            assert f'"{v}"' in skill_md or v in skill_md, f"Dropdown value '{v}' missing from SKILL.md"


def test_pov_context_classification() -> None:
    """PovContext classifies itself as complete, partial, or blocked based on evidence."""
    complete = PovContext(
        prospect=Prospect(account_name="Acme"),  # type: ignore[call-arg]
        business_objectives=[BusinessObjective(objective="O1")],  # type: ignore[call-arg]
        technical_scope={
            "sources": [TechnicalSystem(name="Postgres")],  # type: ignore[call-arg]
            "destinations": [],
            "use_cases": [],
            "requirements": [],
            "dependencies": [],
        },
        success_criteria=[SuccessCriterion(feature_or_capability="X")],  # type: ignore[call-arg]
    )
    assert complete.status == "complete"

    partial = PovContext(
        prospect=Prospect(account_name="Acme"),  # type: ignore[call-arg]
        business_objectives=[BusinessObjective(objective="O1")],  # type: ignore[call-arg]
    )
    assert partial.status == "partial"

    blocked = PovContext(prospect=Prospect(account_name="Acme"))  # type: ignore[call-arg]
    assert blocked.status == "blocked"


def test_find_connectors_classifies_systems() -> None:
    """Connector extraction recognizes common source/destination aliases and explicit backticks."""
    text = "Use `source-postgres` and `destination-snowflake`, also Salesforce and S3."
    found = _find_connectors(text)
    names = {n for n, _ in found}
    assert "Postgres" in names
    assert "Snowflake" in names
    assert "Salesforce" in names
    assert "S3" in names

    assert _classify_system("Postgres", text, "source") == "source"
    assert _classify_system("Snowflake", text, "destination") == "destination"
    assert _classify_system("Salesforce", "destination-salesforce", "destination") == "destination"


def test_resolve_config_and_paths(tmp_path: Path) -> None:
    """The config resolver finds .se-config.yaml and derives workspace paths correctly."""
    cfg = {
        "workspace_root": str(tmp_path),
        "pov_gsheet": {
            "template_url": "https://docs.google.com/spreadsheets/d/TEMPLATE/edit",
            "drive_target_folder_url": "https://drive.google.com/drive/folders/FOLDER",
            "se_name": "Gary Yang",
            "se_title": "Solutions Engineer",
        },
        "salesforce": {"enabled": False},
    }
    (tmp_path / ".se-config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    ws, config = _resolve_se_config(str(tmp_path))
    assert ws == tmp_path
    assert config["pov_gsheet"]["se_name"] == "Gary Yang"

    paths = _derive_paths(config, ws)
    assert paths["customers_dir"] == tmp_path / "customers"
    assert paths["transcripts_dir"] == tmp_path / "customers" / "_transcripts"
    assert paths["notes_dir"] == tmp_path / "notes"


def test_legacy_relative_layout_paths(tmp_path: Path) -> None:
    """Relative layout overrides are resolved against the workspace root."""
    cfg = {
        "workspace_root": str(tmp_path),
        "layout": {
            "customers_dir": "01-customers",
            "transcripts_dir": "01-customers/_transcripts",
            "notes_dir": "04-notes",
        },
        "salesforce": {"enabled": False},
    }
    (tmp_path / ".se-config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    ws, config = _resolve_se_config(str(tmp_path))
    paths = _derive_paths(config, ws)
    assert paths["customers_dir"] == tmp_path / "01-customers"
    assert paths["transcripts_dir"] == tmp_path / "01-customers" / "_transcripts"


def _write_deal_assessment(customers_dir: Path, account: str, opp: str | None) -> None:
    """Create a fake deal-assessment output that exercises extraction."""
    if opp:
        root = customers_dir / account / "opportunities" / opp / "outputs" / "deal-assessment"
    else:
        root = customers_dir / account / "outputs" / "deal-assessment"
    root.mkdir(parents=True)
    md = root / "deal-assessment-2026-07-10.md"
    md.write_text(
        textwrap.dedent(
            """\
            # TestCo — Deal Assessment
            **Date:** July 10, 2026 · **Status:** updated

            ### At a Glance
            - **Verdict:** viable
            - **Probability:** medium

            ## Driver
            The VP of Data wants to replace brittle Fivetran pipelines.

            ## Need
            Reliable hourly syncs from Salesforce, Postgres, and NetSuite into Snowflake.

            ## What Would Close It
            - A clean InfoSec sign-off on the Enterprise Flex VPC architecture.
            - A 2-week POV that proves reliability on Postgres and Salesforce.
            - Mutual close plan with the VP of Data and CFO.

            ## Source Coverage
            - deal-assessment
            """
        ),
        encoding="utf-8",
    )


def _write_connector_feasibility(customers_dir: Path, account: str, opp: str | None) -> None:
    if opp:
        root = customers_dir / account / "opportunities" / opp / "outputs" / "connector-feasibility"
    else:
        root = customers_dir / account / "outputs" / "connector-feasibility"
    root.mkdir(parents=True)
    md = root / "connector-feasibility-2026-07-10.md"
    md.write_text(
        textwrap.dedent(
            """\
            # TestCo — Connector Feasibility
            **Date:** July 10, 2026 · **Status:** feasible

            ### At a Glance
            - **Feasibility:** supported

            ## Connector Coverage
            - `source-postgres` is certified.
            - `source-salesforce` is certified.
            - `destination-snowflake` is certified.

            ## Source Coverage
            - connector-feasibility
            """
        ),
        encoding="utf-8",
    )


def test_context_builder_gathers_workspace_data(tmp_path: Path) -> None:
    """build_context extracts business objectives, systems, and success criteria from outputs."""
    cfg = {
        "workspace_root": str(tmp_path),
        "pov_gsheet": {
            "template_url": "https://docs.google.com/spreadsheets/d/TEMPLATE/edit",
            "drive_target_folder_url": "https://drive.google.com/drive/folders/FOLDER",
            "se_name": "Gary Yang",
            "se_title": "Solutions Engineer",
        },
        "salesforce": {"enabled": False},
    }
    (tmp_path / ".se-config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _write_deal_assessment(tmp_path / "customers", "TestCo", "default")
    _write_connector_feasibility(tmp_path / "customers", "TestCo", "default")

    ctx = build_context(account="TestCo", opp="default", workspace_arg=str(tmp_path))

    assert ctx.prospect.account_name == "TestCo"
    assert ctx.prospect.se_name == "Gary Yang"
    assert ctx.status == "complete"

    business = {o.objective for o in ctx.business_objectives}
    assert "The VP of Data wants to replace brittle Fivetran pipelines." in business

    source_names = {s.name for s in ctx.technical_scope["sources"]}
    dest_names = {s.name for s in ctx.technical_scope["destinations"]}
    assert "Postgres" in source_names
    assert "Salesforce" in source_names
    assert "NetSuite" in source_names
    assert "Snowflake" in dest_names

    assert any("InfoSec sign-off" in c.feature_or_capability for c in ctx.success_criteria)

    # Evidence lineage: Snowflake should cite both outputs it appeared in.
    snowflake = next(s for s in ctx.technical_scope["destinations"] if s.name == "Snowflake")
    assert len(snowflake.sources) == 2


def test_context_builder_no_outputs_is_blocked(tmp_path: Path) -> None:
    """Without any prior outputs, the context is blocked and reports why."""
    cfg = {
        "workspace_root": str(tmp_path),
        "pov_gsheet": {
            "template_url": "https://docs.google.com/spreadsheets/d/TEMPLATE/edit",
            "drive_target_folder_url": "https://drive.google.com/drive/folders/FOLDER",
            "se_name": "Gary Yang",
            "se_title": "Solutions Engineer",
        },
        "salesforce": {"enabled": False},
    }
    (tmp_path / ".se-config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "customers" / "TestCo").mkdir(parents=True)

    ctx = build_context(account="TestCo", workspace_arg=str(tmp_path))
    assert ctx.status == "blocked"
    assert any("prior skill outputs" in w for w in ctx.warnings)


def test_source_coverage_tracks_material_sources(tmp_path: Path) -> None:
    """source_coverage lists which inputs were checked, available, and material."""
    cfg = {
        "workspace_root": str(tmp_path),
        "pov_gsheet": {
            "template_url": "https://docs.google.com/spreadsheets/d/TEMPLATE/edit",
            "drive_target_folder_url": "https://drive.google.com/drive/folders/FOLDER",
            "se_name": "Gary Yang",
            "se_title": "Solutions Engineer",
        },
        "salesforce": {"enabled": False},
    }
    (tmp_path / ".se-config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _write_connector_feasibility(tmp_path / "customers", "TestCo", None)

    ctx = build_context(account="TestCo", workspace_arg=str(tmp_path))
    sources = {s.source for s in ctx.source_coverage}
    assert "prior skill outputs" in sources
    assert "workspace transcripts" in sources
    assert "salesforce" in sources

    prior = next(s for s in ctx.source_coverage if s.source == "prior skill outputs")
    assert prior.available is True
    assert prior.material is True


def test_output_schema_pov_gsheet_registered() -> None:
    """The pov-gsheet output schema requires a receipt and a google-sheet-url/status At a Glance."""
    from output_schema import _SKILL_SCHEMAS

    schema = _SKILL_SCHEMAS["pov-gsheet"]
    assert "receipt" in schema.required_sections
    assert "source-coverage" in schema.required_sections
    assert "google-sheet-url" in schema.required_at_a_glance_labels
    assert "status" in schema.required_at_a_glance_labels


def test_pov_gsheet_permission_profile_declared(repo_root: Path) -> None:
    """The webapp grants write + shell permissions to pov-gsheet (it touches files and the browser)."""
    svc_py = (repo_root / "webapp" / "services" / "skill_runtime_service.py").read_text(encoding="utf-8")
    assert '"pov-gsheet"' in svc_py
    # The SKILL_PERMISSIONS line must grant write and shell, and not grant git.
    m = re.search(r'"pov-gsheet":\s*PermissionProfile\(([^)]+)\)', svc_py)
    assert m, "pov-gsheet permission profile not found"
    profile = m.group(1)
    assert "write=True" in profile
    assert "shell=True" in profile
    assert "git=False" in profile


def test_runner_dry_run_generates_plan(repo_root: Path, minimal_context: dict, tmp_path: Path) -> None:
    """The optional Node helper produces a dry-run plan with all seven tabs."""
    ctx_file = tmp_path / "ctx.json"
    ctx_file.write_text(json.dumps(minimal_context), encoding="utf-8")
    receipt_file = tmp_path / "receipt.json"

    runner = repo_root / "webapp" / "scripts" / "pov-gsheet-runner.mjs"
    subprocess.run(
        [
            "node",
            str(runner),
            "--context",
            str(ctx_file),
            "--template-url",
            "https://docs.google.com/spreadsheets/d/TEMPLATE_ID/edit",
            "--drive-folder-url",
            "https://drive.google.com/drive/folders/FOLDER_ID",
            "--copy-title",
            "Airbyte || TestCo - POV Success Criteria",
            "--dry-run",
            "--out-receipt",
            str(receipt_file),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert receipt["status"] == "dry-run"
    assert receipt["skill"] == "pov-gsheet"
    assert "copy_url" in receipt
    assert "TEMPLATE_ID" in receipt["copy_url"]
    assert "FOLDER_ID" in receipt["copy_url"]
    assert "TestCo" in receipt["copy_url"]

    plan_tabs = receipt["plan"]["tabs"]
    for tab in EXPECTED_TAB_NAMES:
        assert tab in plan_tabs, f"Plan missing tab {tab}"
        assert "startCell" in plan_tabs[tab]
        assert "tsv" in plan_tabs[tab]


def _minimal_config(tmp_path: Path) -> None:
    cfg = {
        "workspace_root": str(tmp_path),
        "pov_gsheet": {
            "template_url": "https://docs.google.com/spreadsheets/d/TEMPLATE/edit",
            "drive_target_folder_url": "https://drive.google.com/drive/folders/FOLDER",
            "se_name": "Gary Yang",
            "se_title": "Solutions Engineer",
        },
        "salesforce": {"enabled": False},
    }
    (tmp_path / ".se-config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_salesforce_evidence_populates_prospect_and_contacts(tmp_path: Path) -> None:
    """External Salesforce evidence merges account, opportunity, and contact facts."""
    _minimal_config(tmp_path)
    (tmp_path / "customers" / "TestCo").mkdir(parents=True)

    evidence = [
        {
            "source": "salesforce",
            "source_id": "001abc",
            "retrieved_at": "2026-07-14T10:00:00Z",
            "account_name": "TestCo",
            "opportunity_name": "TestCo - POV",
            "direct_customer": False,
            "fact_type": "prospect",
            "fact": {
                "opportunity_name": "TestCo - POV",
                "stage": "Tech Eval",
                "owner": "Alice AE",
                "next_step": "Schedule POV kickoff",
            },
            "status": "ok",
        },
        {
            "source": "salesforce",
            "source_id": "003abc",
            "retrieved_at": "2026-07-14T10:00:00Z",
            "account_name": "TestCo",
            "direct_customer": False,
            "fact_type": "contact",
            "fact": {"name": "Bob Prospect", "email": "bob@testco.com", "title": "VP Data", "side": "Customer"},
            "status": "ok",
        },
    ]
    sf_file = tmp_path / "salesforce-evidence.json"
    sf_file.write_text(json.dumps(evidence), encoding="utf-8")

    ctx = build_context(
        account="TestCo",
        workspace_arg=str(tmp_path),
        external_evidence_paths={"salesforce": sf_file},
    )

    assert ctx.prospect.opportunity_name == "TestCo - POV"
    assert ctx.prospect.stage == "Tech Eval"
    assert ctx.prospect.owner == "Alice AE"
    assert any(c.name == "Bob Prospect" and c.email == "bob@testco.com" for c in ctx.contacts["prospect"])
    sf_cov = next(s for s in ctx.source_coverage if s.source == "salesforce")
    assert sf_cov.available is True
    assert sf_cov.material is True
    assert sf_cov.status == "searched"


def test_gong_transcript_evidence_extracts_systems(tmp_path: Path) -> None:
    """A Gong transcript fact has connectors extracted into technical_scope."""
    _minimal_config(tmp_path)
    (tmp_path / "customers" / "TestCo").mkdir(parents=True)

    evidence = [
        {
            "source": "gong",
            "source_id": "call-123",
            "retrieved_at": "2026-07-14T10:00:00Z",
            "account_name": "TestCo",
            "direct_customer": True,
            "fact_type": "transcript",
            "fact": {
                "text": "We need to replicate `source-postgres` and `source-salesforce` into `destination-snowflake`. Also the team uses `source-netsuite`."
            },
            "status": "ok",
        }
    ]
    gong_file = tmp_path / "gong-evidence.json"
    gong_file.write_text(json.dumps(evidence), encoding="utf-8")

    ctx = build_context(
        account="TestCo",
        workspace_arg=str(tmp_path),
        external_evidence_paths={"gong": gong_file},
    )

    source_names = {s.name for s in ctx.technical_scope["sources"]}
    dest_names = {s.name for s in ctx.technical_scope["destinations"]}
    assert "Postgres" in source_names
    assert "Salesforce" in source_names
    assert "NetSuite" in source_names
    assert "Snowflake" in dest_names
    gong_cov = next(s for s in ctx.source_coverage if s.source == "gong")
    assert gong_cov.available is True
    assert gong_cov.material is True


def test_granola_evidence_adds_business_objective(tmp_path: Path) -> None:
    """External meeting-notes evidence contributes business objectives."""
    _minimal_config(tmp_path)
    (tmp_path / "customers" / "TestCo").mkdir(parents=True)

    evidence = [
        {
            "source": "granola",
            "source_id": "meeting-456",
            "retrieved_at": "2026-07-14T10:00:00Z",
            "account_name": "TestCo",
            "direct_customer": True,
            "fact_type": "business_objective",
            "fact": {"objective": "Reduce pipeline maintenance by 30%", "desired_outcome": "Free up two FTEs"},
            "status": "ok",
        }
    ]
    granola_file = tmp_path / "granola-evidence.json"
    granola_file.write_text(json.dumps(evidence), encoding="utf-8")

    ctx = build_context(
        account="TestCo",
        workspace_arg=str(tmp_path),
        external_evidence_paths={"granola": granola_file},
    )

    objectives = {o.objective for o in ctx.business_objectives}
    assert "Reduce pipeline maintenance by 30%" in objectives


def test_unavailable_external_source_is_recorded_not_searched(tmp_path: Path) -> None:
    """An unavailable MCP source produces an unavailable coverage entry, not a false search claim."""
    _minimal_config(tmp_path)
    (tmp_path / "customers" / "TestCo").mkdir(parents=True)

    evidence = [
        {
            "source": "gmail",
            "retrieved_at": "2026-07-14T10:00:00Z",
            "account_name": "TestCo",
            "direct_customer": False,
            "fact_type": "unknown",
            "status": "unavailable",
            "note": "Gmail MCP not configured",
        }
    ]
    gmail_file = tmp_path / "gmail-evidence.json"
    gmail_file.write_text(json.dumps(evidence), encoding="utf-8")

    ctx = build_context(
        account="TestCo",
        workspace_arg=str(tmp_path),
        external_evidence_paths={"gmail": gmail_file},
    )

    gmail_cov = next(s for s in ctx.source_coverage if s.source == "gmail")
    assert gmail_cov.available is False
    assert gmail_cov.status == "unavailable"
    assert "not configured" in gmail_cov.note
    assert not any(s.source in {"gong", "slack"} for s in ctx.source_coverage)


def test_conflict_resolution_prefers_direct_customer(tmp_path: Path) -> None:
    """When sources disagree, prefer the direct and more recent customer statement."""
    _minimal_config(tmp_path)
    (tmp_path / "customers" / "TestCo").mkdir(parents=True)

    evidence = [
        {
            "source": "salesforce",
            "retrieved_at": "2026-07-13T10:00:00Z",
            "direct_customer": False,
            "fact_type": "prospect",
            "fact": {"stage": "Discovery"},
            "status": "ok",
        },
        {
            "source": "gong",
            "retrieved_at": "2026-07-14T10:00:00Z",
            "direct_customer": True,
            "fact_type": "prospect",
            "fact": {"stage": "Tech Eval"},
            "status": "ok",
        },
    ]
    ev_file = tmp_path / "conflict-evidence.json"
    ev_file.write_text(json.dumps(evidence), encoding="utf-8")

    ctx = build_context(
        account="TestCo",
        workspace_arg=str(tmp_path),
        external_evidence_paths={"external": ev_file},
    )

    assert ctx.prospect.stage == "Tech Eval"
    assert any("Prospect conflict on stage" in w for w in ctx.warnings)


def test_external_contact_dedup_merges_emails_and_sources(tmp_path: Path) -> None:
    """Duplicate contacts across sources are merged, preserving the richest record."""
    contacts = [
        Contact(name="Alice", email="alice@testco.com", source="salesforce"),
        Contact(name="Alice", role="VP Data", source="gong"),
    ]
    merged = _dedupe_contacts(contacts)
    assert len(merged) == 1
    assert merged[0].email == "alice@testco.com"
    assert merged[0].role == "VP Data"
    assert merged[0].source == "salesforce"
    assert "gong" in (merged[0].notes or "")


def test_dedupe_by_removes_duplicate_objects() -> None:
    """_dedupe_by removes objects with duplicate keys while preserving first occurrence."""
    items = [
        TechnicalSystem(name="Postgres", kind="source"),
        TechnicalSystem(name="Postgres", kind="destination"),
        TechnicalSystem(name="Snowflake", kind="destination"),
    ]
    unique = _dedupe_by(items, lambda s: s.name.lower())
    assert len(unique) == 2
    assert unique[0].kind == "source"


def test_load_external_evidence_skips_invalid_entries(tmp_path: Path) -> None:
    """Malformed evidence items are dropped without failing the whole file."""
    data = [
        {"source": "salesforce", "fact_type": "prospect", "fact": {"stage": "Open"}, "status": "ok"},
        {"source": "gong"},  # missing required fact_type
        "not-an-object",
    ]
    ev_file = tmp_path / "evidence.json"
    ev_file.write_text(json.dumps(data), encoding="utf-8")
    loaded = _load_external_evidence(ev_file)
    assert len(loaded) == 1
    assert loaded[0].source == "salesforce"
