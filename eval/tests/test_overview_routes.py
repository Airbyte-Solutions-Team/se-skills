"""Route-level tests for the `/api/overview` endpoint."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.account_service import AccountService
from services.output_service import OutputService
from services.overview_service import OverviewService
from webapp import app as app_module


def _safe_name(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-") or "unnamed"


def _titlecase(name: str) -> str:
    import re
    return "-".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9-]+", "-", (name or "").strip()).strip("-") or "opportunity"


def _build_test_overview_service(tmp_path: Path) -> OverviewService:
    import json
    from pathlib import Path

    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    (customers / "Acme" / "outputs" / "next-move").mkdir(parents=True)
    (customers / "Acme" / "outputs" / "next-move" / "a.md").write_text("# Output\n")
    team = tmp_path / "team-members.yaml"
    team.write_text("members:\n  - id: gary\n    name: Gary\n")
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    repo.mkdir(exist_ok=True)

    output_svc = OutputService(
        customers_dir=customers,
        workspace=workspace,
        repo_root=repo,
        se_config=lambda: {},
        safe_name=_safe_name,
        slug=_slug,
    )
    job_svc = SimpleNamespace(overview_jobs=lambda: {})
    account_svc = AccountService(
        customers_dir=customers,
        webapp_dir=tmp_path,
        output_service=output_svc,
        job_service=job_svc,
        safe_name=_safe_name,
        titlecase=_titlecase,
        slug=_slug,
        team_file=team,
        se_config_file=tmp_path / ".se-config.yaml",
    )
    return OverviewService(account_svc, output_svc, job_svc)


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def test_overview_route_registered_and_method_unchanged(client, tmp_path) -> None:
    """GET /api/overview returns the expected overview shape."""
    app_module.app.state.overview_service = _build_test_overview_service(tmp_path)
    response = client.get("/api/overview")
    assert response.status_code == 200
    data = response.json()
    assert "summary" in data
    assert "attention" in data
    assert "recent" in data
    assert "members" in data
    assert "empty" in data
    assert data["summary"]["members"] == 1
    assert data["summary"]["outputs"] == 1


def test_overview_route_returns_safe_fallback_on_failure(client, tmp_path) -> None:
    """A failure during aggregation returns the empty fallback payload."""
    class BrokenOverviewService:
        def build_overview(self) -> dict:
            raise OSError("boom")

    app_module.app.state.overview_service = BrokenOverviewService()
    response = client.get("/api/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["members"] == 0
    assert data["attention"] == []
    assert data["recent"] == []
    assert data["members"] == []
    assert data["empty"]["members"] is True


def test_overview_route_compatability_wrapper_not_needed(client, tmp_path) -> None:
    """The route delegates directly to OverviewService; no legacy `_build_overview` wrapper is used."""
    assert not hasattr(app_module, "_build_overview")
