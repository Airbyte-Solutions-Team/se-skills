"""HTTP boundary tests for the local-only Tech Eval tracker."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.tech_eval import router
from services.tech_eval_service import TechEvalService


def _safe(value: str) -> str:
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError("unsafe")
    return value


class ResolvedWorkspace:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve_identity(self, account: str, opp_slug: str) -> dict:
        self.calls.append((account, opp_slug))
        return {"safe_account": "Acme", "safe_opp": "canonical-opportunity"}


def _client(tmp_path) -> tuple[TestClient, ResolvedWorkspace]:
    (tmp_path / "customers" / "Acme").mkdir(parents=True)
    app = FastAPI()
    workspace = ResolvedWorkspace()
    app.state.opportunity_workspace_service = workspace
    app.state.tech_eval_service = TechEvalService(tmp_path / "customers", safe_name=_safe)
    app.include_router(router)
    return TestClient(app), workspace


def test_routes_resolve_identity_server_side_and_persist(tmp_path) -> None:
    client, workspace = _client(tmp_path)
    initial = client.get("/api/accounts/untrusted/opportunities/untrusted/tech-eval")
    assert initial.status_code == 200
    assert initial.json()["identity"] == {
        "account": "Acme", "opportunity_slug": "canonical-opportunity"
    }

    updated = client.patch(
        "/api/accounts/untrusted/opportunities/untrusted/tech-eval/items/customer_success_criteria",
        json={"status": "blocked", "owner": "SE", "note": "Awaiting approval"},
    )
    assert updated.status_code == 200
    assert updated.json()["summary"]["overall_state"] == "blocked"
    assert updated.json()["last_change_source"] == "manual"
    assert workspace.calls == [("untrusted", "untrusted"), ("untrusted", "untrusted")]

    restarted = TechEvalService(tmp_path / "customers", safe_name=_safe)
    client.app.state.tech_eval_service = restarted
    loaded = client.get("/api/accounts/untrusted/opportunities/untrusted/tech-eval")
    item = next(item for item in loaded.json()["items"] if item["id"] == "customer_success_criteria")
    assert item["status"] == "blocked"


def test_routes_reject_invalid_status_item_and_requests(tmp_path) -> None:
    client, _workspace = _client(tmp_path)
    base = "/api/accounts/Acme/opportunities/canonical-opportunity/tech-eval/items"
    assert client.patch(f"{base}/technical_champion", json={"status": "ready"}).status_code == 422
    assert client.patch(f"{base}/technical_champion", json={}).status_code == 422
    assert client.patch(
        f"{base}/technical_champion", json={"status": "done", "evidence": "forbidden"}
    ).status_code == 422
    assert client.patch(f"{base}/made-up", json={"status": "done"}).status_code == 404


def test_hosted_mode_registers_no_tech_eval_route(repo_root) -> None:
    env = {
        **os.environ,
        "HOSTED_MODE": "1",
        "PYTHONPATH": f"{repo_root}{os.pathsep}{repo_root / 'webapp'}",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key",
        "HOSTED_JWT_ALGORITHM": "HS256",
        "HOSTED_JWT_SECRET": "super-secret-32-byte-test-jwt-key!",
        "SUPABASE_JWT_SECRET": "super-secret-32-byte-test-storage-jwt-key!",
        "HOSTED_CONTEXT_SECRET": "test-context-secret-32-bytes!!",
    }
    probe = (
        "import json, webapp.app as a;"
        "print(json.dumps([getattr(r, 'path', '') for r in a.app.routes]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=repo_root, env=env,
        check=True, capture_output=True, text=True,
    )
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert paths
    assert not [path for path in paths if "tech-eval" in path]
