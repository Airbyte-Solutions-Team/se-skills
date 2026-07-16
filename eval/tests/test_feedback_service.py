"""Deterministic tests for `FeedbackService` and the feedback routes layer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.feedback as feedback_routes
from services.feedback_service import FeedbackError, FeedbackService, OutputFeedback


def _make_md(customers_dir: Path, rel: str = "Acme/outputs/next-move/next-move-2026-07-14.md") -> Path:
    f = customers_dir / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Output\n", encoding="utf-8")
    return f


def test_read_feedback_empty(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    resp = svc.read_feedback(rel)
    assert resp["path"] == rel
    assert resp["entries"] == []


def test_add_feedback_appends_jsonl(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    svc.add_feedback(OutputFeedback(path=rel, action="comment", comment="Check metrics."))
    entries = svc.read_feedback(rel)["entries"]
    assert len(entries) == 1
    assert entries[0]["action"] == "comment"


def test_add_feedback_rejects_nonexistent(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    with pytest.raises(FeedbackError) as exc:
        svc.add_feedback(OutputFeedback(path="missing.md", action="comment"))
    assert exc.value.status_code == 404


def test_add_feedback_rejects_non_md(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    f = tmp_path / "Acme" / "outputs" / "next-move" / "handover.html"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<html></html>")
    with pytest.raises(FeedbackError) as exc:
        svc.add_feedback(OutputFeedback(path=str(f.relative_to(tmp_path)), action="comment"))
    assert exc.value.status_code == 400


def test_feedback_model_max_lengths() -> None:
    with pytest.raises(Exception):
        OutputFeedback(path="x" * 501, action="comment")
    with pytest.raises(Exception):
        OutputFeedback(path="ok.md", action="comment", comment="x" * 2_001)
    with pytest.raises(Exception):
        OutputFeedback(path="ok.md", action="comment", author="x" * 101)


def test_route_feedback_get(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(feedback_service=svc)))
    resp = feedback_routes.api_output_feedback_get(rel, req)
    assert resp["entries"] == []


def test_route_feedback_post(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.md"
    _make_md(tmp_path, rel)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(feedback_service=svc)))
    body = OutputFeedback(path=rel, action="approve")
    resp = feedback_routes.api_output_feedback_post(body, req)
    assert resp["entry"]["action"] == "approve"


def test_route_feedback_404_on_missing(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(feedback_service=svc)))
    with pytest.raises(HTTPException) as exc:
        feedback_routes.api_output_feedback_get("missing.md", req)
    assert exc.value.status_code == 404


def test_feedback_traversal_rejected(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    with pytest.raises(FeedbackError) as exc:
        svc.read_feedback("../etc/passwd")
    assert exc.value.status_code == 404


def test_feedback_absolute_path_rejected(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    with pytest.raises(FeedbackError) as exc:
        svc.read_feedback("/etc/passwd")
    assert exc.value.status_code == 404


def test_feedback_symlinked_file_outside_rejected(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = "Acme/outputs/skill/file.md"
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Safe\n", encoding="utf-8")

    outside = tmp_path.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    link = tmp_path / "Acme" / "outputs" / "skill" / "link.md"
    link.symlink_to(outside)

    with pytest.raises(FeedbackError) as exc:
        svc.read_feedback("Acme/outputs/skill/link.md")
    assert exc.value.status_code == 404


def test_feedback_routes_expose_original_path() -> None:
    paths = {(r.path, tuple(sorted(r.methods))) for r in feedback_routes.router.routes}
    assert ("/api/output/feedback", ("GET",)) in paths
    assert ("/api/output/feedback", ("POST",)) in paths
