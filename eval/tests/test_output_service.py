"""Deterministic tests for `OutputService` and the output routes layer.

These tests exercise download/delete/render paths and verify that the FastAPI
router in `webapp/routes/outputs.py` keeps the original URL/method surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

import routes.feedback as feedback_routes
import routes.outputs as outputs_routes
from routes.outputs import OutputDiff, OutputGolden, OutputPdf, OutputRender
from services.feedback_service import FeedbackService, OutputFeedback
from services.output_service import OutputError, OutputService


def _svc(customers_dir: Path) -> OutputService:
    return OutputService(
        customers_dir=customers_dir,
        workspace=customers_dir,
        repo_root=customers_dir,
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n.replace(" ", "-").lower(),
        run_cmd=None,
        internal_repo=None,
    )


def _request(svc: OutputService | None = None, feedback_svc: FeedbackService | None = None) -> SimpleNamespace:
    state = SimpleNamespace(output_service=svc, feedback_service=feedback_svc)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _make_md(customers_dir: Path, rel: str = "Acme/outputs/next-move/next-move-2026-07-14.md") -> Path:
    f = customers_dir / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Acme — next-move\n\n- Item A\n", encoding="utf-8")
    return f


def test_read_output_content_returns_markdown(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    assert "# Acme" in svc.read_output_content(rel)


def test_read_output_html_serves_html_output(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/handover/handover.html"
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<!DOCTYPE html><html><body>Handoff</body></html>", encoding="utf-8")
    html = svc.read_output_html(rel)
    assert "Handoff" in html


def test_export_internal_html_returns_html_and_filename(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    doc, filename = svc.export_internal_html(rel)
    assert "<html" in doc
    assert filename == "next-move-2026-07-14.html"


def test_export_pdf_raises_without_chrome(tmp_path: Path, monkeypatch) -> None:
    import services.output_service as output_service_module

    monkeypatch.setattr(output_service_module.pdf_render, "find_chrome", lambda: None)
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    with pytest.raises(OutputError) as exc:
        svc.export_pdf(rel)
    assert exc.value.status_code == 503


def test_delete_output_moves_md_to_trash(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    md = _make_md(tmp_path, rel)
    result = svc.delete_output(rel)
    assert result["deleted"] is True
    assert not md.exists()
    assert (tmp_path / "_trash").is_dir()


def test_delete_output_moves_html_to_trash(tmp_path: Path) -> None:
    # Coverage-handoff outputs are .html; they must be deletable (moved to _trash).
    svc = _svc(tmp_path)
    rel = "Acme/outputs/coverage-handoff/coverage-handoff-2026-07-07-Acme.html"
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<!DOCTYPE html><html><body>Handoff</body></html>", encoding="utf-8")
    result = svc.delete_output(rel)
    assert result["deleted"] is True
    assert not f.exists()
    assert (tmp_path / "_trash").is_dir()


def test_delete_output_rejects_other_types(tmp_path: Path) -> None:
    # Only .md and .html are deletable here; sidecars (.json) and anything else are rejected.
    svc = _svc(tmp_path)
    f = tmp_path / "Acme" / "outputs" / "next-move" / "next-move-2026-07-14.md.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}")
    with pytest.raises(OutputError) as exc:
        svc.delete_output(str(f.relative_to(tmp_path)))
    assert exc.value.status_code == 400


def test_render_markdown_converts_to_html() -> None:
    html = OutputService.render_markdown("# Hello\n\nWorld.")
    assert "<h1" in html
    assert "Hello" in html


def test_repo_path_returns_expected_structure(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    result = svc.repo_path("Acme Co", member="Gary Yang")
    assert result["account_slug"] == "acme-co"
    assert result["member_slug"] == "gary-yang"
    assert result["relative"] == "accounts/acme-co/index.html"
    assert result["full"].endswith("gary-yang/accounts/acme-co/index.html")


def test_golden_manifests_returns_phase1_scenarios(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    result = svc.golden_manifests("tech-qual")
    assert "phase1-missing-technical-input" in result["scenarios"]


def test_route_get_output_content(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    text = outputs_routes.api_output_content(rel, _request(svc))
    assert "# Acme" in text


def test_route_get_output_meta(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    data = outputs_routes.api_output_meta(rel, _request(svc))
    assert data["skill"] == "next-move"
    assert "validation_status" in data


def test_route_delete_output(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    result = outputs_routes.api_delete_output(rel, _request(svc))
    assert result["deleted"] is True


def test_route_pdf_without_chrome_raises_503(tmp_path: Path, monkeypatch) -> None:
    import services.output_service as output_service_module

    monkeypatch.setattr(output_service_module.pdf_render, "find_chrome", lambda: None)
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    with pytest.raises(HTTPException) as exc:
        outputs_routes.api_output_pdf(rel, _request(svc))
    assert exc.value.status_code == 503


def test_route_internal_html(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    resp = outputs_routes.api_output_internal_html(rel, _request(svc))
    assert resp.media_type == "text/html; charset=utf-8"
    body = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    assert "<html" in body


def test_route_pdf_post_without_chrome_raises_503(tmp_path: Path, monkeypatch) -> None:
    import services.output_service as output_service_module

    monkeypatch.setattr(output_service_module.pdf_render, "find_chrome", lambda: None)
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    with pytest.raises(HTTPException) as exc:
        outputs_routes.api_output_pdf_post(OutputPdf(path=rel), _request(svc))
    assert exc.value.status_code == 503


def test_route_render_markdown() -> None:
    result = outputs_routes.api_output_render(OutputRender(md="# Title"))
    assert "<h1" in result.html


def test_route_repo_path(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    result = outputs_routes.api_output_repo_path("Acme", _request(svc), member="Gary")
    assert result["account_slug"] == "acme"


def test_route_golden_manifests(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    result = outputs_routes.api_golden_manifests("tech-qual", _request(svc))
    assert "phase1-missing-technical-input" in result["scenarios"]


def test_route_diff_returns_rows(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    left = "Acme/outputs/deal-assessment/deal-2026-07-10.md"
    right = "Acme/outputs/deal-assessment/deal-2026-07-14.md"
    (tmp_path / left).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / left).write_text("# A\n- one\n", encoding="utf-8")
    (tmp_path / right).write_text("# A\n- two\n", encoding="utf-8")
    result = outputs_routes.api_output_diff(OutputDiff(left=left, right=right), _request(svc))
    assert "rows" in result


def test_route_feedback_get_and_post(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    feedback_svc = FeedbackService(tmp_path)
    req = _request(svc, feedback_svc)

    get_resp = feedback_routes.api_output_feedback_get(rel, req)
    assert get_resp["entries"] == []

    post_resp = feedback_routes.api_output_feedback_post(
        OutputFeedback(path=rel, action="approve", comment="LGTM", author="Ada"), req
    )
    assert post_resp["entry"]["action"] == "approve"


def test_routes_preserve_original_paths() -> None:
    """The output and feedback routers expose the same URL/method surface as before extraction."""
    output_paths = {(r.path, tuple(sorted(r.methods))) for r in outputs_routes.router.routes}
    feedback_paths = {(r.path, tuple(sorted(r.methods))) for r in feedback_routes.router.routes}

    assert ("/api/output", ("GET",)) in output_paths
    assert ("/api/output/meta", ("GET",)) in output_paths
    assert ("/api/output/html", ("GET",)) in output_paths
    assert ("/api/output/repo-path", ("GET",)) in output_paths
    assert ("/api/output/push-to-repo", ("POST",)) in output_paths
    assert ("/api/output/push-status", ("GET",)) in output_paths
    assert ("/api/output/pdf", ("GET",)) in output_paths
    assert ("/api/output/pdf", ("POST",)) in output_paths
    assert ("/api/output/internal-html", ("GET",)) in output_paths
    assert ("/api/output/render", ("POST",)) in output_paths
    assert ("/api/output", ("DELETE",)) in output_paths
    assert ("/api/golden/manifests", ("GET",)) in output_paths
    assert ("/api/output/golden", ("POST",)) in output_paths
    assert ("/api/output/diff", ("POST",)) in output_paths

    assert ("/api/output/feedback", ("GET",)) in feedback_paths
    assert ("/api/output/feedback", ("POST",)) in feedback_paths
