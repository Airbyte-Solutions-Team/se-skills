"""Deterministic tests for ORCH-001: deterministic prerequisite planner.

Tests both the `orchestrator` module and the `GET /api/plan` / `POST /api/invoke`
routes that expose it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import output_schema
import orchestrator
import webapp.app as app


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


def test_api_plan_returns_prerequisite_status(monkeypatch, tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    _write_transcript(customers, "Acme", "Acme-07.14.26.txt", "call")

    data = app.api_plan(account="Acme", skill="biz-qual")
    assert data["skill"] == "biz-qual"
    assert data["ready"] is True
    assert data["can_override"] is True


def test_api_plan_blocks_poc_plan_without_upstream(monkeypatch, tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)

    data = app.api_plan(account="Acme", skill="poc-plan", opp_slug="intro")
    assert data["ready"] is False
    assert data["missing"]


def test_api_invoke_blocks_without_override(monkeypatch, tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(app, "JOBS", {})

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(app, "_save_jobs_snapshot", _noop)
    monkeypatch.setattr(app, "_run_job", lambda *args, **kwargs: None)
    monkeypatch.setattr("asyncio.create_task", lambda coro, *, name=None: None)

    body = app.InvokeBody(account="Acme", skill="poc-plan", opportunity="intro", opp_slug="intro")
    resp = asyncio.run(app.api_invoke(body))
    data = resp.body if hasattr(resp, "body") else resp
    # JSONResponse has a `.body` bytes attribute; decode it.
    if isinstance(data, bytes):
        import json
        data = json.loads(data)
    elif hasattr(data, "body"):
        data = json.loads(data.body)
    assert data["blocked"] is True
    assert "prerequisites" in data
    assert "job_id" not in data


def test_api_invoke_allows_override(monkeypatch, tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(app, "JOBS", {})

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(app, "_save_jobs_snapshot", _noop)
    monkeypatch.setattr(app, "_run_job", lambda *args, **kwargs: None)
    monkeypatch.setattr("asyncio.create_task", lambda coro, *, name=None: None)

    body = app.InvokeBody(
        account="Acme",
        skill="poc-plan",
        opportunity="intro",
        opp_slug="intro",
        override_prerequisites=True,
    )
    resp = asyncio.run(app.api_invoke(body))
    data = resp.body if hasattr(resp, "body") else resp
    if isinstance(data, bytes):
        import json
        data = json.loads(data)
    elif hasattr(data, "body"):
        data = json.loads(data.body)
    assert data.get("job_id")
    assert "blocked" not in data
