"""Coverage-handoff template v2: reviewable items contract.

The skill's update flow (SKILL.md -> "Updating an existing handoff") depends on the
template carrying stable item ids/statuses and the review UI, so guard both.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "coverage-handoff"
STATUSES = {"open", "in-progress", "done", "dropped"}


@pytest.fixture(scope="module")
def template() -> str:
    return (SKILL_DIR / "template.html").read_text(encoding="utf-8")


def test_template_version_is_v2(template: str) -> None:
    assert "TEMPLATE VERSION: v2" in template


def test_each_action_section_has_a_reviewable_item(template: str) -> None:
    ids = re.findall(r'data-item-id="([A-Z]{2}-\d+)"', template)
    assert {i.split("-")[0] for i in ids} == {"CM", "OI", "TT"}


def test_items_declare_a_valid_status_and_review_controls(template: str) -> None:
    rows = re.findall(r"<(?:tr|li) data-item-id=\"[^\"]+\" data-status=\"([^\"]+)\".*?</(?:tr|li)>", template, re.S)
    assert rows and set(rows) <= STATUSES
    for block in re.findall(r"<(?:tr|li) data-item-id=.*?</(?:tr|li)>", template, re.S):
        assert "<select" in block and 'type="text"' in block
        for status in STATUSES:
            assert f'value="{status}"' in block


def test_review_ui_and_handoff_id_present(template: str) -> None:
    for needle in ('id="review-bar"', 'id="rv-export"', 'id="rv-reset"', 'name="coverage-handoff-id"', "<script>"):
        assert needle in template
    assert "UPDATE RUNS ONLY" in template  # the "Changes since" callout is documented as conditional


def test_skill_documents_update_mode() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "HANDOFF UPDATE NOTES" in skill
    assert "Updating an existing handoff" in skill
    assert "Keep ids stable" in skill
