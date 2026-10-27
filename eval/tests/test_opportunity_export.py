from __future__ import annotations

import time

import pytest

import pdf_render
from eval.tests.test_opportunity_state_routes import _client
from opportunity_export import export_filename, render_export
from opportunity_print import render_print_html

BASE = "/api/accounts/Acme/opportunities/synthetic-opportunity/overview"


def _create_overview(client, evidence_id) -> None:
    started = client.post(f"{BASE}/create", json={"transcript_ids": [evidence_id]})
    assert started.status_code == 202
    for _ in range(200):
        job = client.get(f"{BASE}/jobs/{started.json()['job_id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.01)
    assert job["ok"] is True


def _current_version(client):
    return client.app.state.opportunity_state_service.read_current("Acme", "synthetic-opportunity")


def _identity(client) -> dict:
    import asyncio

    return asyncio.run(
        client.app.state.opportunity_state_create_service.resolve_identity("Acme", "synthetic-opportunity")
    )


def test_export_requires_an_existing_overview(tmp_path) -> None:
    client, _ = _client(tmp_path)
    response = client.get(f"{BASE}/export?format=html")
    assert response.status_code == 404


def test_export_rejects_unknown_format(tmp_path) -> None:
    client, _ = _client(tmp_path)
    assert client.get(f"{BASE}/export?format=docx").status_code == 422


def test_export_html_downloads_workspace_style_page(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)

    response = client.get(f"{BASE}/export?format=html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"] == (
        'attachment; filename="synthetic-opportunity-opportunity-brief-r1.html"'
    )
    page = response.text
    # Same document the PDF is printed from: workspace sections and class names.
    for marker in ("workspace-hero", "Opportunity Brief", "Health indicators", "MEDDPICC",
                   "Stakeholders", "Evidence and missing information", "overview-brief-grid",
                   "Customer objective", "@media screen"):
        assert marker in page
    assert "<script" not in page and "https://" not in page  # self-contained, no external assets
    # Default format is HTML.
    assert client.get(f"{BASE}/export").headers["content-type"].startswith("text/html")


def test_export_pdf_mirrors_workspace_layout_and_sets_pdf_headers(tmp_path, monkeypatch) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)
    seen: dict[str, str] = {}

    def fake_render(html_doc: str) -> bytes:
        seen["html"] = html_doc
        return b"%PDF-fake"

    monkeypatch.setattr(pdf_render, "render_html_pdf", fake_render)
    response = client.get(f"{BASE}/export?format=pdf")

    assert response.status_code == 200
    assert response.content == b"%PDF-fake"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].endswith('-r1.pdf"')
    page = seen["html"]
    # Same sections, in the same order, and with the workspace's own class names.
    order = [
        "workspace-hero", "Opportunity Brief", "Health indicators", "Top Risks",
        "Recommended Actions", "Business Case", "MEDDPICC", "Stakeholders",
        "Evidence and missing information",
    ]
    positions = [page.index(marker) for marker in order]
    assert positions == sorted(positions)
    for cls in ("overview-brief-grid", "overview-health-grid", "overview-two-column",
                "overview-business-case-quad", "overview-meddpicc-dimensions"):
        assert cls in page


def test_export_pdf_reports_missing_chrome_as_503(tmp_path, monkeypatch) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)

    def no_chrome(_html: str) -> bytes:
        raise RuntimeError("No Chrome/Chromium found for PDF rendering.")

    monkeypatch.setattr(pdf_render, "render_html_pdf", no_chrome)
    response = client.get(f"{BASE}/export?format=pdf")

    assert response.status_code == 503
    assert "Chrome" in response.json()["detail"]


def test_model_authored_text_is_escaped(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)
    version = _current_version(client)
    version.state.brief.customer_objective.value = "<script>alert(1)</script> ==hot== | a | b |"
    version.state.recommended_actions[0].action = "<img src=x onerror=alert(1)> **bold**"

    page = render_export(version, "html", account={"name": "<i>Acme</i>"}, opportunity={"name": "<b>Deal</b>"})

    assert "<script>alert(1)</script>" not in page
    assert "<img src=x" not in page
    assert "<b>Deal</b>" not in page and "&lt;b&gt;Deal&lt;/b&gt;" in page
    assert "<i>Acme</i>" not in page


def test_header_facts_and_evidence_names_only(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    _create_overview(client, evidence_id)
    version = _current_version(client)

    page = render_print_html(
        version,
        account={"name": "Acme", "owner_name": "Pat SE"},
        opportunity={
            "name": "Acme Expansion", "stage": "Technical Validation", "stage_num": "3",
            "amount": 120000, "close_date": "2026-12-01", "type": "New Business", "ae": "Sam AE",
        },
    )

    assert "<h1>Acme Expansion</h1>" in page
    for fact in ("3 · Technical Validation", "$120,000", "Target close 2026-12-01",
                 "Owner Pat SE", "AE Sam AE", "Revision 1"):
        assert fact in page
    # Evidence is listed by display name only -- never hashes.
    assert version.evidence_manifest[0].sha256 not in page


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("latecoere-Opportunity-2026-06-01", "latecoere-Opportunity-2026-06-01-opportunity-brief-r4.pdf"),
        ('we"ird/../slug', "we-ird-slug-opportunity-brief-r4.pdf"),
        ("///", "opportunity-opportunity-brief-r4.pdf"),
    ],
)
def test_export_filename_is_header_safe(slug: str, expected: str) -> None:
    assert export_filename(slug, 4, "pdf") == expected
