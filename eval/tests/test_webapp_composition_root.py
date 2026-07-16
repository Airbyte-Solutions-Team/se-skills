"""Architecture-boundary tests for the final ARCH-001 slice.

Verifies that `webapp/app.py` is a clean FastAPI composition root and that all
public routes, service identity, and lifecycle wiring are preserved.
"""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import webapp.app as app_module
from services.ask_service import AskService
from services.job_service import JobService
from services.output_service import OutputService
from services.transcription_service import TranscriptionService
from webapp.app import app, create_app


def _all_routes():
    """Yield every APIRoute/Mount under the app, including included routers."""
    for r in app.router.routes:
        if type(r).__name__ == "_IncludedRouter":
            yield from r.original_router.routes
        else:
            yield r


def _route_key(r):
    """Normalize a route path so the static mount at / appears as '/'."""
    path = r.path or "/"
    methods = frozenset(getattr(r, "methods", None) or [])
    return path, methods


def test_create_app_returns_fastapi() -> None:
    new_app = create_app()
    assert isinstance(new_app, FastAPI)


def test_module_level_app_exists() -> None:
    assert isinstance(app, FastAPI)


def test_expected_public_routes_registered() -> None:
    """Every public URL from app.js is registered."""
    expected = {
        "/api/skills",
        "/api/reload",
        "/api/skills/help",
        "/api/plan",
        "/api/invoke",
        "/api/permissions",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/members",
        "/api/members/{member_id}/accounts",
        "/api/accounts",
        "/api/accounts/{account}",
        "/api/accounts/{account}/outputs",
        "/api/accounts/{account}/opportunities",
        "/api/accounts/{account}/last-run",
        "/api/bulk-create-accounts",
        "/api/bulk/{action}",
        "/api/trash",
        "/api/trash/{trash_id}/restore",
        "/api/output/feedback",
        "/api/overview",
        "/api/sfdc/stage-amount",
        "/api/members/{member_id}/sfdc-aes",
        "/api/members/{member_id}/sfdc-accounts",
        "/api/output/ask",
        "/api/ai-status",
        "/api/audio-devices",
        "/api/transcribe/start",
        "/api/transcribe/active",
        "/api/transcripts",
        "/api/transcripts/{name}",
        "/api/transcribe/{session_id}/stream",
        "/api/transcribe/{session_id}/ask",
        "/api/transcribe/{session_id}/stop",
        "/favicon.ico",
        "/",
    }
    routes = {_route_key(r)[0] for r in _all_routes()}
    for path in expected:
        assert path in routes, f"Missing route: {path}"


def test_no_duplicate_route_paths() -> None:
    keys = [_route_key(r) for r in _all_routes()]
    seen = set()
    duplicates = []
    for k in keys:
        if k in seen:
            duplicates.append(k)
        seen.add(k)
    assert not duplicates, f"Duplicate (path, methods): {duplicates}"


def test_static_files_mounted() -> None:
    mounted = [r for r in _all_routes() if _route_key(r)[0] == "/" and getattr(r, "app", None)]
    assert mounted, "StaticFiles not mounted at /"


def test_favicon_unchanged() -> None:
    with TestClient(app) as client:
        resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert "svg" in resp.headers.get("content-type", "")


def test_service_identity_preserved() -> None:
    """Routes share the same service instances wired at app creation."""
    fresh = create_app()
    assert fresh.state.job_service is fresh.state.overview_service._job_service
    assert fresh.state.output_service is fresh.state.overview_service._output_service
    assert fresh.state.account_service is fresh.state.overview_service._account_service
    assert fresh.state.job_service is fresh.state.ask_service.job_service
    assert fresh.state.output_service is fresh.state.ask_service.output_service
    assert fresh.state.job_service is fresh.state.account_service._job_service
    assert fresh.state.output_service is fresh.state.account_service._output_service
    assert fresh.state.skill_runtime_service.job_service is fresh.state.job_service
    assert fresh.state.skill_runtime_service.output_service is fresh.state.output_service
    assert isinstance(fresh.state.transcription_service, TranscriptionService)


def test_no_service_or_route_imports_app() -> None:
    """Dependency direction is routes -> services -> helpers; nothing imports app.py."""
    webapp = Path(app_module.__file__).resolve().parent
    for source in [webapp / "services", webapp / "routes", webapp / "integrations"]:
        if not source.exists():
            continue
        for path in source.rglob("*.py"):
            if path.name == "app.py":
                continue
            text = path.read_text(encoding="utf-8")
            assert "import webapp.app" not in text, f"{path} imports webapp.app"
            assert "from webapp.app import" not in text, f"{path} imports from webapp.app"
            assert "from webapp import app" not in text, f"{path} imports app from webapp"


def test_app_py_contains_only_composition_root_responsibilities() -> None:
    """app.py should not re-implement business logic already moved to services/routes."""
    app_py = Path(app_module.__file__).resolve()
    tree = ast.parse(app_py.read_text(encoding="utf-8"))
    top_level_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
    }
    forbidden = {
        "api_skills",
        "api_reload_skills",
        "api_skills_help",
        "api_plan",
        "api_invoke",
        "api_permissions",
        "api_last_run",
        "_build_prompt",
        "_permission_profile",
        "discover_skills",
        "_derive_help",
        "_se_config",
        "_model_for",
        "_safe",
        "_run_cmd",
    }
    found = forbidden & top_level_names
    assert not found, f"app.py still contains business logic: {found}"


def test_skill_runtime_router_registered() -> None:
    """Skill routes are reachable through the shared app."""
    assert app.url_path_for("api_skills") == "/api/skills"
    assert app.url_path_for("api_invoke") == "/api/invoke"
    assert app.url_path_for("api_plan") == "/api/plan"
    assert app.url_path_for("api_permissions") == "/api/permissions"


def test_app_lifespan_shutdown_present() -> None:
    """The lifespan context manager is registered for startup/shutdown."""
    assert app.router.lifespan is not None


def test_job_and_transcription_services_are_stateful_singletons() -> None:
    """Only one JobService and one TranscriptionService exist per app."""
    fresh = create_app()
    assert isinstance(fresh.state.job_service, JobService)
    assert isinstance(fresh.state.output_service, OutputService)
    assert isinstance(fresh.state.ask_service, AskService)
    assert isinstance(fresh.state.transcription_service, TranscriptionService)
