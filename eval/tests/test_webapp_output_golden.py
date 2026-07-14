"""Tests for the webapp golden-fixture promotion endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

import app
import golden


SAMPLE_MD = """# Acme — biz-qual: viable

**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Verdict:** viable

## Source Coverage
- synthetic
"""


def test_api_output_golden_post_writes_fixture(monkeypatch, tmp_path: Path) -> None:
    """Promoting an output writes the Markdown into the golden fixture tree."""
    customers = tmp_path / "customers"
    d = customers / "Acme" / "outputs" / "biz-qual"
    d.mkdir(parents=True)
    md = d / "biz-qual-2026-07-01.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")

    golden_root = tmp_path / "golden"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(golden, "_GOLDEN_DIR", golden_root)

    result = app.api_output_golden_post(
        app.OutputGolden(path="Acme/outputs/biz-qual/biz-qual-2026-07-01.md", scenario="test-scenario")
    )

    assert result["skill"] == "biz-qual"
    assert result["scenario"] == "test-scenario"

    fixture = golden_root / "biz-qual" / "test-scenario.md"
    assert fixture.exists()
    assert fixture.read_text(encoding="utf-8") == SAMPLE_MD


def test_api_output_golden_post_rejects_bad_path(monkeypatch, tmp_path: Path) -> None:
    """The endpoint rejects paths outside the customers directory."""
    customers = tmp_path / "customers"
    customers.mkdir()
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)

    with pytest.raises(Exception):
        app.api_output_golden_post(app.OutputGolden(path="../etc/passwd.md", scenario="x"))
