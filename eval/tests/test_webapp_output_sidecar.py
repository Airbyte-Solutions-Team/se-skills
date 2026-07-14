"""Deterministic tests for STRUCT-003 output sidecars and API wiring.

These tests call FastAPI route functions and `list_outputs` directly against a
monkeypatched `CUSTOMERS_DIR` so they do not touch the real workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import webapp.app as app


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


def test_list_outputs_exposes_validation_metadata(monkeypatch, tmp_path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)

    outputs = app.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    assert outputs[0]["valid"] is True
    assert outputs[0]["validation_status"] == "valid"
    assert outputs[0]["validation_errors"] == []
    assert outputs[0]["missing_sections"] == []


def test_list_outputs_flags_invalid_output(monkeypatch, tmp_path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    bad = """# Bad

**Date:** 2026-07-01

## At a Glance
- **Verdict:** qualified

## MEDDPICC Scorecard
ok
"""
    _write_md(customers, "Acme", "intro", "biz-qual", "bad.md", bad)

    outputs = app.list_outputs("Acme", "intro")
    assert outputs[0]["valid"] is False
    assert outputs[0]["validation_status"] == "invalid"
    assert any("source-coverage" in e for e in outputs[0]["validation_errors"])


def test_list_outputs_writes_sidecar(monkeypatch, tmp_path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    md = _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)

    app.list_outputs("Acme", "intro")
    sidecar = md.with_suffix(md.suffix + ".json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema_version"] == app.output_schema.SCHEMA_VERSION
    assert data["validation_status"] == "valid"


def test_api_output_meta_returns_validation(monkeypatch, tmp_path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    md = _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)
    rel = str(md.relative_to(customers))

    data = app.api_output_meta(path=rel)
    assert data["skill"] == "biz-qual"
    assert data["valid"] is True
    assert data["validation_status"] == "valid"
    assert data["schema_version"] == app.output_schema.SCHEMA_VERSION


def test_api_output_meta_404_outside_customers_dir(monkeypatch, tmp_path) -> None:
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    customers.mkdir(parents=True)

    with pytest.raises(HTTPException) as exc:
        app.api_output_meta(path="../etc/passwd")
    assert exc.value.status_code == 404
