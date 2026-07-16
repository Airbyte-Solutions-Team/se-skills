"""Deterministic tests for reference-freshness wiring in the webapp.

These tests call `OutputService` directly against a temporary workspace.
Reference freshness is recomputed at read time, but the sidecar preserves the
generation-time snapshot.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import webapp.services.output_service as output_service_module
from webapp.services.output_service import OutputService


def _repo_root() -> Path:
    # se-skills repo root (parent of eval/)
    return Path(__file__).resolve().parent.parent.parent


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

**Date:** 2026-07-01 · **Skill:** connector-feasibility

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


def _write_fresh_reference_files(workspace: Path) -> None:
    registry = workspace / "02-repos" / "registry"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "oss_registry.json").write_text("{}", encoding="utf-8")
    (registry / "cloud_registry.json").write_text("{}", encoding="utf-8")
    _set_mtime(workspace / "02-repos" / "airbyte-enterprise" / ".git" / "HEAD", 0)


@pytest.fixture
def svc(monkeypatch, tmp_path: Path):
    """Patch OutputService paths to a temp workspace and create a fresh registry cache."""
    customers = tmp_path / "customers"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_fresh_reference_files(workspace)
    return OutputService(
        customers_dir=customers,
        workspace=workspace,
        repo_root=_repo_root(),
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n,
        run_cmd=None,
        internal_repo=None,
    )


def test_list_outputs_exposes_reference_freshness_for_relevant_skill(svc) -> None:
    customers = svc.customers_dir
    _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    svc._write_output_sidecar("Acme", "intro", "connector-feasibility")

    outputs = svc.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    at_gen = outputs[0]["reference_freshness_at_generation"]
    assert isinstance(at_gen, list)
    assert {r["source"] for r in at_gen} == {"registry", "airbyte_enterprise"}
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["fresh"] is True
    assert registry_entry["status"] == "fresh"


def test_list_outputs_omits_reference_freshness_for_irrelevant_skill(svc) -> None:
    customers = svc.customers_dir
    _write_md(customers, "Acme", "intro", "biz-qual", "bq-2026-07-01.md", VALID_BIZ_QUAL)
    svc._write_output_sidecar("Acme", "intro", "biz-qual")

    outputs = svc.list_outputs("Acme", "intro")
    assert len(outputs) == 1
    assert outputs[0]["reference_freshness_at_generation"] == []
    assert outputs[0]["reference_changed_since_generation"] == []


def test_list_outputs_warns_when_registry_stale(svc, monkeypatch) -> None:
    customers = svc.customers_dir
    workspace = svc.workspace
    # Backdate registry cache so the lowered threshold marks it stale.
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 1.0)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 1.0)
    monkeypatch.setattr(
        output_service_module.reference_freshness,
        "_DEFAULT_THRESHOLDS",
        {**output_service_module.reference_freshness._DEFAULT_THRESHOLDS, "registry": 0},
    )

    _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    svc._write_output_sidecar("Acme", "intro", "connector-feasibility")

    outputs = svc.list_outputs("Acme", "intro")
    at_gen = outputs[0]["reference_freshness_at_generation"]
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["fresh"] is False
    assert registry_entry["status"] == "stale"


def test_list_outputs_detects_changed_reference_since_generation(svc, monkeypatch) -> None:
    customers = svc.customers_dir
    workspace = svc.workspace
    # Generate with fresh registry
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 0)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 0)

    _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    # Simulate the skill run that writes the generation-time snapshot.
    svc._write_output_sidecar("Acme", "intro", "connector-feasibility")

    # Now backdate the registry to simulate a refresh/update after generation.
    _set_mtime(workspace / "02-repos" / "registry" / "oss_registry.json", 10)
    _set_mtime(workspace / "02-repos" / "registry" / "cloud_registry.json", 10)

    outputs = svc.list_outputs("Acme", "intro")
    changed = outputs[0]["reference_changed_since_generation"]
    assert len(changed) == 1
    assert changed[0]["source"] == "registry"
    assert changed[0]["old_status"] == "fresh"
    assert changed[0]["new_status"] == "stale"


def test_read_output_meta_exposes_reference_freshness(svc) -> None:
    customers = svc.customers_dir
    md = _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    svc._write_output_sidecar("Acme", "intro", "connector-feasibility")
    rel = str(md.relative_to(customers))

    data = svc.read_output_meta(rel)
    at_gen = data["reference_freshness_at_generation"]
    assert isinstance(at_gen, list)
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["fresh"] is True


def test_read_output_meta_missing_source_is_not_fresh(svc) -> None:
    customers = svc.customers_dir
    workspace = svc.workspace
    # Remove the registry cache so the freshness entry is missing.
    shutil.rmtree(workspace / "02-repos" / "registry")

    md = _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    svc._write_output_sidecar("Acme", "intro", "connector-feasibility")
    rel = str(md.relative_to(customers))

    data = svc.read_output_meta(rel)
    at_gen = data["reference_freshness_at_generation"]
    registry_entry = next(r for r in at_gen if r["source"] == "registry")
    assert registry_entry["status"] == "missing"
    assert registry_entry["fresh"] is False


def test_legacy_sidecar_without_generation_snapshot_is_unknown(svc) -> None:
    customers = svc.customers_dir
    md = _write_md(customers, "Acme", "intro", "connector-feasibility", "cf-2026-07-01.md", VALID_CONNECTOR_FEASIBILITY)
    # Write an old-style sidecar without reference_freshness_at_generation.
    sidecar = md.with_suffix(md.suffix + ".json")
    sidecar.write_text('{"skill":"connector-feasibility","valid":true,"schema_version":1}', encoding="utf-8")

    rel = str(md.relative_to(customers))
    data = svc.read_output_meta(rel)
    assert data["reference_freshness_at_generation"] is None
    assert data["reference_changed_since_generation"] is None
