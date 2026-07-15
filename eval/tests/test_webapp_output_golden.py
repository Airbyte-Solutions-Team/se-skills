"""Tests for the output golden-fixture promotion service."""

from __future__ import annotations

from pathlib import Path

import pytest

import golden
from webapp.routes.outputs import OutputGolden
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


SAMPLE_MD = """# Acme — tech-qual: missing critical input

**Date:** 2026-07-01 · **Skill:** tech-qual

## At a Glance
- **Technical Fit:** insufficient
- **Primary Risk:** missing discovery

## Technical Fit Summary
Not enough technical input to qualify.

## Source Coverage
- synthetic
"""


def _write_test_output(customers: Path) -> Path:
    d = customers / "Acme" / "outputs" / "tech-qual"
    d.mkdir(parents=True)
    md = d / "tech-qual-2026-07-01.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    return md


def test_promote_to_golden_writes_active_fixture(monkeypatch, tmp_path: Path) -> None:
    """Promoting an output writes the Markdown into the golden fixture tree
    when the scenario matches a Phase 1 manifest for the skill."""
    customers = tmp_path / "customers"
    _write_test_output(customers)

    golden_root = tmp_path / "golden"
    monkeypatch.setattr(golden, "_GOLDEN_DIR", golden_root)

    svc = _svc(customers)
    result = svc.promote_to_golden(
        OutputGolden(
            path="Acme/outputs/tech-qual/tech-qual-2026-07-01.md",
            scenario="phase1-missing-technical-input",
            confirm_synthetic=True,
        ),
        customers_dir=customers,
        repo_root=tmp_path,
    )

    assert result["skill"] == "tech-qual"
    assert result["scenario"] == "phase1-missing-technical-input"
    assert result["active"] is True

    fixture = golden_root / "tech-qual" / "phase1-missing-technical-input.md"
    assert fixture.exists()
    assert fixture.read_text(encoding="utf-8") == SAMPLE_MD


def test_promote_to_golden_uses_edited_text(monkeypatch, tmp_path: Path) -> None:
    """The SE can edit the Markdown preview before saving the golden fixture."""
    customers = tmp_path / "customers"
    _write_test_output(customers)

    golden_root = tmp_path / "golden"
    monkeypatch.setattr(golden, "_GOLDEN_DIR", golden_root)

    svc = _svc(customers)
    edited = SAMPLE_MD + "\n<!-- edited for golden fixture -->\n"
    result = svc.promote_to_golden(
        OutputGolden(
            path="Acme/outputs/tech-qual/tech-qual-2026-07-01.md",
            scenario="phase1-missing-technical-input",
            text=edited,
            confirm_synthetic=True,
        ),
        customers_dir=customers,
        repo_root=tmp_path,
    )

    fixture = golden_root / "tech-qual" / "phase1-missing-technical-input.md"
    assert fixture.read_text(encoding="utf-8") == edited


def test_promote_to_golden_rejects_missing_synthetic_confirmation(tmp_path: Path) -> None:
    """The service rejects promotion unless the SE confirms the content is synthetic."""
    customers = tmp_path / "customers"
    _write_test_output(customers)
    svc = _svc(customers)

    with pytest.raises(OutputError):
        svc.promote_to_golden(
            OutputGolden(
                path="Acme/outputs/tech-qual/tech-qual-2026-07-01.md",
                scenario="phase1-missing-technical-input",
                confirm_synthetic=False,
            ),
            customers_dir=customers,
            repo_root=tmp_path,
        )


def test_promote_to_golden_rejects_untested_scenario(tmp_path: Path) -> None:
    """The service rejects a scenario that no Phase 1 manifest exercises for this skill."""
    customers = tmp_path / "customers"
    _write_test_output(customers)
    svc = _svc(customers)

    with pytest.raises(OutputError):
        svc.promote_to_golden(
            OutputGolden(
                path="Acme/outputs/tech-qual/tech-qual-2026-07-01.md",
                scenario="not-a-real-scenario",
                confirm_synthetic=True,
            ),
            customers_dir=customers,
            repo_root=tmp_path,
        )


def test_golden_manifests_returns_active_scenarios() -> None:
    """The manifest helper returns Phase 1 scenario IDs that exercise a skill."""
    scenarios = golden.manifest_scenarios("tech-qual")
    assert "phase1-missing-technical-input" in scenarios
    assert "phase1-unverified-connector" in scenarios


def test_promote_to_golden_rejects_bad_path(tmp_path: Path) -> None:
    """The service rejects paths outside the customers directory."""
    customers = tmp_path / "customers"
    customers.mkdir()
    svc = _svc(customers)

    with pytest.raises(OutputError):
        svc.promote_to_golden(
            OutputGolden(
                path="../etc/passwd.md",
                scenario="phase1-missing-technical-input",
                confirm_synthetic=True,
            ),
            customers_dir=customers,
            repo_root=tmp_path,
        )
