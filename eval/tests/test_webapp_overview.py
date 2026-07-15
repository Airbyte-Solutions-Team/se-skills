"""Deterministic tests for the `/api/overview` aggregation rules.

These tests call `_build_overview` directly with a monkeypatched `CUSTOMERS_DIR`
and `TEAM_FILE` so they do not touch the real workspace. They verify that the
landing-page aggregation keeps human review workflow state (from
`.feedback.jsonl`) distinct from output validation state (from `.md.json`), and
that malformed or legacy records do not crash the endpoint.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import webapp.app as app


def _write_team(tmp_path: Path) -> None:
    team = tmp_path / "team-members.yaml"
    team.write_text("members:\n  - id: gary\n    name: Gary\n    email: gary@example.com\n")


def _write_output(
    customers: Path,
    account: str,
    opp: str | None,
    skill: str,
    filename: str,
    sidecar: dict | None = None,
    feedback_lines: list[str] | None = None,
) -> Path:
    d = customers / account
    if opp:
        d = d / "opportunities" / opp
    d = d / "outputs" / skill
    d.mkdir(parents=True, exist_ok=True)
    md = d / filename
    md.write_text("# Output\n", encoding="utf-8")
    if sidecar is not None:
        md.with_suffix(".md.json").write_text(json.dumps(sidecar), encoding="utf-8")
    if feedback_lines is not None:
        md.with_suffix(".feedback.jsonl").write_text("\n".join(feedback_lines) + "\n", encoding="utf-8")
    return md


def _overview(tmp_path: Path, monkeypatch, jobs: dict | None = None) -> dict:
    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    _write_team(tmp_path)
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(app, "TEAM_FILE", tmp_path / "team-members.yaml")
    monkeypatch.setattr(app, "SE_CONFIG", tmp_path / ".se-config.yaml")
    return app._build_overview(jobs or {})


def test_overview_valid_output_awaits_review(tmp_path, monkeypatch) -> None:
    """A valid sidecar with no feedback sidecar is awaiting human review."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    assert data["summary"]["outputs"] == 1
    assert len(data["attention"]) == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["status"] == "awaiting review"
    assert item["validation_status"] == "valid"
    assert item["review_status"] == "awaiting review"
    assert item["skill"] == "next-move"
    assert item["account"] == "Acme"


def test_overview_approved_output_not_attention(tmp_path, monkeypatch) -> None:
    """An approved output with valid metadata needs no attention."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
        feedback_lines=[json.dumps({"action": "approve", "comment": "", "author": "Ada"})],
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 0
    assert data["summary"]["outputs"] == 1
    assert data["attention"] == []


def test_overview_commented_output_awaits_review(tmp_path, monkeypatch) -> None:
    """A comment keeps the output in the review queue."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
        feedback_lines=[json.dumps({"action": "comment", "comment": "Check numbers."})],
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["review_status"] == "commented"
    assert item["status"] == "commented"


def test_overview_corrected_output_awaits_review(tmp_path, monkeypatch) -> None:
    """A correction request keeps the output in the review queue."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
        feedback_lines=[json.dumps({"action": "correct", "comment": "Fix deployment."})],
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["review_status"] == "corrected"
    assert item["status"] == "corrected"


def test_overview_invalid_output_needs_attention(tmp_path, monkeypatch) -> None:
    """An invalid output is a validation issue, not a review label."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": False, "validation_status": "invalid", "validation_errors": ["Missing source coverage"]},
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "attention"
    assert item["validation_status"] == "invalid"
    assert item["review_status"] == "awaiting review"
    assert item["level"] == "error"


def test_overview_stale_output_needs_attention(tmp_path, monkeypatch) -> None:
    """A stale reference output is flagged as attention (validation)."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={
            "valid": True,
            "validation_status": "valid",
            "reference_freshness_at_generation": [{"fresh": False, "label": " Gong"}],
        },
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "attention"
    assert item["validation_status"] == "stale"
    assert item["review_status"] == "awaiting review"


def test_overview_unvalidated_output_awaits_review(tmp_path, monkeypatch) -> None:
    """A successfully parsed output from a skill with no schema is unvalidated
    but does not need validation attention; it still awaits human review."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "unvalidated"},
    )
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    assert len(data["attention"]) == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["status"] == "awaiting review"
    assert item["validation_status"] == "unvalidated"
    assert item["review_status"] == "awaiting review"


def test_overview_legacy_output_without_modern_metadata(tmp_path, monkeypatch) -> None:
    """An output with no `.md.json` sidecar is validation `unknown` and awaiting review."""
    customers = tmp_path / "customers"
    _write_output(customers, "Acme", None, "next-move", "legacy.md")
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["validation_status"] == "unknown"
    assert item["review_status"] == "awaiting review"
    assert item["status"] == "awaiting review"


def test_overview_malformed_sidecar_and_feedback_are_skipped(tmp_path, monkeypatch) -> None:
    """Malformed `.md.json` and `.feedback.jsonl` do not crash the overview."""
    customers = tmp_path / "customers"
    md = _write_output(customers, "Acme", None, "next-move", "broken.md")
    md.with_suffix(".md.json").write_text("not json")
    md.with_suffix(".feedback.jsonl").write_text("not json\n")

    data = _overview(tmp_path, monkeypatch)
    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["validation_status"] == "unknown"
    assert item["review_status"] == "awaiting review"


def test_overview_skips_missing_output_file(tmp_path, monkeypatch) -> None:
    """If an output file disappears after it is globbed, stat failure is skipped."""
    customers = tmp_path / "customers"
    d = customers / "Acme" / "outputs" / "next-move"
    d.mkdir(parents=True, exist_ok=True)
    md = d / "next-move-2026-07-14.md"
    md.write_text("# Output\n")
    # _collect_output is called with a real Path; if we delete the file after
    # constructing the path but before the call, it should return without raising.
    md.unlink()
    meta = {"output_count": 0, "last_updated_ts": 0.0, "last_output": None, "needs_attention": 0, "opp_slugs": set()}
    recent = []
    attention = []
    app._collect_output("Acme", None, "next-move", md, meta, recent, attention)

    assert meta["output_count"] == 0
    assert meta["needs_attention"] == 0
    assert recent == []
    assert attention == []


def test_overview_resilient_to_bad_account_directory(tmp_path, monkeypatch) -> None:
    """A failure reading one account directory does not prevent reading another."""
    customers = tmp_path / "customers"
    _write_output(
        customers,
        "Beta",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
    )
    # Create an account entry that is a file (not a directory) so the loop's
    # `is_dir()` check naturally skips it without raising.
    (customers / "BadActor").write_text("not a directory")
    data = _overview(tmp_path, monkeypatch)

    assert data["summary"]["outputs"] == 1
    assert data["summary"]["needs_attention"] == 1  # awaiting review
    assert len(data["attention"]) == 1
    assert data["attention"][0]["account"] == "Beta"


def test_overview_legacy_jobs_without_timestamps_degrade(tmp_path, monkeypatch) -> None:
    """Legacy job records without `started_at` or `finished_at` do not break rendering."""
    jobs = {
        "j1": {"status": "done", "ok": True, "skill": "next-move", "account": "Acme"},
        "j2": {"status": "error", "ok": False, "skill": "next-move", "account": "Acme"},
    }
    data = _overview(tmp_path, monkeypatch, jobs=jobs)

    assert data["summary"]["recent_failures"] == 1
    assert len(data["recent"]) == 2
    recent_types = {r["type"] for r in data["recent"]}
    assert "job_done" in recent_types
    assert "job_error" in recent_types


def test_api_overview_returns_safe_fallback_on_unexpected_failure(tmp_path, monkeypatch) -> None:
    """The HTTP endpoint returns an empty-but-renderable payload if aggregation fails."""
    import asyncio

    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    _write_team(tmp_path)
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(app, "TEAM_FILE", tmp_path / "team-members.yaml")
    monkeypatch.setattr(app, "SE_CONFIG", tmp_path / ".se-config.yaml")

    async def _run():
        return await app.api_overview()

    monkeypatch.setattr(app, "_build_overview", lambda _jobs: (_ for _ in [1]).throw(OSError("boom")))
    result = asyncio.run(_run())
    assert result["summary"]["members"] == 0
    assert result["attention"] == []
    assert result["recent"] == []
    assert result["members"] == []
