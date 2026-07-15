"""Deterministic tests for reference-data freshness computation."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import reference_freshness as rf
import output_schema


def _set_mtime(path: Path, days_ago: float) -> None:
    """Create a file if it does not exist and backdate its mtime by days."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    os.utime(path, (old_ts, old_ts))


@pytest.fixture
def tmp_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Keep the objection-reference home-dir fallback (reference_freshness.py)
    # inside the temp dir so an installed ~/.claude/skills copy on the dev
    # machine cannot leak into these hermetic tests.
    monkeypatch.setattr(rf.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _write_all_reference_files(workspace: Path) -> None:
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 1.5)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 2.5)
    _set_mtime(workspace / "02-repos" / "airbyte-platform" / ".git" / "FETCH_HEAD", 5)
    _set_mtime(workspace / "02-repos" / "airbyte-enterprise" / ".git" / "HEAD", 20)
    _set_mtime(
        workspace / "skills" / "_reference" / "airbyte-objection-reference.md",
        3,
    )


def test_registry_fresh(tmp_workspace: Path) -> None:
    _set_mtime(tmp_workspace / "02-repos" / "registry" / "oss_registry.json", 1.5)
    _set_mtime(tmp_workspace / "02-repos" / "registry" / "cloud_registry.json", 2.5)

    result = rf.compute_reference_freshness({}, tmp_workspace, tmp_workspace)
    entry = next(r for r in result if r.source == "registry")

    assert entry.status == "fresh"
    assert entry.fresh is True
    assert entry.age_days in (1, 2)
    assert entry.threshold_days == 7


def test_registry_stale(tmp_workspace: Path) -> None:
    _set_mtime(tmp_workspace / "02-repos" / "registry" / "oss_registry.json", 10)

    result = rf.compute_reference_freshness({}, tmp_workspace, tmp_workspace)
    entry = next(r for r in result if r.source == "registry")

    assert entry.status == "stale"
    assert entry.fresh is False
    assert entry.age_days == 10


def test_repo_fresh_and_stale(tmp_workspace: Path) -> None:
    platform = tmp_workspace / "02-repos" / "airbyte-platform"
    _set_mtime(platform / ".git" / "FETCH_HEAD", 5)
    enterprise = tmp_workspace / "02-repos" / "airbyte-enterprise"
    _set_mtime(enterprise / ".git" / "HEAD", 20)

    result = rf.compute_reference_freshness({}, tmp_workspace, tmp_workspace)

    platform_entry = next(r for r in result if r.source == "airbyte_platform")
    assert platform_entry.status == "fresh"
    assert platform_entry.age_days == 5
    assert platform_entry.threshold_days == 14

    enterprise_entry = next(r for r in result if r.source == "airbyte_enterprise")
    assert enterprise_entry.status == "stale"
    assert enterprise_entry.age_days == 20


def test_objection_reference_fresh(tmp_workspace: Path) -> None:
    _set_mtime(
        tmp_workspace / "skills" / "_reference" / "airbyte-objection-reference.md",
        3,
    )

    result = rf.compute_reference_freshness({}, tmp_workspace, tmp_workspace)
    entry = next(r for r in result if r.source == "objection_reference")

    assert entry.status == "fresh"
    assert entry.age_days == 3
    assert entry.threshold_days == 7


def test_missing_sources_are_flagged(tmp_workspace: Path) -> None:
    result = rf.compute_reference_freshness({}, tmp_workspace, tmp_workspace)

    for source in (
        "registry",
        "airbyte_platform",
        "airbyte_enterprise",
        "objection_reference",
    ):
        entry = next(r for r in result if r.source == source)
        assert entry.status == "missing"
        assert entry.fresh is False
        assert entry.date == "unknown"
        assert entry.age_days is None


def test_config_overrides_paths(tmp_workspace: Path) -> None:
    config = {
        "airbyte_repos_dir": str(tmp_workspace / "custom-repos"),
        "reference_data": {
            "registry": {"cache_dir": "custom-registry"},
            "repos": {
                "airbyte_platform": "plat",
                "airbyte_enterprise": "ent",
            },
        },
    }
    _set_mtime(tmp_workspace / "custom-repos" / "custom-registry" / "oss_registry.json", 0)
    _set_mtime(tmp_workspace / "custom-repos" / "plat" / ".git" / "HEAD", 0)
    _set_mtime(tmp_workspace / "custom-repos" / "ent" / ".git" / "HEAD", 0)

    result = rf.compute_reference_freshness(config, tmp_workspace, tmp_workspace)

    registry = next(r for r in result if r.source == "registry")
    assert registry.path.endswith("custom-registry")
    assert registry.fresh is True

    platform = next(r for r in result if r.source == "airbyte_platform")
    assert platform.path.endswith("plat")
    assert platform.fresh is True


def test_skill_filtering_limits_sources(tmp_workspace: Path) -> None:
    _write_all_reference_files(tmp_workspace)

    connector = rf.compute_reference_freshness(
        {}, tmp_workspace, tmp_workspace, skill="connector-feasibility"
    )
    assert {r.source for r in connector} == {"registry", "airbyte_enterprise"}

    deployment = rf.compute_reference_freshness(
        {}, tmp_workspace, tmp_workspace, skill="deployment-model-qual"
    )
    assert {r.source for r in deployment} == {"airbyte_platform", "airbyte_enterprise"}

    objection = rf.compute_reference_freshness(
        {}, tmp_workspace, tmp_workspace, skill="objection-handler"
    )
    assert {r.source for r in objection} == {
        "objection_reference",
        "airbyte_platform",
        "airbyte_enterprise",
    }

    biz = rf.compute_reference_freshness(
        {}, tmp_workspace, tmp_workspace, skill="biz-qual"
    )
    assert biz == []

    unknown = rf.compute_reference_freshness(
        {}, tmp_workspace, tmp_workspace, skill="not-a-skill"
    )
    assert unknown == []


def test_compare_to_generation_detects_changes() -> None:
    fresh = rf.ReferenceFreshness(
        source="registry",
        label="Connector registry cache",
        status="fresh",
        date="2026-07-14",
        age_days=0,
        fresh=True,
        threshold_days=7,
        path="/tmp",
    )
    stale = rf.ReferenceFreshness(
        source="objection_reference",
        label="Objection reference",
        status="stale",
        date="2026-07-01",
        age_days=13,
        fresh=False,
        threshold_days=7,
        path="/tmp",
    )

    # Same as generation -> no changes
    assert rf.compare_to_generation([fresh], [fresh]) == []

    # Status changed
    current = [fresh.model_copy(update={"status": "stale", "fresh": False, "age_days": 10})]
    changes = rf.compare_to_generation(current, [fresh])
    assert len(changes) == 1
    assert changes[0].source == "registry"
    assert changes[0].old_status == "fresh"
    assert changes[0].new_status == "stale"

    # Date changed
    current = [fresh.model_copy(update={"date": "2026-07-15"})]
    changes = rf.compare_to_generation(current, [fresh])
    assert changes[0].new_date == "2026-07-15"

    # New source appeared
    changes = rf.compare_to_generation([fresh, stale], [fresh])
    assert len(changes) == 1
    assert changes[0].source == "objection_reference"
    assert changes[0].old_date is None
    assert changes[0].new_date == "2026-07-01"


def test_compare_to_generation_unknown_for_legacy() -> None:
    fresh = rf.ReferenceFreshness(
        source="registry",
        label="Connector registry cache",
        status="fresh",
        date="2026-07-14",
        age_days=0,
        fresh=True,
        threshold_days=7,
        path="/tmp",
    )
    assert rf.compare_to_generation([fresh], None) is None
    assert rf.compare_to_generation([], []) == []
    # Known empty generation vs newly observed source is a new source.
    changes = rf.compare_to_generation([fresh], [])
    assert len(changes) == 1
    assert changes[0].source == "registry"
    assert changes[0].old_date is None


def test_output_metadata_reference_freshness_roundtrip(tmp_workspace: Path) -> None:
    fresh = rf.ReferenceFreshness(
        source="registry",
        label="Connector registry cache",
        status="fresh",
        date="2026-07-14",
        age_days=0,
        fresh=True,
        threshold_days=7,
        path=str(tmp_workspace),
    )
    stale = rf.ReferenceFreshness(
        source="objection_reference",
        label="Objection reference",
        status="stale",
        date="2026-07-01",
        age_days=13,
        fresh=False,
        threshold_days=7,
        path=str(tmp_workspace),
    )
    change = rf.ReferenceChange(
        source="registry",
        label="Connector registry cache",
        old_date="2026-07-14",
        new_date="2026-07-15",
        old_status="fresh",
        new_status="stale",
    )

    md = output_schema.parse_output(
        "unknown-skill",
        "# Title\n\n|**Date:** 2026-07-14 · **Skill:** unknown-skill\n\n## At a Glance\n- **Verdict:** ok\n",
        reference_freshness_at_generation=[fresh, stale],
    )
    md.reference_changed_since_generation = [change]
    data = md.model_dump()
    restored = output_schema.OutputMetadata(**data)

    assert len(restored.reference_freshness_at_generation) == 2
    assert restored.reference_freshness_at_generation[0].fresh is True
    assert restored.reference_freshness_at_generation[1].fresh is False
    assert len(restored.reference_changed_since_generation) == 1
    assert restored.reference_changed_since_generation[0].new_status == "stale"


def test_output_metadata_migrates_legacy_reference_freshness() -> None:
    legacy = {
        "skill": "biz-qual",
        "title": "Legacy",
        "reference_freshness": [
            {
                "source": "registry",
                "label": "Connector registry cache",
                "status": "stale",
                "date": "2026-07-01",
                "age_days": 13,
                "fresh": False,
                "threshold_days": 7,
                "path": "/tmp",
            }
        ],
    }
    restored = output_schema.OutputMetadata(**legacy)
    assert restored.reference_freshness_at_generation is not None
    assert restored.reference_freshness_at_generation[0].source == "registry"
    assert restored.reference_changed_since_generation == []
