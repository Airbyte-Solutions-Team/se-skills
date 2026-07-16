"""Tests for SEC-001: per-skill permission profiles and invoke approval gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.skill_runtime_service import SkillRuntimeService
from webapp import config as app_config


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


def _runtime_svc(tmp_path: Path) -> SkillRuntimeService:
    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    return SkillRuntimeService(
        customers_dir=customers,
        workspace=tmp_path,
        output_service=_FakeOutputService(customers),
        job_service=_FakeJobService(),
        se_config=app_config._se_config,
        se_config_clear=app_config._se_config_clear,
        safe_name=lambda n: n,
        skills_dir=app_config.SUITE_SKILLS_DIR,
        skills_dirs=app_config.SKILLS_DIRS,
    )


@pytest.mark.parametrize(
    "skill,freeform,expected_write,expected_shell,expected_git",
    [
        pytest.param("prep-call", False, True, False, False, id="prep-call-write-only"),
        pytest.param("biz-qual", False, True, False, False, id="biz-qual-write-only"),
        pytest.param("connector-feasibility", False, True, True, True, id="connector-feasibility-shell-git"),
        pytest.param("full-qual", False, True, False, False, id="full-qual-write-only"),
        pytest.param("next-move", False, True, False, False, id="next-move-write-only"),
        pytest.param(None, True, True, True, True, id="freeform-broad"),
    ],
)
def test_permission_profile_classifies_skills(skill, freeform, expected_write, expected_shell, expected_git):
    svc = _runtime_svc(Path("/tmp"))
    profile = svc._permission_profile(skill, freeform=freeform)
    data = profile.model_dump()
    assert data["write"] is expected_write
    assert data["shell"] is expected_shell
    assert data["git"] is expected_git
    assert data["requires_approval"] is True
    assert data["summary"]


def test_permission_profile_defaults_unknown_skill_to_write():
    svc = _runtime_svc(Path("/tmp"))
    profile = svc._permission_profile("not-a-skill")
    data = profile.model_dump()
    assert data["write"] is True
    assert data["shell"] is False
    assert data["git"] is False
    assert data["requires_approval"] is True


def test_api_permissions_returns_profile_for_known_skill():
    svc = _runtime_svc(Path("/tmp"))
    data = svc.permission_for("connector-feasibility")
    assert data["write"] is True
    assert data["shell"] is True
    assert data["git"] is True
    assert data["requires_approval"] is True
    assert "summary" in data


def test_api_permissions_returns_broad_profile_for_freeform():
    svc = _runtime_svc(Path("/tmp"))
    data = svc.permission_for(None, freeform=True)
    assert data["write"] is True
    assert data["shell"] is True
    assert data["git"] is True


def test_api_permissions_rejects_unknown_skill():
    svc = _runtime_svc(Path("/tmp"))
    with pytest.raises(Exception):
        svc.permission_for("not-a-skill")


def test_api_invoke_blocks_without_permission_approval(monkeypatch, tmp_path: Path):
    svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="prep-call",
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=False,
    ))

    assert result["blocked"] is True
    assert "permissions" in result
    assert result["permissions"]["write"] is True
    assert "job_id" not in result


def test_api_invoke_runs_after_permission_approval(monkeypatch, tmp_path: Path):
    svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="prep-call",
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
    ))

    assert result.get("job_id")
    assert "blocked" not in result


def test_api_invoke_blocks_freeform_without_permission_approval(monkeypatch, tmp_path: Path):
    svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill=None,
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform="Summarize the latest call",
        override_prerequisites=False,
        approve_permissions=False,
    ))

    assert result["blocked"] is True
    assert result["permissions"]["write"] is True
    assert result["permissions"]["shell"] is True
    assert result["permissions"]["git"] is True
    assert "job_id" not in result


def test_api_invoke_freeform_runs_after_permission_approval(monkeypatch, tmp_path: Path):
    svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill=None,
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform="Summarize the latest call",
        override_prerequisites=False,
        approve_permissions=True,
    ))

    assert result.get("job_id")
    assert "blocked" not in result
