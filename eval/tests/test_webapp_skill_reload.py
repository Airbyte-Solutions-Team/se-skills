"""Deterministic tests for ARCH-007 runtime skill discovery refresh."""

import webapp.config as config
from services.skill_runtime_service import SkillRuntimeService
from webapp.config import _model_for, _se_config_clear


def _skill_runtime_service(skills_dir, workspace, customers_dir=None):
    return SkillRuntimeService(
        customers_dir=customers_dir or workspace,
        workspace=workspace,
        output_service=None,
        job_service=None,
        se_config=config._se_config,
        se_config_clear=config._se_config_clear,
        safe_name=lambda n: n,
        skills_dir=skills_dir,
        skills_dirs=[skills_dir],
    )


def test_discover_skills_finds_new_skill_folder(monkeypatch, tmp_path) -> None:
    """Adding a new SKILL.md under the configured skills dir makes it appear."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "new-skill").mkdir()
    (skills_dir / "new-skill" / "SKILL.md").write_text("---\nname: new-skill\n---\n# New skill\n")
    svc = _skill_runtime_service(skills_dir, tmp_path)
    ids = [s["id"] for s in svc.skills]
    assert "new-skill" in ids


def test_api_reload_clears_config_cache(monkeypatch, tmp_path) -> None:
    """POST /api/reload picks up a changed .se-config.yaml (model config)."""
    cfg = tmp_path / ".se-config.yaml"
    cfg.write_text("models:\n  default: claude-sonnet-4-6\n")
    monkeypatch.setattr(config, "SE_CONFIG", cfg)
    _se_config_clear()
    assert _model_for("quick-ask") == "claude-sonnet-4-6"

    cfg.write_text("models:\n  default: claude-haiku-4-5\n")
    # Simulate a stale cached config.
    config._se_config._cache = {"models": {"default": "stale"}}
    assert _model_for("quick-ask") == "stale"

    svc = _skill_runtime_service(tmp_path / "skills", tmp_path)
    svc.reload()
    assert _model_for("quick-ask") == "claude-haiku-4-5"


def test_api_reload_updates_service_state(monkeypatch, tmp_path) -> None:
    """POST /api/reload re-reads the skills directory and updates service state."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "old-skill").mkdir()
    (skills_dir / "old-skill" / "SKILL.md").write_text("---\nname: old-skill\n---\n\n")

    svc = _skill_runtime_service(skills_dir, tmp_path)
    assert "old-skill" in svc.skill_ids

    # add a new skill after startup
    (skills_dir / "fresh-skill").mkdir()
    (skills_dir / "fresh-skill" / "SKILL.md").write_text("---\nname: fresh-skill\n---\n\n")

    resp = svc.reload()
    assert resp["reloaded"] is True
    ids = {s["id"] for s in resp["skills"]}
    assert "fresh-skill" in ids
    assert "old-skill" in ids
    assert "fresh-skill" in svc.skill_ids
