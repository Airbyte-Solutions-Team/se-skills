"""Deterministic tests for the output diff endpoint in `webapp/app.py`.

These tests call the FastAPI route function directly so they do not need an
HTTP client. They verify that two `.md` outputs can be compared line-by-line.
"""

import pytest

import webapp.app as app
from webapp.app import OutputDiff, _diff_lines, api_output_diff


def test_diff_lines_detects_equal_insert_delete_replace() -> None:
    left = "A\nB\nC\n"
    right = "A\nX\nC\nD\n"
    rows = _diff_lines(left, right)
    types = [r["type"] for r in rows]
    assert types == ["equal", "replace", "equal", "insert"]
    assert rows[0]["left"] == rows[0]["right"] == "A"
    assert rows[2]["left"] == rows[2]["right"] == "C"
    assert rows[3]["left"] is None and rows[3]["right"] == "D"


def test_output_diff_returns_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "CUSTOMERS_DIR", tmp_path)
    left_rel = "Acme/outputs/deal-assessment/deal-assessment-2026-07-10.md"
    right_rel = "Acme/outputs/deal-assessment/deal-assessment-2026-07-14.md"
    (tmp_path / left_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / left_rel).write_text("# Acme Deal Assessment\n\n- Probability: ==40-60%==\n")
    (tmp_path / right_rel).write_text("# Acme Deal Assessment\n\n- Probability: ==60-80%==\n")

    resp = api_output_diff(body=OutputDiff(left=left_rel, right=right_rel))
    assert resp["left"] == left_rel
    assert resp["right"] == right_rel
    assert "rows" in resp
    rows = resp["rows"]
    assert any(r["type"] == "equal" for r in rows)
    assert any(r["type"] == "replace" for r in rows)


def test_output_diff_rejects_nonexistent_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "CUSTOMERS_DIR", tmp_path)
    with pytest.raises(Exception):
        api_output_diff(body=OutputDiff(left="missing.md", right="missing2.md"))


def test_output_diff_rejects_path_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "CUSTOMERS_DIR", tmp_path)
    (tmp_path / "Acme" / "outputs" / "deal-assessment").mkdir(parents=True, exist_ok=True)
    f = tmp_path / "Acme" / "outputs" / "deal-assessment" / "deal-assessment-2026-07-14.md"
    f.write_text("# ok")
    with pytest.raises(Exception):
        api_output_diff(body=OutputDiff(left="../etc/passwd", right=str(f.relative_to(tmp_path))))


def test_output_diff_model_max_lengths() -> None:
    with pytest.raises(Exception):
        OutputDiff(left="x" * 501, right="ok.md")
    with pytest.raises(Exception):
        OutputDiff(left="ok.md", right="x" * 501)
