"""Deterministic tests for reference-freshness wiring in the webapp.

These tests call FastAPI route functions and `list_outputs` directly against a
monkeypatched `CUSTOMERS_DIR` and `WORKSPACE` so they do not touch the real
workspace. Reference freshness is recomputed at read time, but the sidecar
preserves the generation-time snapshot.
"""

from __future__ import annotations

import os
import shutil
import time
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


VALID_CONNECTOR_FEASIBILITY = """# Acme — connector-feasibility: viable

|**Date:** 2026-07-01 · **Skill:** connector-feasibility

## At a Glance
- **Feasibility:** viable

## Fit Verdict
| Connector | Status |
|---|---|
| source | green |

## Source Coverage
- synthetic
"""

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
    _set_mtime(workspace / "02-repos" / "airbyte-enterprise" / ".git" / "HEAD", 0)


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


def test_list_outputs_exposes_reference_freshness_for_relevant_skill(patched) -> None:
    customers, _ = patched
    _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    app._write_output_sidecar("Acme", "intro", "connector-feasibility")

    outputs = app.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    at_gen = outputs[0]["reference_freshness_at_generation"]
    assert isinstance(at_gen, list)
    assert {r["source"] for r in at_gen} == {"registry", "airbyte_enterprise"}
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["fresh"] is True
    assert registry_entry["status"] == "fresh"


def test_list_outputs_omits_reference_freshness_for_irrelevant_skill(patched) -> None:
    customers, _ = patched
    _write_md(customers, "Acme", "intro", "biz-qual", "bq-2026-07-01.md", VALID_BIZ_QUAL)
    app._write_output_sidecar("Acme", "intro", "biz-qual")

    outputs = app.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    assert outputs[0]["reference_freshness_at_generation"] == []
    assert outputs[0]["reference_changed_since_generation"] == []


def test_list_outputs_warns_when_registry_stale(patched, monkeypatch) -> None:
    customers, workspace = patched
    # Backdate registry cache so the lowered threshold marks it stale.
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 1.0)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 1.0)
    monkeypatch.setattr(rf, "_DEFAULT_THRESHOLDS", {**rf._DEFAULT_THRESHOLDS, "registry": 0})

    _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    app._write_output_sidecar("Acme", "intro", "connector-feasibility")

    outputs = app.list_outputs("Acme", "intro")
    at_gen = outputs[0]["reference_freshness_at_generation"]
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["fresh"] is False
    assert registry_entry["status"] == "stale"


def test_list_outputs_detects_changed_reference_since_generation(patched, monkeypatch) -> None:
    customers, workspace = patched
    # Generate with fresh registry
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 0)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 0)

    _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    # Simulate the skill run that writes the generation-time snapshot.
    app._write_output_sidecar("Acme", "intro", "connector-feasibility")

    # Now backdate the registry to simulate a refresh/update after generation.
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 10)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 10)

    outputs = app.list_outputs("Acme", "intro")
    changed = outputs[0]["reference_changed_since_generation"]
    assert len(changed) == 1
    assert changed[0]["source"] == "registry"
    assert changed[0]["old_status"] == "fresh"
    assert changed[0]["new_status"] == "stale"


def test_api_output_meta_exposes_reference_freshness(patched) -> None:
    customers, _ = patched
    md = _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    app._write_output_sidecar("Acme", "intro", "connector-feasibility")
    rel = str(md.relative_to(customers))

    data = app.api_output_meta(path=rel)
    at_gen = data["reference_freshness_at_generation"]
    assert isinstance(at_gen, list)
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["fresh"] is True


def test_api_output_meta_missing_source_is_not_fresh(patched) -> None:
    customers, workspace = patched
    # Remove the registry cache so the freshness entry is missing.
    shutil.rmtree(workspace / "02-repos" / "registry")

    md = _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    app._write_output_sidecar("Acme", "intro", "connector-feasibility")
    rel = str(md.relative_to(customers))

    data = app.api_output_meta(path=rel)
    at_gen = data["reference_freshness_at_generation"]
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["status"] == "missing"
    assert registry_entry["fresh"] is False


def test_legacy_sidecar_without_generation_snapshot_is_unknown(patched, monkeypatch) -> None:
    customers, _ = patched
    md = _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    # Write an old-style sidecar without reference_freshness_at_generation.
    sidecar = md.with_suffix(md.suffix + ".json")
    sidecar.write_text('{"skill":"connector-feasibility","valid":true,"schema_version":1}', encoding="utf-8")

    rel = str(md.relative_to(customers))
    data = app.api_output_meta(path=rel)
    assert data["reference_freshness_at_generation"] is None
    assert data["reference_changed_since_generation"] is None
