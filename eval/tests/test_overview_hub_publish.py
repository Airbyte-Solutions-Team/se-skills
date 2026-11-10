"""Overview -> internal hub: route wiring plus the REAL rendered Overview through the git flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from services.output_service import OutputService
from eval.tests.test_hub_publish import BASE, _git, _origin_files, env  # noqa: F401  (env is a fixture)
from eval.tests.test_opportunity_export import _create_overview
from eval.tests.test_opportunity_state_routes import _client

OVERVIEW = "/api/accounts/Acme/opportunities/synthetic-opportunity/overview"


def test_publish_requires_an_existing_overview(tmp_path) -> None:
    client, _ = _client(tmp_path)
    assert client.post(f"{OVERVIEW}/publish-to-hub", json={}).status_code == 404


def test_publish_route_renders_the_overview_and_hands_it_to_the_hub_publisher(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)
    seen = {}

    async def fake_publish(account, html, member=""):
        seen.update(account=account, html=html, member=member)
        return {"status": "published", "pr_url": "https://example/pr/1", "auto_merge": True, "targets": []}

    client.app.state.output_service = SimpleNamespace(publish_overview_to_hub=fake_publish)
    res = client.post(f"{OVERVIEW}/publish-to-hub", json={"member": "Gary Yang"})

    assert res.status_code == 200 and res.json()["pr_url"] == "https://example/pr/1"
    assert seen["account"] == "Acme" and seen["member"] == "Gary Yang"
    assert "workspace-hero" in seen["html"] and "Opportunity Brief" in seen["html"]
    assert "<script" not in seen["html"]


def test_publish_route_rejects_unknown_body_fields(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)
    # the target member/path must never be client-controllable beyond the fenced `member` check
    assert client.post(f"{OVERVIEW}/publish-to-hub", json={"path": "../../x"}).status_code == 422


def test_hub_plan_route_reports_files_and_enforces_member(tmp_path) -> None:
    client, _ = _client(tmp_path)
    client.app.state.output_service = OutputService(
        tmp_path, tmp_path, tmp_path, se_config=lambda: {}, safe_name=lambda n: n,
        slug=lambda n: n.replace(" ", "-").lower(), internal_repo=lambda: tmp_path / "no-clone",
    )
    ok = client.get(f"{OVERVIEW}/hub-plan", params={"member": "Gary Yang"})
    assert ok.status_code == 200
    plan = ok.json()
    assert plan["member"] == "gary-yang" and plan["skill"] == "overview"
    assert all(t.startswith(BASE + "/") for t in plan["targets"])
    assert client.get(f"{OVERVIEW}/hub-plan", params={"member": "Someone Else"}).status_code == 403


def test_real_rendered_overview_publishes_as_account_page(tmp_path, env) -> None:  # noqa: F811
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)
    html = client.get(f"{OVERVIEW}/export?format=html").text

    res = asyncio.run(env.svc.publish_overview_to_hub("Acme", html, "Gary Yang"))

    assert res["status"] == "published"
    assert f"{BASE}/accounts/acme/index.html" in _origin_files(env, res["branch"])
    page = _git(env.origin, "show", f"{res['branch']}:{BASE}/accounts/acme/index.html")
    assert env.svc.OVERVIEW_MARKER in page and "Opportunity Brief" in page
    # the tile sits inside the hero's facts grid, before the rest of the Overview
    assert 'class="workspace-fact hub-tile" href="outputs.html"' in page
    assert page.index("workspace-hero") < page.index("hub-tile") < page.index("Opportunity Brief")
    assert page.index("hub-tile") < page.index("</section>")  # i.e. inside the hero section
    assert f"{BASE}/accounts/acme/outputs.html" in _origin_files(env, res["branch"])
