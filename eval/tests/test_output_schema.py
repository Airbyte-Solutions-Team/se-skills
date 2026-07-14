"""Deterministic tests for STRUCT-001 output schemas and STRUCT-003 sidecars."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import output_schema


def _load_fixture(repo_root: Path, filename: str) -> str:
    return (repo_root / "eval" / "fixtures" / "outputs" / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "skill, filename",
    [
        pytest.param("biz-qual", "next-move-biz-qual.md", id="biz-qual-valid"),
        pytest.param("tech-qual", "next-move-tech-qual.md", id="tech-qual-valid"),
        pytest.param("deployment-model-qual", "next-move-deployment-qual.md", id="deployment-qual-valid"),
        pytest.param("connector-feasibility", "next-move-connector-feasibility.md", id="connector-feasibility-valid"),
    ],
)
def test_parse_output_valid_for_known_skill(skill: str, filename: str, repo_root: Path) -> None:
    text = _load_fixture(repo_root, filename)
    meta = output_schema.parse_output(skill, text)
    assert meta.skill == skill
    assert meta.valid is True
    assert meta.validation_status == "valid"
    assert meta.schema_version == output_schema.SCHEMA_VERSION
    assert meta.title
    assert meta.date
    assert "source-coverage" in meta.sections
    assert not meta.validation_errors


def test_parse_output_flags_missing_required_sections() -> None:
    text = """# Biz Qual

**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Verdict:** qualified

## MEDDPICC Scorecard
| Letter | Status |
|---|---|
| M | green |
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("source-coverage" in e for e in meta.validation_errors)
    assert meta.missing_sections == ["source-coverage"]


def test_parse_output_flags_missing_date() -> None:
    text = """# Biz Qual

## At a Glance
- **Verdict:** qualified

## MEDDPICC Scorecard
ok

## Source Coverage
- transcript
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("Date" in e for e in meta.validation_errors)


def test_parse_output_extracts_at_a_glance_kv_pairs() -> None:
    text = """# Test

**Date:** 2026-07-01

## At a Glance
- **Verdict:** qualified · **Confidence:** Medium
- **Recommended Motion:** run tech-qual

## MEDDPICC Scorecard
ok

## Source Coverage
- synthetic
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.at_a_glance["verdict"] == "qualified"
    assert meta.at_a_glance["confidence"] == "Medium"
    assert meta.at_a_glance["recommended-motion"] == "run tech-qual"
    assert meta.validation_status == "valid"


def test_write_and_read_sidecar(tmp_path: Path) -> None:
    text = """# Test

**Date:** 2026-07-01

## At a Glance
- **Feasibility:** viable

## Fit Verdict
- viable

## Source Coverage
- synthetic
"""
    md_path = tmp_path / "connector-feasibility" / "out.md"
    md_path.parent.mkdir(parents=True)
    md_path.write_text(text, encoding="utf-8")

    meta = output_schema.parse_output("connector-feasibility", text)
    output_schema.write_sidecar(md_path, meta)

    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["skill"] == "connector-feasibility"
    assert data["valid"] is True
    assert data["schema_version"] == output_schema.SCHEMA_VERSION
    assert data["validation_status"] == "valid"

    reloaded = output_schema.read_or_parse_sidecar(md_path, "connector-feasibility")
    assert reloaded.valid == meta.valid
    assert reloaded.title == meta.title


def test_read_or_parse_sidecar_reparses_stale_sidecar(tmp_path: Path) -> None:
    md_path = tmp_path / "biz-qual" / "out.md"
    md_path.parent.mkdir(parents=True)
    md_path.write_text("# Title\n\n**Date:** 2026-07-01\n\n## Source Coverage\n- x\n", encoding="utf-8")

    stale_meta = output_schema.parse_output("biz-qual", md_path.read_text(encoding="utf-8"))
    # manually create an old sidecar that will be considered stale
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    sidecar.write_text(json.dumps(stale_meta.model_dump()), encoding="utf-8")

    # overwrite markdown so sidecar is older
    import time
    time.sleep(0.01)
    md_path.write_text(
        "# Title\n\n**Date:** 2026-07-01\n\n## MEDDPICC Scorecard\nok\n\n## Source Coverage\n- x\n",
        encoding="utf-8",
    )

    reloaded = output_schema.read_or_parse_sidecar(md_path, "biz-qual")
    assert "meddpicc-scorecard" in reloaded.sections


def test_parse_output_fuzzy_matches_section_headings() -> None:
    text = """# Test

**Date:** 2026-07-01

## At a Glance
- **Verdict:** qualified

## MEDDPICC Pre-Scorecard
ok

## Source Coverage
- x
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.valid is True
    assert meta.validation_status == "valid"


def test_parse_output_treats_legacy_format_as_unvalidated() -> None:
    """A document with title/date but no At a Glance and an older heading name
    should not be flagged as definitively broken."""
    text = """# Acme — Biz Qual: qualified

**Date:** 2026-07-01

## Business Qualification
- Metrics: quantified

## Source Coverage
- transcript
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.validation_status == "unvalidated"
    assert meta.valid is True
    assert not meta.validation_errors
    assert not meta.missing_sections


def test_read_or_parse_sidecar_reparses_on_schema_version_mismatch(tmp_path: Path) -> None:
    md_path = tmp_path / "biz-qual" / "out.md"
    md_path.parent.mkdir(parents=True)
    md_path.write_text(
        "# Title\n\n**Date:** 2026-07-01\n\n## At a Glance\n- **Verdict:** qualified\n\n## MEDDPICC Scorecard\nok\n\n## Source Coverage\n- x\n",
        encoding="utf-8",
    )

    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    # Simulate an old-format sidecar with a stale schema version.
    sidecar.write_text(json.dumps({"schema_version": 0, "skill": "biz-qual"}), encoding="utf-8")

    reloaded = output_schema.read_or_parse_sidecar(md_path, "biz-qual")
    assert reloaded.schema_version == output_schema.SCHEMA_VERSION
    assert reloaded.validation_status == "valid"
