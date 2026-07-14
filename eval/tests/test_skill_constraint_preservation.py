"""Deterministic prompt-regression tests for SKILL-004.

These tests protect the guardrail language that prevents `roi-business-case`
and `poc-plan` from silently lowering customer capacity, sync frequency,
concurrency, throughput, or POC schedule assumptions.
"""

import pytest
from pathlib import Path


SKILL_REQUIREMENTS = [
    pytest.param(
        "skills/roi-business-case/SKILL.md",
        [
            "capacity sizing",
            "sync frequency",
            "concurrency",
            "throughput",
            "primary scenario",
            "optimization alternative",
            "explicitly approves",
            "Operating Discipline D5",
        ],
        id="roi-business-case",
    ),
    pytest.param(
        "skills/poc-plan/SKILL.md",
        [
            "capacity sizing",
            "sync frequency",
            "concurrency",
            "throughput",
            "POC schedule",
            "success criteria",
            "Optional stretch scope",
            "Production requirements",
            "Operating Discipline D5",
        ],
        id="poc-plan",
    ),
]


@pytest.mark.parametrize("skill_path, required_phrases", SKILL_REQUIREMENTS)
def test_skill_preserves_customer_constraints(skill_path: str, required_phrases: list[str], repo_root: Path) -> None:
    """Each sizing/planning skill must name the four customer constraints and the permission/labeling rule."""
    text = (repo_root / skill_path).read_text(encoding="utf-8").lower()
    missing = [phrase for phrase in required_phrases if phrase.lower() not in text]
    assert not missing, f"{skill_path} missing constraint-preservation language: {missing}"


def test_playbook_defines_constraint_preservation_discipline(repo_root: Path) -> None:
    """The shared Operating Discipline D5 is the source of truth for the guardrail."""
    text = (repo_root / "skills/_se-playbook.md").read_text(encoding="utf-8").lower()
    for phrase in [
        "customer-constraint preservation",
        "capacity sizing",
        "sync frequency",
        "concurrency",
        "throughput",
        "primary scenario",
        "optimization alternative",
        "do not lower these in the primary estimate",
    ]:
        assert phrase.lower() in text, f"_se-playbook.md missing D5 language: {phrase!r}"
