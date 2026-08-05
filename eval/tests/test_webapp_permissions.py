"""Tests for SEC-001: per-skill permission profiles and invoke approval gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.skill_runtime_service import SKILL_PERMISSIONS, SkillRuntimeService
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


def test_worker_analysis_blocked_without_permission_approval(tmp_path: Path):
    """worker-analysis cannot run until the backend approval gate succeeds."""
    svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="worker-analysis",
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=False,
    ))

    assert result["blocked"] is True
    assert "permissions" in result
    assert result["permissions"]["shell"] is True
    assert "job_id" not in result


def test_worker_analysis_permission_plan_shows_before_approval(tmp_path: Path):
    """Permission planning is returned before approval so the user can review it."""
    svc = _runtime_svc(tmp_path)

    plan = svc.permission_for("worker-analysis")
    assert plan["write"] is True
    assert plan["shell"] is True
    assert plan["git"] is False
    assert plan["requires_approval"] is True
    assert plan["permission_mode"] == "bypassPermissions"
    assert "runs shell commands" in plan["summary"]


def test_worker_analysis_runs_after_permission_approval(tmp_path: Path):
    """A user can invoke worker-analysis only after explicitly approving permissions."""
    svc = _runtime_svc(tmp_path)

    result = asyncio.run(svc.invoke(
        account="Acme",
        skill="worker-analysis",
        opportunity=None,
        opp_slug=None,
        extra="questionnaire mode",
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
    ))

    assert result.get("job_id")
    assert "blocked" not in result
    # Launch meta must carry the bypass permission mode for non-interactive shell use.
    launch = svc.job_service.launch_calls[-1]
    assert launch["meta"]["permission_mode"] == "bypassPermissions"


def test_worker_analysis_questionnaire_mode_has_only_required_permissions(tmp_path: Path):
    """worker-analysis only needs write (output) and shell (toolkit); no git."""
    svc = _runtime_svc(tmp_path)
    plan = svc.permission_for("worker-analysis")
    assert plan["write"] is True
    assert plan["shell"] is True
    assert plan["git"] is False


def test_only_allowlisted_shell_skills_receive_bypass_permissions(tmp_path: Path):
    """bypassPermissions is limited to the reviewed allowlist."""
    svc = _runtime_svc(tmp_path)
    # freeform is a special pseudo-skill that is not discovered from disk.
    for sid in ("connector-feasibility", "pov-gsheet", "worker-analysis"):
        plan = svc.permission_for(sid)
        assert plan["shell"] is True
        assert plan["permission_mode"] == "bypassPermissions"

    freeform = svc._permission_profile(None, freeform=True)
    assert freeform.shell is True
    assert freeform.permission_mode == "bypassPermissions"

    for sid in ("prep-call", "biz-qual", "next-move", "post-call"):
        plan = svc.permission_for(sid)
        assert plan["shell"] is False
        assert plan["permission_mode"] == "auto"


def test_unknown_skill_cannot_obtain_bypass_permissions_by_declaring_shell(tmp_path: Path, monkeypatch):
    """Skill metadata alone (a declared shell=True) does not grant bypassPermissions."""
    from services.skill_runtime_service import PermissionProfile

    svc = _runtime_svc(tmp_path)
    # Pretend an untrusted skill is declared with shell=True but is not in the allowlist.
    monkeypatch.setitem(SKILL_PERMISSIONS, "untrusted-skill", PermissionProfile(write=True, shell=True, git=False))
    profile = svc._permission_profile("untrusted-skill")
    assert profile.shell is True
    assert profile.permission_mode == "auto", "shell alone must not grant bypassPermissions"


def test_non_shell_skills_retain_write_only_permission_behavior(tmp_path: Path):
    """Existing non-shell skills keep write-only auto permission mode."""
    svc = _runtime_svc(tmp_path)
    plan = svc.permission_for("prep-call")
    assert plan["write"] is True
    assert plan["shell"] is False
    assert plan["git"] is False
    assert plan["permission_mode"] == "auto"
    assert plan["requires_approval"] is True


def test_invoke_block_leaves_service_usable(tmp_path: Path):
    """A rejected permission plan prevents invocation but does not break later calls."""
    svc = _runtime_svc(tmp_path)

    blocked = asyncio.run(svc.invoke(
        account="Acme",
        skill="worker-analysis",
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=False,
    ))
    assert blocked["blocked"] is True
    assert "job_id" not in blocked

    approved = asyncio.run(svc.invoke(
        account="Acme",
        skill="worker-analysis",
        opportunity=None,
        opp_slug=None,
        extra=None,
        freeform=None,
        override_prerequisites=False,
        approve_permissions=True,
    ))
    assert approved.get("job_id")
    assert "blocked" not in approved
