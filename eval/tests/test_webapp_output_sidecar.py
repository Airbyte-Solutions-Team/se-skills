"""Deterministic tests for output sidecar parsing and listing.

These tests call `OutputService` directly so they do not need an HTTP client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import webapp.output_schema as output_schema
from webapp.services.output_service import OutputError, OutputService


def _svc(customers_dir: Path) -> OutputService:
    return OutputService(
        customers_dir=customers_dir,
        workspace=customers_dir,
        repo_root=customers_dir,
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n,
        run_cmd=None,
        internal_repo=None,
    )


def _write_md(customers_dir: Path, account: str, opp: str, skill: str, filename: str, text: str) -> Path:
    d = customers_dir / account / "opportunities" / opp / "outputs" / skill
    d.mkdir(parents=True, exist_ok=True)
    md = d / filename
    md.write_text(text, encoding="utf-8")
    return md


VALID_BIZ_QUAL = """# Acme — biz-qual: viable

**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Verdict:** viable

## MEDDPICC Scorecard
| Letter | Status |
|---|---|
| M | green |

## Source Coverage
- synthetic
"""


VALID_TECH_QUAL = """# Acme — tech-qual: fit

**Date:** 2026-07-01 · **Skill:** tech-qual

## At a Glance
- **Technical Fit:** fit
- **Primary Risk:** none

## Technical Fit Summary
Good fit.

## Source Coverage
- synthetic
"""


def test_list_outputs_exposes_validation_metadata(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    _write_md(tmp_path, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)

    outputs = svc.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    assert outputs[0]["valid"] is True
    assert outputs[0]["validation_status"] == "valid"
    assert outputs[0]["validation_errors"] == []
    assert outputs[0]["missing_sections"] == []
    assert outputs[0]["validation_supported"] is True
    assert outputs[0]["reference_sources_tracked"] is False


def test_list_outputs_flags_invalid_output(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    bad = """# Bad

**Date:** 2026-07-01

## At a Glance
- **Verdict:** qualified

## MEDDPICC Scorecard
ok
"""
    _write_md(tmp_path, "Acme", "intro", "biz-qual", "bad.md", bad)

    outputs = svc.list_outputs("Acme", "intro")
    assert outputs[0]["valid"] is False
    assert outputs[0]["validation_status"] == "invalid"
    assert any("source-coverage" in e for e in outputs[0]["validation_errors"])


def test_list_outputs_writes_sidecar(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)

    svc.list_outputs("Acme", "intro")
    sidecar = md.with_suffix(".md.json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema_version"] == output_schema.SCHEMA_VERSION
    assert data["validation_status"] == "valid"


def test_read_output_meta_returns_validation(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)
    rel = str(md.relative_to(tmp_path))

    data = svc.read_output_meta(rel)
    assert data["skill"] == "biz-qual"
    assert data["valid"] is True
    assert data["validation_status"] == "valid"
    assert data["schema_version"] == output_schema.SCHEMA_VERSION


def test_prep_call_runtime_metadata_is_unsupported_and_not_validation_attention(
    tmp_path: Path,
) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "prep-call", "prep.md", "# Prep Call\n")
    rel = str(md.relative_to(tmp_path))

    data = svc.read_output_meta(rel)
    assert data["validation_supported"] is False
    assert data["reference_sources_tracked"] is False
    assert svc.output_validation_status(md.with_suffix(".md.json")) == (False, "unvalidated")


def test_tech_qual_runtime_metadata_tracks_references(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "tech-qual", "tech.md", VALID_TECH_QUAL)
    rel = str(md.relative_to(tmp_path))

    data = svc.read_output_meta(rel)
    assert data["valid"] is True
    assert data["validation_status"] == "valid"
    assert data["validation_supported"] is True
    assert data["reference_sources_tracked"] is True


def test_roi_business_case_runtime_metadata_tracks_objection_reference(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "roi-business-case", "roi.md", "# ROI\n")
    data = svc.read_output_meta(str(md.relative_to(tmp_path)))
    assert data["reference_sources_tracked"] is True


def test_roi_business_case_runtime_metadata_tracks_objection_reference(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "roi-business-case", "roi.md", "# ROI\n")
    data = svc.read_output_meta(str(md.relative_to(tmp_path)))
    assert data["reference_sources_tracked"] is True


def test_legacy_tech_qual_is_unvalidated_but_supported(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "tech-qual", "legacy.md", "# Legacy technical notes\n\nOld format.\n")
    rel = str(md.relative_to(tmp_path))

    data = svc.read_output_meta(rel)
    assert data["validation_status"] == "unvalidated"
    assert data["validation_supported"] is True
    assert data["reference_sources_tracked"] is True


def test_repeated_read_output_meta_preserves_generation_snapshot(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    md = _write_md(tmp_path, "Acme", "intro", "tech-qual", "snapshot.md", VALID_TECH_QUAL)
    snapshot = [
        {
            "source": "airbyte_platform",
            "label": "airbyte-platform repo",
            "status": "fresh",
            "date": "2026-07-01",
            "age_days": 1,
            "fresh": True,
            "threshold_days": 14,
            "path": "/synthetic/airbyte-platform",
        }
    ]
    sidecar = md.with_suffix(".md.json")
    sidecar.write_text(
        json.dumps({
            "skill": "tech-qual",
            "schema_version": output_schema.SCHEMA_VERSION,
            "valid": True,
            "validation_status": "valid",
            "reference_freshness_at_generation": snapshot,
        }),
        encoding="utf-8",
    )
    expected = json.dumps(snapshot, sort_keys=True).encode("utf-8")

    first = svc.read_output_meta(str(md.relative_to(tmp_path)))
    second = svc.read_output_meta(str(md.relative_to(tmp_path)))
    assert json.dumps(first["reference_freshness_at_generation"], sort_keys=True).encode("utf-8") == expected
    assert json.dumps(second["reference_freshness_at_generation"], sort_keys=True).encode("utf-8") == expected
    assert first["reference_changed_since_generation"] == second["reference_changed_since_generation"]
    assert first["reference_changed_since_generation"] is not None


def test_read_output_meta_404_outside_customers_dir(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    (tmp_path / "customers").mkdir(parents=True)

    with pytest.raises(OutputError) as exc:
        svc.read_output_meta("../etc/passwd")
    assert exc.value.status_code == 404
