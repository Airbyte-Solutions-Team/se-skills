"""Deterministic tests for ORCH-001: deterministic prerequisite planner.

Tests both the `orchestrator` module and the `SkillRuntimeService` planning and
invocation methods that expose it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import orchestrator
import output_schema
from services.skill_runtime_service import SkillRuntimeService
from webapp import config as app_config


def _write_transcript(customers_dir: Path, account: str, name: str, text: str) -> Path:
    tdir = customers_dir / "_transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_output(customers_dir: Path, account: str, opp: str | None, skill: str, filename: str, text: str, valid: bool = True) -> Path:
    if opp:
        d = customers_dir / account / "opportunities" / opp / "outputs" / skill
    else:
        d = customers_dir / account / "outputs" / skill
    d.mkdir(parents=True, exist_ok=True)
    md = d / filename
    md.write_text(text, encoding="utf-8")
    meta = output_schema.parse_output(skill, text)
    # Force the validation_status for the test; parse_output may decide unvalidated
    # if the fixture does not match the contract, but we want explicit control.
    meta.valid = valid
    meta.validation_status = "valid" if valid else "invalid"
    output_schema.write_sidecar(md, meta)
    return md


VALID_BIZ_QUAL = """# Acme — biz-qual: viable

**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Verdict:** viable

## MEDDPICC Scorecard
| Letter | Status |
|---|---|
| M | green |

## Source Coverage
- synthetic
"""


VALID_TECH_QUAL = """# Acme — tech-qual: green

**Date:** 2026-07-01 · **Skill:** tech-qual

## At a Glance
- **Technical Fit:** green
- **Primary Risk:** none

## Technical Fit Summary
ok

## Source Coverage
- synthetic
"""


def test_check_prerequisites_prep_call_ready_without_data(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    plan = orchestrator.check_prerequisites("prep-call", "Acme", None, customers)
    assert plan.ready is True
    assert plan.can_override is True
    assert not plan.missing


@pytest.mark.parametrize(
    ("skill", "warning"),
    [
        (
            "biz-qual",
            "No local transcript found. Biz Qual will check Gong for customer calls before qualification and will stop if no customer voice is available.",
        ),
        (
            "deployment-model-qual",
            "No local transcript found. Deployment Model Qual will check Gong for customer calls; it needs customer answers to the 5 deployment questions (or answers you provide directly) and will stop if none are available.",
        ),
        (
            "tech-qual",
            "No local transcript found. Tech Qual will check Gong for calls with technical discovery and will stop if no technical discovery is available.",
        ),
        (
            "full-qual",
            "No local transcript found. Full Qual will check Gong; biz-qual and tech-qual each apply their own source requirements and report partial completion if one refuses.",
        ),
    ],
)
def test_source_resolvable_transcript_rules_are_advisory(
    tmp_path: Path, skill: str, warning: str,
) -> None:
    customers = tmp_path / "customers"
    plan = orchestrator.check_prerequisites(skill, "Acme", None, customers)
    assert plan.ready is True
    assert plan.missing == []
    assert plan.warnings == [warning]


def test_check_prerequisites_post_call_without_transcript_warns_about_gong(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    plan = orchestrator.check_prerequisites("post-call", "Acme", None, customers)
    assert plan.ready is True
    assert not plan.missing
    assert plan.warnings == [
        "No local transcript found for this account — post-call will search Gong for the most recent completed call and save it to _transcripts/ before analysis."
    ]


@pytest.mark.parametrize("extension", [".txt", ".rtf"])
def test_check_prerequisites_post_call_accepts_local_transcript_extensions(
    tmp_path: Path, extension: str,
) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", f"Acme-07.14.26{extension}", "call")
    plan = orchestrator.check_prerequisites("post-call", "Acme", None, customers)
    assert plan.ready is True
    assert not plan.warnings


def test_check_prerequisites_biz_qual_ready_with_transcript(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    plan = orchestrator.check_prerequisites("biz-qual", "Acme", None, customers)
    assert plan.ready is True


def test_check_prerequisites_tech_qual_does_not_require_biz_qual(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    plan = orchestrator.check_prerequisites("tech-qual", "Acme", None, customers)
    assert plan.ready is True
    assert "biz-qual" not in plan.missing
    assert plan.upstream == {}


def test_check_prerequisites_tech_qual_ignores_biz_qual_even_when_present(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    _write_output(customers, "Acme", None, "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL, valid=True)
    plan = orchestrator.check_prerequisites("tech-qual", "Acme", None, customers)
    assert plan.ready is True
    assert plan.upstream == {}


def test_check_prerequisites_poc_plan_ready_with_biz_and_tech(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    _write_output(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL, valid=True)
    _write_output(customers, "Acme", "intro", "tech-qual", "tech-qual-2026-07-01.md", VALID_TECH_QUAL, valid=True)
    plan = orchestrator.check_prerequisites("poc-plan", "Acme", "intro", customers)
    assert plan.ready is True
    assert plan.missing == []
    assert plan.warnings == []
    assert plan.upstream["biz-qual"].status == "valid"
    assert plan.upstream["tech-qual"].status == "valid"


def test_check_prerequisites_poc_plan_advises_on_invalid_upstream(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    _write_output(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL, valid=True)
    _write_output(customers, "Acme", "intro", "tech-qual", "tech-qual-2026-07-01.md", VALID_TECH_QUAL, valid=False)
    plan = orchestrator.check_prerequisites("poc-plan", "Acme", "intro", customers)
    assert plan.ready is True
    assert plan.missing == []
    assert plan.upstream["tech-qual"].status == "invalid"
    assert plan.warnings == [
        "Missing qualification doc(s): tech-qual. POC Plan will offer to run them first, or proceed with a scope-drift warning if you skip."
    ]


def test_check_prerequisites_deal_assessment_uses_qualification_doc(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_output(customers, "Acme", None, "biz-qual", "prior.md", VALID_BIZ_QUAL, valid=True)
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", None, customers)
    assert plan.ready is True
    assert plan.missing == []
    assert plan.warnings == [
        "No local transcript found; using prior qualification doc(s) as source. Deal Assessment will flag thin sources."
    ]


def test_check_prerequisites_deal_assessment_without_local_evidence_is_advisory(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", None, customers)
    assert plan.ready is True
    assert plan.missing == []
    assert plan.warnings == [
        "No local transcript or qualification doc found. Deal Assessment will check Gong for customer calls before assessing and will stop if no customer evidence is available."
    ]


def test_check_prerequisites_connector_feasibility_has_no_tech_gate(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    plan = orchestrator.check_prerequisites("connector-feasibility", "Acme", None, customers)
    assert plan.ready is True
    assert plan.missing == []
    assert plan.upstream == {}


def test_check_prerequisites_roi_and_close_are_source_resolvable(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    roi = orchestrator.check_prerequisites("roi-business-case", "Acme", None, customers)
    close = orchestrator.check_prerequisites("mutual-close-plan", "Acme", None, customers)
    assert roi.ready is True
    assert roi.missing == []
    assert roi.warnings == [
        "No local transcript found. ROI Business Case will check Gong; it requires customer-stated inputs and will stop if none exist.",
        "No biz-qual found — its Metrics are the inputs to the ROI case; the skill will offer to run it first.",
    ]
    assert close.ready is True
    assert close.missing == []
    assert close.warnings == [
        "No local transcript found. Mutual Close Plan will check Gong; it requires the customer's actual buying process and will stop if no customer voice exists.",
        "No biz-qual found — its Paper Process and Economic Buyer sections are the backbone of a close plan; the skill will offer to run it first.",
    ]


class _FakeOutputService:
    def __init__(self, customers_dir: Path) -> None:
        self.customers_dir = customers_dir

    def opp_outputs_dir(self, account: str, opp_slug: str) -> Path:
        d = self.customers_dir / account / "opportunities" / opp_slug / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d


class _FakeJobService:
    def __init__(self) -> None:
        self.jobs = {}
        self.launch_calls = []

    def find_reused_job(self, sig):
        return None

    async def launch(self, *, account, opp_slug, skill, opportunity, sig, prompt, meta):
        self.launch_calls.append({
            "account": account,
            "opp_slug": opp_slug,
            "skill": skill,
            "opportunity": opportunity,
            "sig": sig,
            "prompt": prompt,
            "meta": meta,
        })
        return "job-123", None


def _runtime_svc(tmp_path: Path) -> tuple[SkillRuntimeService, _FakeJobService]:
    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    output_svc = _FakeOutputService(customers)
    job_svc = _FakeJobService()
    return SkillRuntimeService(
        customers_dir=customers,
        workspace=tmp_path,
        output_service=output_svc,
        job_service=job_svc,
        se_config=app_config._se_config,
        se_config_clear=app_config._se_config_clear,
        safe_name=lambda n: n,
        skills_dir=app_config.SUITE_SKILLS_DIR,
        skills_dirs=app_config.SKILLS_DIRS,
    ), job_svc


def test_api_plan_returns_prerequisite_status(monkeypatch, tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    svc, _ = _runtime_svc(tmp_path)
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")

    data = svc.plan("biz-qual", "Acme", None)
    assert data["skill"] == "biz-qual"
    assert data["ready"] is True
    assert data["can_override"] is True


def test_api_plan_advises_poc_plan_without_upstream(monkeypatch, tmp_path: Path) -> None:
    svc, _ = _runtime_svc(tmp_path)

    data = svc.plan("poc-plan", "Acme", "intro")
    assert data["ready"] is True
    assert data["missing"] == []
    assert data["warnings"] == [
        "No local transcript found. POC Plan will check Gong; it refuses only if no customer voice exists in any source.",
        "Missing qualification doc(s): biz-qual, tech-qual. POC Plan will offer to run them first, or proceed with a scope-drift warning if you skip.",
    ]


def test_api_invoke_biz_qual_launches_without_prerequisite_override(monkeypatch, tmp_path: Path) -> None:
    svc, _ = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="biz-qual",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
    ))
    assert result.get("job_id")
    assert "blocked" not in result


def test_api_invoke_allows_override(monkeypatch, tmp_path: Path) -> None:
    svc, job_svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=True,
        approve_permissions=True,
    ))
    assert result.get("job_id")
    assert "blocked" not in result
    assert len(job_svc.launch_calls) == 1
    assert job_svc.launch_calls[0]["skill"] == "poc-plan"
