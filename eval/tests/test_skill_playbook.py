"""Deterministic checks for SKILL-001: shared playbook centralization.

These tests verify that the shared `_se-playbook.md` fragments exist, that every
affected skill references them, and that skill-specific guardrails and output
structure were not accidentally removed by the refactor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
PLAYBOOK = SKILLS_DIR / "_se-playbook.md"
INSTALL_SCRIPT = REPO_ROOT / "install.sh"

OUTPUT_FORMAT_SKILLS = {
    "account-refresher",
    "biz-qual",
    "connector-feasibility",
    "deal-assessment",
    "deployment-model-qual",
    "internal-prep",
    "mutual-close-plan",
    "poc-plan",
    "post-call",
    "prep-call",
    "roi-business-case",
    "tech-qual",
}

SAVING_SKILLS = {
    "account-refresher",
    "biz-qual",
    "connector-feasibility",
    "coverage-handoff",
    "deal-assessment",
    "deployment-model-qual",
    "follow-up-email",
    "internal-prep",
    "mutual-close-plan",
    "next-move",
    "objection-handler",
    "poc-plan",
    "post-call",
    "prep-call",
    "roi-business-case",
    "tech-qual",
}


def _skill_files() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.glob("*/SKILL.md") if not p.parent.name.startswith("_"))


def _playbook_headings() -> list[str]:
    text = PLAYBOOK.read_text(encoding="utf-8")
    return [m.group(2).strip() for m in re.finditer(r"^(#{2,4}) +(.+)$", text, re.MULTILINE)]


@pytest.fixture
def headings() -> list[str]:
    assert PLAYBOOK.exists(), "_se-playbook.md should exist"
    return _playbook_headings()


@pytest.fixture
def skill_files() -> list[Path]:
    files = _skill_files()
    assert files, "should find skill files"
    return files


def test_playbook_has_shared_boilerplate_sections(headings: list[str]) -> None:
    required = [
        "Shared Skill Boilerplate",
        "Output format reference",
        "Pre-flight source check (qualification and synthesis skills)",
        "After Generating (saving skills)",
    ]
    for title in required:
        assert title in headings, f"missing playbook section: {title}"


def test_install_script_exposes_playbook() -> None:
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "_se-playbook.md" in script, "install.sh must expose the shared playbook"
    assert "skills/" in script or "~/.claude/skills" in script, "install.sh should symlink skills/"


@pytest.mark.parametrize("skill", sorted(SAVING_SKILLS))
def test_saving_skills_reference_after_generating_boilerplate(skill: str) -> None:
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "Shared Skill Boilerplate" in text, f"{skill} should reference the shared boilerplate"
    assert "After Generating (saving skills)" in text, f"{skill} should reference the shared After Generating rules"


@pytest.mark.parametrize("skill", ["biz-qual", "full-qual"])
def test_qualification_skills_reference_preflight_check(skill: str) -> None:
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "Pre-flight source check" in text, f"{skill} should reference the shared pre-flight check"


@pytest.mark.parametrize("skill", sorted(OUTPUT_FORMAT_SKILLS))
def test_skills_reference_output_format(skill: str) -> None:
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "Output format reference" in text, f"{skill} should reference the shared output format"


def test_duplicated_auto_save_boilerplate_removed(skill_files: list[Path]) -> None:
    old_auto_save = 'Per `_se-playbook.md` "Output Persistence (Auto-Save)"'
    for path in skill_files:
        text = path.read_text(encoding="utf-8")
        assert old_auto_save not in text, f"{path.parent.name} still uses old auto-save boilerplate"


def test_duplicated_output_format_sentence_removed(skill_files: list[Path]) -> None:
    old = "Document structure follows `_se-playbook.md` → Output Document Format ("
    for path in skill_files:
        text = path.read_text(encoding="utf-8")
        assert old not in text, f"{path.parent.name} still uses the old output-format sentence"


def test_preflight_source_check_centralized() -> None:
    old_preflight = "Before doing anything else, check:\n1. `{transcripts_dir}/`"
    new_preflight = "Pre-flight source check"
    for name in ("biz-qual", "full-qual"):
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert old_preflight not in text, f"{name} still contains the old pre-flight checklist"
        assert new_preflight in text, f"{name} should reference the shared pre-flight source check"


@pytest.mark.parametrize(
    "skill_path,expected_phrase",
    [
        pytest.param(SKILLS_DIR / "biz-qual" / "SKILL.md", "MEDDPICC", id="biz-qual-meddpicc"),
        pytest.param(
            SKILLS_DIR / "tech-qual" / "SKILL.md",
            "EntitlementDefinitions.kt",
            id="tech-qual-entitlement",
        ),
        pytest.param(
            SKILLS_DIR / "deal-assessment" / "SKILL.md",
            "Probability",
            id="deal-assessment-probability",
        ),
        pytest.param(
            SKILLS_DIR / "deployment-model-qual" / "SKILL.md",
            "5 questions",
            id="deployment-qual-five-questions",
        ),
        pytest.param(
            SKILLS_DIR / "poc-plan" / "SKILL.md",
            "success criteria",
            id="poc-plan-success-criteria",
        ),
        pytest.param(
            SKILLS_DIR / "next-move" / "SKILL.md",
            "routing recommendation",
            id="next-move-router",
        ),
        pytest.param(
            SKILLS_DIR / "follow-up-email" / "SKILL.md",
            'Don\'t hardcode "Gary"',
            id="follow-up-email-no-gary",
        ),
    ],
)
def test_skill_specific_guardrails_preserved(skill_path: Path, expected_phrase: str) -> None:
    text = skill_path.read_text(encoding="utf-8")
    assert expected_phrase in text, f"{skill_path.parent.name} lost a skill-specific guardrail: {expected_phrase!r}"


def test_shared_references_resolve(headings: list[str]) -> None:
    """Every new shared heading referenced by skills actually exists in the playbook."""
    shared_sections = [
        "Shared Skill Boilerplate",
        "Output format reference",
        "Pre-flight source check (qualification and synthesis skills)",
        "After Generating (saving skills)",
    ]
    for section in shared_sections:
        assert section in headings, f"shared playbook section missing: {section}"
