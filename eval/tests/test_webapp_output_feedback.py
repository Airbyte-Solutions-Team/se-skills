"""Deterministic tests for the feedback lifecycle service.

These tests call `FeedbackService` directly so they do not need an HTTP client.
They verify that SE feedback (approve / comment / correct) is stored as a sidecar
JSONL file next to the output and can be retrieved.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from webapp.services.feedback_service import FeedbackError, FeedbackService, OutputFeedback


def _make_output(customers_dir: Path, rel_path: str = "Acme/outputs/next-move/next-move-2026-07-14.md") -> str:
    f = customers_dir / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Sample output\n\nBody.\n")
    return rel_path


def test_feedback_get_returns_empty_when_no_feedback_exists(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = _make_output(tmp_path)
    resp = svc.read_feedback(rel)
    assert resp["path"] == rel
    assert resp["entries"] == []


def test_feedback_post_records_entry(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = _make_output(tmp_path)

    resp = svc.add_feedback(
        OutputFeedback(path=rel, action="correct", comment="Fix the deployment model assumption.", author="Ada")
    )
    assert resp["path"] == rel
    entry = resp["entry"]
    assert entry["action"] == "correct"
    assert entry["comment"] == "Fix the deployment model assumption."
    assert entry["author"] == "Ada"
    assert "timestamp" in entry
    datetime.fromisoformat(entry["timestamp"])

    sidecar = (tmp_path / rel).with_suffix(".feedback.jsonl")
    assert sidecar.is_file()

    get_resp = svc.read_feedback(rel)
    assert len(get_resp["entries"]) == 1
    assert get_resp["entries"][0]["action"] == "correct"


def test_feedback_post_appends_multiple_entries(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = _make_output(tmp_path)

    svc.add_feedback(OutputFeedback(path=rel, action="approve"))
    svc.add_feedback(OutputFeedback(path=rel, action="comment", comment="Looks good but double-check metrics."))

    get_resp = svc.read_feedback(rel)
    assert [e["action"] for e in get_resp["entries"]] == ["approve", "comment"]


def test_feedback_post_rejects_nonexistent_output(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    with pytest.raises(FeedbackError):
        svc.add_feedback(OutputFeedback(path="Acme/outputs/missing.md", action="comment"))


def test_feedback_post_rejects_non_md_files(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = "Acme/outputs/next-move/next-move-2026-07-14.pdf"
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("not a markdown output")
    with pytest.raises(FeedbackError):
        svc.add_feedback(OutputFeedback(path=rel, action="comment"))


def test_feedback_model_rejects_invalid_action() -> None:
    with pytest.raises(Exception):
        OutputFeedback(path="ok.md", action="reject")


def test_feedback_model_enforces_max_lengths() -> None:
    with pytest.raises(Exception):
        OutputFeedback(path="x" * 501, action="comment")
    with pytest.raises(Exception):
        OutputFeedback(path="ok.md", action="comment", comment="x" * 2_001)
    with pytest.raises(Exception):
        OutputFeedback(path="ok.md", action="comment", author="x" * 101)


def test_feedback_get_ignores_malformed_jsonl_lines(tmp_path: Path) -> None:
    svc = FeedbackService(tmp_path)
    rel = _make_output(tmp_path)
    sidecar = (tmp_path / rel).with_suffix(".feedback.jsonl")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"action":"comment","comment":"ok"}\nnot json\n{"action":"approve"}\n')

    resp = svc.read_feedback(rel)
    assert [e["action"] for e in resp["entries"]] == ["comment", "approve"]
