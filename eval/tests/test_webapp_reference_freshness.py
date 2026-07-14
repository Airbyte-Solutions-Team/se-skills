"""Deterministic tests for reference-freshness wiring in the webapp.

These tests call FastAPI route functions and `list_outputs` directly against a
monkeypatched `CUSTOMERS_DIR` and `WORKSPACE` so they do not touch the real
workspace. Reference freshness is recomputed at read time, so the results reflect
the temporary filesystem state.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import webapp.app as app

rf = app.reference_freshness


def _write_md(
    customers_dir: Path,
    account: str,
    opp: str,
    skill: str,
    filename: str,
    text: str,
) -> Path:
    d = customers_dir / account / "opportunities" / opp / "outputs" / skill
    d.mkdir(parents=True, exist_ok=True)
    md = d / filename
    md.write_text(text, encoding="utf-8")
    return md


def _set_mtime(path: Path, days_ago: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    os.utime(path, (old_ts, old_ts))


VALID_BIZ_QUAL = """# Acme — biz-qual: viable

|**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Verdict:** viable

## MEDDPICC Scorecard
| Letter | Status |
|---|---|
| M | green |

## Source Coverage
- synthetic
"""


def _write_fresh_reference_files(workspace: Path) -> None:
    registry = workspace / "02-repos" / "registry"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "oss_registry.json").write_text("{}", encoding="utf-8")
    (registry / "cloud_registry.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def patched(monkeypatch, tmp_path: Path):
    """Patch app paths to a temp workspace and create a fresh registry cache."""
    customers = tmp_path / "customers"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_fresh_reference_files(workspace)
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(app, "WORKSPACE", workspace)
    return customers, workspace


def test_list_outputs_exposes_reference_freshness(patched) -> None:
    customers, _ = patched
    _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)

    outputs = app.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    ref = outputs[0]["reference_freshness"]
    assert isinstance(ref, list)
    assert any(r["source"] == "registry" for r in ref)
    registry_entry = next(r for r in ref if r["source"] == "registry")
    assert registry_entry["fresh"] is True
    assert registry_entry["status"] == "fresh"


def test_list_outputs_warns_when_registry_stale(patched, monkeypatch) -> None:
    customers, workspace = patched
    # Backdate registry cache so the lowered threshold marks it stale.
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 1.0)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 1.0)
    monkeypatch.setattr(rf, "_DEFAULT_THRESHOLDS", {**rf._DEFAULT_THRESHOLDS, "registry": 0})

    _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)

    outputs = app.list_outputs("Acme", "intro")
    ref = outputs[0]["reference_freshness"]
    registry_entry = next(r for r in ref if r["source"] == "registry")
    assert registry_entry["fresh"] is False
    assert registry_entry["status"] == "stale"


def test_api_output_meta_exposes_reference_freshness(patched) -> None:
    customers, _ = patched
    md = _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)
    rel = str(md.relative_to(customers))

    data = app.api_output_meta(path=rel)
    ref = data["reference_freshness"]
    assert isinstance(ref, list)
    registry_entry = next(r for r in ref if r["source"] == "registry")
    assert registry_entry["fresh"] is True


def test_api_output_meta_missing_source_is_not_fresh(patched) -> None:
    customers, workspace = patched
    # Remove the registry cache so the freshness entry is missing.
    shutil.rmtree(workspace / "02-repos" / "registry")

    md = _write_md(customers, "Acme", "intro", "biz-qual", "biz-qual-2026-07-01.md", VALID_BIZ_QUAL)
    rel = str(md.relative_to(customers))

    data = app.api_output_meta(path=rel)
    ref = data["reference_freshness"]
    registry_entry = next(r for r in ref if r["source"] == "registry")
    assert registry_entry["status"] == "missing"
    assert registry_entry["fresh"] is False
