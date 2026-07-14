"""Deterministic tests for ARCH-007 runtime skill discovery refresh."""

import webapp.app as app
from webapp.app import api_reload_skills, discover_skills


def test_discover_skills_finds_new_skill_folder(monkeypatch, tmp_path) -> None:
    """Adding a new SKILL.md under the configured skills dir makes it appear."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "new-skill").mkdir()
    (skills_dir / "new-skill" / "SKILL.md").write_text("---\nname: new-skill\n---\n# New skill\n")
    monkeypatch.setattr(app, "SUITE_SKILLS_DIR", skills_dir)
    found = discover_skills()
    ids = [s["id"] for s in found]
    assert "new-skill" in ids


def test_api_reload_updates_globals(monkeypatch, tmp_path) -> None:
    """POST /api/reload re-reads the skills directory and updates SKILLS/IDS."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "old-skill").mkdir()
    (skills_dir / "old-skill" / "SKILL.md").write_text("---\nname: old-skill\n---\n\n")

    monkeypatch.setattr(app, "SUITE_SKILLS_DIR", skills_dir)
    # seed the globals as if the app just booted with one skill
    initial = discover_skills()
    monkeypatch.setattr(app, "SKILLS", initial)
    monkeypatch.setattr(app, "SKILL_IDS", {s["id"] for s in initial})

    # add a new skill after startup
    (skills_dir / "fresh-skill").mkdir()
    (skills_dir / "fresh-skill" / "SKILL.md").write_text("---\nname: fresh-skill\n---\n\n")

    resp = api_reload_skills()
    assert resp["reloaded"] is True
    ids = {s["id"] for s in resp["skills"]}
    assert "fresh-skill" in ids
    assert "old-skill" in ids
    assert "fresh-skill" in app.SKILL_IDS
