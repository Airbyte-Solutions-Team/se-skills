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

Customer stakeholders described the current process, measurable pain, decision
criteria, timeline, budget context, implementation risks, and the agreed next
steps for validating these assumptions with the buying team.
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
    assert plan.warnings == []
    assert plan.choices == [{
        "id": "run-quals:tech-qual",
        "skills": ["tech-qual"],
        "message": "Missing qualification doc(s): tech-qual. POC Plan will offer to run them first, or proceed with a scope-drift warning if you skip.",
    }]


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
    ]
    assert roi.choices == [{
        "id": "run-quals:biz-qual",
        "skills": ["biz-qual"],
        "message": "No biz-qual found — its Metrics are the inputs to the ROI case; the skill will offer to run it first.",
    }]
    assert close.ready is True
    assert close.missing == []
    assert close.warnings == [
        "No local transcript found. Mutual Close Plan will check Gong; it requires the customer's actual buying process and will stop if no customer voice exists.",
    ]
    assert close.choices == [{
        "id": "run-quals:biz-qual",
        "skills": ["biz-qual"],
        "message": "No biz-qual found — its Paper Process and Economic Buyer sections are the backbone of a close plan; the skill will offer to run it first.",
    }]


@pytest.mark.parametrize(
    "text",
    ["", " \n\t ", "plain text without a heading", "# Notes\njunk"],
)
def test_deal_assessment_ignores_unusable_qualification_docs(
    tmp_path: Path, text: str,
) -> None:
    customers = tmp_path / "customers"
    _write_output(customers, "Acme", None, "biz-qual", "prior.md", text)
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", None, customers)
    assert "No local transcript or qualification doc found." in plan.warnings[0]


def test_deal_assessment_rejects_invalid_utf8_qualification_doc(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    path = customers / "Acme" / "outputs" / "biz-qual" / "invalid.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# Notes\n" + b"\xff\xfe")
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", None, customers)
    assert "No local transcript or qualification doc found." in plan.warnings[0]


def test_deal_assessment_accepts_realistic_legacy_qualification_doc(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    body = "\n".join(
        [
            "# Legacy Business Qualification",
            "",
            "Customer stakeholders described the current process, measurable pain, "
            "decision criteria, timeline, budget context, and implementation risks. " * 4,
            "",
            "## Metrics",
            "The team reported recurring manual work and agreed to validate the baseline "
            "during the next discovery session. " * 3,
        ]
    )
    _write_output(customers, "Acme", None, "biz-qual", "legacy.md", body)
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", None, customers)
    assert "using prior qualification doc(s) as source" in plan.warnings[0]


def test_deal_assessment_scopes_qualification_docs_to_selected_opportunity(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_output(customers, "Acme", "other", "biz-qual", "prior.md", VALID_BIZ_QUAL)
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", "intro", customers)
    assert "No local transcript or qualification doc found." in plan.warnings[0]


@pytest.mark.parametrize("skill_dir", ["deployment-qual", "deployment-model-qual"])
def test_deal_assessment_accepts_deployment_qualification_doc(
    tmp_path: Path, skill_dir: str,
) -> None:
    customers = tmp_path / "customers"
    body = (
        "# Deployment Qual\n\n"
        "The customer described deployment ownership, networking constraints, "
        "security review, environments, operational support, and rollout timing. " * 5
    )
    _write_output(
        customers, "Acme", "intro", skill_dir, "prior.md",
        body,
    )
    plan = orchestrator.check_prerequisites("deal-assessment", "Acme", "intro", customers)
    assert plan.warnings == [
        "No local transcript found; using prior qualification doc(s) as source. Deal Assessment will flag thin sources."
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
        for call in self.launch_calls:
            if call["sig"] == sig:
                return "job-123", call
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
    ]
    assert data["choices"] == [{
        "id": "run-quals:biz-qual,tech-qual",
        "skills": ["biz-qual", "tech-qual"],
        "message": "Missing qualification doc(s): biz-qual, tech-qual. POC Plan will offer to run them first, or proceed with a scope-drift warning if you skip.",
    }]


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


def test_api_invoke_poc_plan_requires_choice_before_launch(monkeypatch, tmp_path: Path) -> None:
    svc, job_svc = _runtime_svc(tmp_path)
    _write_transcript(tmp_path / "customers", "Acme", "Acme-07.14.26.txt", "call")

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
    ))
    assert result["blocked"] is True
    assert result["choices"][0]["id"] == "run-quals:biz-qual,tech-qual"
    assert job_svc.launch_calls == []


def test_api_invoke_poc_plan_records_acknowledged_choice(monkeypatch, tmp_path: Path) -> None:
    svc, job_svc = _runtime_svc(tmp_path)
    _write_transcript(tmp_path / "customers", "Acme", "Acme-07.14.26.txt", "call")
    choice_id = "run-quals:biz-qual,tech-qual"

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
        acknowledged_choices=[choice_id],
    ))
    assert result.get("job_id")
    assert len(job_svc.launch_calls) == 1
    assert (
        "The SE explicitly chose to skip running the missing upstream skill(s) "
        "(biz-qual, tech-qual) first."
    ) in job_svc.launch_calls[0]["prompt"]
    assert job_svc.launch_calls[0]["meta"]["acknowledged_choices"] == [choice_id]


@pytest.mark.parametrize(
    ("skill", "choice_id"),
    [
        ("roi-business-case", "run-quals:biz-qual"),
        ("mutual-close-plan", "run-quals:biz-qual"),
    ],
)
def test_api_invoke_late_stage_skills_require_and_accept_choice(
    tmp_path: Path, skill: str, choice_id: str,
) -> None:
    svc, job_svc = _runtime_svc(tmp_path)
    _write_transcript(tmp_path / "customers", "Acme", "Acme-07.14.26.txt", "call")

    blocked = asyncio.run(svc.invoke(
        account="Acme",
        skill=skill,
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
    ))
    assert blocked["blocked"] is True
    assert blocked["choices"][0]["id"] == choice_id
    assert job_svc.launch_calls == []

    launched = asyncio.run(svc.invoke(
        account="Acme",
        skill=skill,
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
        acknowledged_choices=[choice_id],
    ))
    assert launched.get("job_id")
    assert job_svc.launch_calls[-1]["meta"]["acknowledged_choices"] == [choice_id]


@pytest.mark.parametrize(
    ("skill", "choice_id"),
    [
        ("poc-plan", "run-quals:biz-qual,tech-qual"),
        ("roi-business-case", "run-quals:biz-qual"),
        ("mutual-close-plan", "run-quals:biz-qual"),
    ],
)
def test_api_invoke_override_does_not_bypass_choice(
    tmp_path: Path, skill: str, choice_id: str,
) -> None:
    svc, job_svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill=skill,
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=True,
        approve_permissions=True,
    ))
    assert result["blocked"] is True
    assert result["choices"][0]["id"] == choice_id
    assert job_svc.launch_calls == []

    acknowledged = asyncio.run(svc.invoke(
        account="Acme",
        skill=skill,
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=True,
        approve_permissions=True,
        acknowledged_choices=[choice_id],
    ))
    assert acknowledged.get("job_id")
    assert len(job_svc.launch_calls) == 1
    assert job_svc.launch_calls[0]["meta"]["acknowledged_choices"] == [choice_id]


def test_api_invoke_ignores_forged_choice_ids(tmp_path: Path) -> None:
    svc, job_svc = _runtime_svc(tmp_path)
    valid_id = "run-quals:biz-qual,tech-qual"
    forged_id = "run-quals:bogus"

    blocked = asyncio.run(svc.invoke(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=True,
        approve_permissions=True,
        acknowledged_choices=[forged_id],
    ))
    assert blocked["blocked"] is True
    assert blocked["choices"][0]["id"] == valid_id
    assert job_svc.launch_calls == []

    launched = asyncio.run(svc.invoke(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=True,
        approve_permissions=True,
        acknowledged_choices=[valid_id, forged_id],
    ))
    assert launched.get("job_id")
    call = job_svc.launch_calls[-1]
    assert call["meta"]["acknowledged_choices"] == [valid_id]
    assert forged_id not in call["prompt"]


def test_api_invoke_signature_includes_accepted_choice_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    svc, job_svc = _runtime_svc(tmp_path)
    first_id = "run-quals:biz-qual"
    second_id = "run-quals:tech-qual"
    plans = iter([
        orchestrator.PlanResult(
            skill="poc-plan",
            ready=True,
            choices=[{"id": first_id, "skills": ["biz-qual"], "message": "skip biz"}],
        ),
        orchestrator.PlanResult(
            skill="poc-plan",
            ready=True,
            choices=[
                {"id": first_id, "skills": ["biz-qual"], "message": "skip biz"},
                {"id": second_id, "skills": ["tech-qual"], "message": "skip tech"},
            ],
        ),
        orchestrator.PlanResult(
            skill="poc-plan",
            ready=True,
            choices=[
                {"id": first_id, "skills": ["biz-qual"], "message": "skip biz"},
                {"id": second_id, "skills": ["tech-qual"], "message": "skip tech"},
            ],
        ),
    ])
    monkeypatch.setattr(orchestrator, "check_prerequisites", lambda *args: next(plans))
    first = asyncio.run(svc.invoke(
        account="Acme", skill="poc-plan", opportunity="intro", opp_slug="intro",
        extra=None, freeform=None, override_prerequisites=True, approve_permissions=True,
        acknowledged_choices=[first_id],
    ))
    assert first.get("job_id")
    second = asyncio.run(svc.invoke(
        account="Acme", skill="poc-plan", opportunity="intro", opp_slug="intro",
        extra=None, freeform=None, override_prerequisites=True, approve_permissions=True,
        acknowledged_choices=[first_id, second_id],
    ))
    assert second.get("job_id")
    assert len(job_svc.launch_calls) == 2
    third = asyncio.run(svc.invoke(
        account="Acme", skill="poc-plan", opportunity="intro", opp_slug="intro",
        extra=None, freeform=None, override_prerequisites=True, approve_permissions=True,
        acknowledged_choices=[second_id, first_id],
    ))
    assert third["reused"] is True
    assert len(job_svc.launch_calls) == 2
