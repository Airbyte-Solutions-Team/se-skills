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


def test_check_prerequisites_biz_qual_requires_transcript(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    plan = orchestrator.check_prerequisites("biz-qual", "Acme", None, customers)
    assert plan.ready is False
    assert any("transcript" in m.lower() for m in plan.missing)


def test_check_prerequisites_biz_qual_ready_with_transcript(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    plan = orchestrator.check_prerequisites("biz-qual", "Acme", None, customers)
    assert plan.ready is True


def test_check_prerequisites_tech_qual_requires_biz_qual(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    plan = orchestrator.check_prerequisites("tech-qual", "Acme", None, customers)
    assert plan.ready is False
    assert any("biz-qual" in m for m in plan.missing)


def test_check_prerequisites_tech_qual_ready_with_valid_upstream(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    _write_output(customers, "Acme", None, "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL, valid=True)
    plan = orchestrator.check_prerequisites("tech-qual", "Acme", None, customers)
    assert plan.ready is True
    assert plan.upstream["biz-qual"].status == "valid"


def test_check_prerequisites_poc_plan_ready_with_biz_and_tech(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    _write_output(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL, valid=True)
    _write_output(customers, "Acme", "intro", "tech-qual", "tech-qual-2026-07-01.md", VALID_TECH_QUAL, valid=True)
    plan = orchestrator.check_prerequisites("poc-plan", "Acme", "intro", customers)
    assert plan.ready is True


def test_check_prerequisites_poc_plan_rejects_invalid_upstream(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")
    _write_output(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL, valid=True)
    _write_output(customers, "Acme", "intro", "tech-qual", "tech-qual-2026-07-01.md", VALID_TECH_QUAL, valid=False)
    plan = orchestrator.check_prerequisites("poc-plan", "Acme", "intro", customers)
    assert plan.ready is False
    assert any("tech-qual" in m for m in plan.missing)


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


def test_api_plan_blocks_poc_plan_without_upstream(monkeypatch, tmp_path: Path) -> None:
    svc, _ = _runtime_svc(tmp_path)

    data = svc.plan("poc-plan", "Acme", "intro")
    assert data["ready"] is False
    assert data["missing"]


def test_api_invoke_blocks_without_override(monkeypatch, tmp_path: Path) -> None:
    svc, _ = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=False,
    ))
    assert result["blocked"] is True
    assert "prerequisites" in result
    assert "job_id" not in result


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
