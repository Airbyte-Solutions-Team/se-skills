"""Deterministic tests for webapp skill presentation tiers and ordering."""

import pytest

from services.skill_runtime_service import (
    TIER_ANYTIME,
    TIER_LATE,
    TIER_META,
    TIER_WORKFLOW,
    SkillRuntimeService,
)
from webapp.config import SUITE_SKILLS_DIR


@pytest.fixture
def skills_list():
    svc = SkillRuntimeService(
        customers_dir=SUITE_SKILLS_DIR,
        workspace=SUITE_SKILLS_DIR,
        output_service=None,
        job_service=None,
        se_config=lambda: {},
        se_config_clear=lambda: None,
        safe_name=lambda n: n,
        skills_dir=SUITE_SKILLS_DIR,
        skills_dirs=[SUITE_SKILLS_DIR],
    )
    return svc.discover_skills()


@pytest.fixture
def skills_by_id(skills_list):
    return {s["id"]: s for s in skills_list}


def _index(skills_list, sid):
    for i, s in enumerate(skills_list):
        if s["id"] == sid:
            return i
    raise AssertionError(f"{sid} not found in discovered skills")


def test_late_stage_tier_is_distinct():
    assert TIER_LATE == "Late-stage — after POC"


def test_roi_business_case_and_mutual_close_plan_are_late_stage(skills_list, skills_by_id):
    roi = skills_by_id["roi-business-case"]
    close = skills_by_id["mutual-close-plan"]

    assert roi["tier"] == TIER_LATE
    assert close["tier"] == TIER_LATE

    assert roi["step"] == 8
    assert close["step"] == 9

    assert _index(skills_list, "roi-business-case") < _index(skills_list, "mutual-close-plan")


def test_poc_plan_precedes_late_stage(skills_list, skills_by_id):
    poc = skills_by_id["poc-plan"]
    roi = skills_by_id["roi-business-case"]
    close = skills_by_id["mutual-close-plan"]

    assert poc["tier"] == TIER_WORKFLOW
    assert poc["step"] == 7
    assert (
        _index(skills_list, "poc-plan")
        < _index(skills_list, "roi-business-case")
        < _index(skills_list, "mutual-close-plan")
    )


def test_workflow_numbers_are_sequential_and_anytime_router_unnumbered(skills_by_id):
    """Workflow skills have step numbers; anytime/router skills are unnumbered."""
    workflow_steps = {
        skills_by_id[sid]["step"]
        for sid in [
            "prep-call",
            "post-call",
            "deployment-model-qual",
            "biz-qual",
            "tech-qual",
            "connector-feasibility",
            "poc-plan",
        ]
    }
    assert workflow_steps == {1, 2, 3, 4, 5, 6, 7}

    assert skills_by_id["full-qual"]["step"] is None
    assert skills_by_id["deal-assessment"]["tier"] == TIER_ANYTIME
    assert skills_by_id["next-move"]["tier"] == TIER_META
