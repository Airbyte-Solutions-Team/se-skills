"""Deterministic tests for the landing-page overview aggregation.

These tests exercise `OverviewService` directly with temporary workspaces so
they do not depend on the real `~/airbyte-work` directory. They cover
summary counts, job/output activity, attention rules, recent activity,
malformed-data resilience, and the 67-item ARCH-001 Slice 4 checklist.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.account_service import AccountService
from services.output_service import OutputService
from services.overview_service import OverviewService


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-") or "unnamed"


def _titlecase(name: str) -> str:
    return "-".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", (name or "").strip()).strip("-") or "opportunity"


def _write_team(tmp_path: Path, content: str | None = None) -> Path:
    team = tmp_path / "team-members.yaml"
    if content is None:
        content = "members:\n  - id: gary\n    name: Gary\n    email: gary@example.com\n"
    team.write_text(content, encoding="utf-8")
    return team


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
        md.with_suffix(".feedback.jsonl").write_text(
            "\n".join(feedback_lines) + "\n", encoding="utf-8"
        )
    return md


def _make_services(tmp_path: Path, jobs: dict | None = None, team_yaml: str | None = None):
    """Build real service instances over a temporary workspace."""
    customers = tmp_path / "customers"
    customers.mkdir(parents=True, exist_ok=True)
    team_file = _write_team(tmp_path, team_yaml)
    workspace = tmp_path / "workspace"
    repo_root = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    repo_root.mkdir(exist_ok=True)

    output_svc = OutputService(
        customers_dir=customers,
        workspace=workspace,
        repo_root=repo_root,
        se_config=lambda: {},
        safe_name=_safe_name,
        slug=_slug,
    )
    job_svc = SimpleNamespace(overview_jobs=lambda: (jobs or {}))
    account_svc = AccountService(
        customers_dir=customers,
        webapp_dir=tmp_path,
        output_service=output_svc,
        job_service=job_svc,
        safe_name=_safe_name,
        titlecase=_titlecase,
        slug=_slug,
        team_file=team_file,
        se_config_file=tmp_path / ".se-config.yaml",
    )
    overview_svc = OverviewService(account_svc, output_svc, job_svc)
    return overview_svc, output_svc, account_svc, job_svc, customers


def _overview(tmp_path: Path, jobs: dict | None = None, team_yaml: str | None = None) -> dict:
    overview_svc, *_ = _make_services(tmp_path, jobs, team_yaml)
    return overview_svc.build_overview(jobs)


# ---------------------------------------------------------------------------
# Summary counts
# ---------------------------------------------------------------------------

def test_empty_overview(tmp_path) -> None:
    """An empty workspace returns zeroed counts and empty lists."""
    data = _overview(tmp_path)
    assert data["summary"]["members"] == 1  # fallback "Me" member
    assert data["summary"]["active_accounts"] == 0
    assert data["summary"]["archived_accounts"] == 0
    assert data["summary"]["opportunities"] == 0
    assert data["summary"]["outputs"] == 0
    assert data["summary"]["running_jobs"] == 0
    assert data["summary"]["recent_failures"] == 0
    assert data["summary"]["needs_attention"] == 0
    assert data["attention"] == []
    assert data["recent"] == []
    assert data["empty"]["accounts"] is True


def test_member_count(tmp_path) -> None:
    """summary.members reflects the configured team size."""
    data = _overview(
        tmp_path,
        team_yaml="members:\n  - id: a\n    name: Ada\n  - id: b\n    name: Bob\n",
    )
    assert data["summary"]["members"] == 2
    assert len(data["members"]) == 2


def test_account_count_and_active_archived(tmp_path) -> None:
    """Active and archived account totals are split correctly."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    _write_output(customers, "Acme", None, "next-move", "a.md")
    _write_output(customers, "Beta", None, "next-move", "b.md")
    (customers / "Beta" / ".archived").write_text("2026-01-01T00:00:00Z")

    data = overview_svc.build_overview()
    assert data["summary"]["active_accounts"] == 1
    assert data["summary"]["archived_accounts"] == 1


def test_opportunity_count(tmp_path) -> None:
    """summary.opportunities sums per-account opportunity counts."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    _write_output(customers, "Acme", "general", "next-move", "a.md")
    _write_output(customers, "Acme", "expansion", "next-move", "b.md")

    data = overview_svc.build_overview()
    assert data["summary"]["opportunities"] == 2


def test_output_count(tmp_path) -> None:
    """summary.outputs counts every saved output."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    _write_output(customers, "Acme", None, "next-move", "a.md")
    _write_output(customers, "Acme", None, "next-move", "b.md")
    _write_output(customers, "Beta", "general", "next-move", "c.md")

    data = overview_svc.build_overview()
    assert data["summary"]["outputs"] == 3


def test_counts_across_multiple_members_and_accounts(tmp_path) -> None:
    """Per-member rollups aggregate correctly across assigned accounts."""
    overview_svc, output_svc, *_ = _make_services(
        tmp_path,
        team_yaml="members:\n  - id: ada\n    name: Ada\n  - id: bob\n    name: Bob\n",
    )
    customers = output_svc.customers_dir
    _write_output(customers, "Ada-Corp", None, "next-move", "a.md")
    (customers / "Ada-Corp" / ".owner").write_text("ada")
    _write_output(customers, "Bob-Corp", None, "next-move", "b.md", sidecar={"valid": True, "validation_status": "valid"})
    (customers / "Bob-Corp" / ".owner").write_text("bob")

    data = overview_svc.build_overview()
    ada = next(m for m in data["members"] if m["id"] == "ada")
    bob = next(m for m in data["members"] if m["id"] == "bob")
    assert ada["account_count"] == 1
    assert ada["output_count"] == 1
    assert bob["account_count"] == 1
    assert bob["output_count"] == 1


# ---------------------------------------------------------------------------
# Output review / validation attention
# ---------------------------------------------------------------------------

def test_overview_valid_output_awaits_review(tmp_path) -> None:
    """A valid sidecar with no feedback sidecar is awaiting human review."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
    )
    data = overview_svc.build_overview()

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


def test_overview_approved_output_not_attention(tmp_path) -> None:
    """An approved output with valid metadata needs no attention."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
        feedback_lines=[json.dumps({"action": "approve", "comment": "", "author": "Ada"})],
    )
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 0
    assert data["summary"]["outputs"] == 1
    assert data["attention"] == []


def test_overview_commented_output_awaits_review(tmp_path) -> None:
    """A comment keeps the output in the review queue."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
        feedback_lines=[json.dumps({"action": "comment", "comment": "Check numbers."})],
    )
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["review_status"] == "commented"
    assert item["status"] == "commented"


def test_overview_corrected_output_awaits_review(tmp_path) -> None:
    """A correction request keeps the output in the review queue."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "valid"},
        feedback_lines=[json.dumps({"action": "correct", "comment": "Fix deployment."})],
    )
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["review_status"] == "corrected"
    assert item["status"] == "corrected"


def test_overview_invalid_output_needs_attention(tmp_path) -> None:
    """An invalid output is a validation issue, not a review label."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": False, "validation_status": "invalid", "validation_errors": ["Missing source coverage"]},
    )
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "attention"
    assert item["validation_status"] == "invalid"
    assert item["review_status"] == "awaiting review"
    assert item["level"] == "error"


def test_overview_stale_output_needs_attention(tmp_path) -> None:
    """A stale reference output is flagged as attention (validation)."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
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
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "attention"
    assert item["validation_status"] == "stale"
    assert item["review_status"] == "awaiting review"


def test_overview_unvalidated_output_awaits_review(tmp_path) -> None:
    """A successfully parsed output from a skill with no schema is unvalidated
    but does not need validation attention; it still awaits human review."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(
        output_svc.customers_dir,
        "Acme",
        None,
        "next-move",
        "next-move-2026-07-14.md",
        sidecar={"valid": True, "validation_status": "unvalidated"},
    )
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 1
    assert len(data["attention"]) == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["status"] == "awaiting review"
    assert item["validation_status"] == "unvalidated"
    assert item["review_status"] == "awaiting review"


def test_overview_legacy_output_without_modern_metadata(tmp_path) -> None:
    """An output with no `.md.json` sidecar is validation `unknown` and awaiting review."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(output_svc.customers_dir, "Acme", None, "next-move", "legacy.md")
    data = overview_svc.build_overview()

    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["validation_status"] == "unknown"
    assert item["review_status"] == "awaiting review"
    assert item["status"] == "awaiting review"


def test_overview_malformed_sidecar_and_feedback_are_skipped(tmp_path) -> None:
    """Malformed `.md.json` and `.feedback.jsonl` do not crash the overview."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    md = _write_output(output_svc.customers_dir, "Acme", None, "next-move", "broken.md")
    md.with_suffix(".md.json").write_text("not json", encoding="utf-8")
    md.with_suffix(".feedback.jsonl").write_text("not json\n", encoding="utf-8")

    data = overview_svc.build_overview()
    assert data["summary"]["needs_attention"] == 1
    item = data["attention"][0]
    assert item["type"] == "review"
    assert item["validation_status"] == "unknown"
    assert item["review_status"] == "awaiting review"


def test_overview_skips_missing_output_file(tmp_path) -> None:
    """If an output file disappears after it is globbed, stat failure is skipped."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    d = customers / "Acme" / "outputs" / "next-move"
    d.mkdir(parents=True, exist_ok=True)
    md = d / "next-move-2026-07-14.md"
    md.write_text("# Output\n", encoding="utf-8")
    md.unlink()
    meta = {"output_count": 0, "last_updated_ts": 0.0, "last_output": None, "needs_attention": 0, "opp_slugs": set()}
    recent: list[dict] = []
    attention: list[dict] = []
    output_svc.collect_output("Acme", None, "next-move", md, meta, recent, attention, customers_dir=customers)

    assert meta["output_count"] == 0
    assert meta["needs_attention"] == 0
    assert recent == []
    assert attention == []


# ---------------------------------------------------------------------------
# Job activity
# ---------------------------------------------------------------------------

def test_running_completed_failed_jobs_classified(tmp_path) -> None:
    """Jobs are classified into running, done, and error buckets."""
    now = time.time()
    jobs = {
        "r1": {"status": "running", "skill": "prep-call", "account": "Acme", "started_at": now - 60},
        "d1": {"status": "done", "ok": True, "skill": "next-move", "account": "Acme", "finished_at": now - 120},
        "e1": {"status": "error", "ok": False, "skill": "poc-plan", "account": "Acme", "finished_at": now - 60},
    }
    data = _overview(tmp_path, jobs=jobs)
    assert data["summary"]["running_jobs"] == 1
    assert data["summary"]["recent_failures"] == 1
    recent_types = {r["type"] for r in data["recent"]}
    assert {"job_started", "job_done", "job_error"} <= recent_types


def test_long_running_job_attention(tmp_path) -> None:
    """A running job older than the threshold becomes a long-running warning."""
    now = time.time()
    jobs = {
        "r1": {"status": "running", "skill": "prep-call", "account": "Acme", "started_at": now - 600},
    }
    data = _overview(tmp_path, jobs=jobs)
    assert len(data["attention"]) == 1
    assert data["attention"][0]["type"] == "long-running"
    assert data["attention"][0]["level"] == "warn"
    assert data["attention"][0]["duration_min"] >= 10


def test_job_recovered_from_server_restart(tmp_path) -> None:
    """A failed job with 'Server restarted' in stderr is marked as recovered."""
    now = time.time()
    jobs = {
        "e1": {"status": "error", "ok": False, "skill": "prep-call", "account": "Acme", "finished_at": now - 60, "stderr": "Server restarted; job was interrupted"},
    }
    data = _overview(tmp_path, jobs=jobs)
    assert data["recent"][0]["type"] == "job_recovered"


def test_jobs_without_account_are_classified_not_attributed(tmp_path) -> None:
    """Jobs missing an account still appear in summary/recent but not member rollups."""
    now = time.time()
    jobs = {
        "r1": {"status": "running", "skill": "prep-call", "started_at": now - 60},
    }
    data = _overview(tmp_path, jobs=jobs)
    assert data["summary"]["running_jobs"] == 1
    assert data["recent"][0]["account"] == "unknown"


def test_legacy_jobs_without_timestamps_degrade(tmp_path) -> None:
    """Legacy job records without `started_at` or `finished_at` do not break rendering."""
    jobs = {
        "j1": {"status": "done", "ok": True, "skill": "next-move", "account": "Acme"},
        "j2": {"status": "error", "ok": False, "skill": "next-move", "account": "Acme"},
    }
    data = _overview(tmp_path, jobs=jobs)

    assert data["summary"]["recent_failures"] == 1
    assert len(data["recent"]) == 2
    recent_types = {r["type"] for r in data["recent"]}
    assert "job_done" in recent_types
    assert "job_error" in recent_types


def test_job_data_read_only_via_service(tmp_path) -> None:
    """OverviewService only reads job state; the passed jobs dict is unchanged."""
    jobs = {"j1": {"status": "done", "ok": True, "skill": "next-move", "account": "Acme", "finished_at": 1.0}}
    overview_svc, *_ = _make_services(tmp_path, jobs=jobs)
    _ = overview_svc.build_overview(jobs)
    assert "job_id" not in jobs["j1"]


# ---------------------------------------------------------------------------
# Recent activity
# ---------------------------------------------------------------------------

def test_recent_output_and_job_activity_mixed_and_sorted(tmp_path) -> None:
    """Recent activity interleaves outputs and job events sorted by `when`."""
    now = time.time()
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    md = _write_output(output_svc.customers_dir, "Acme", None, "next-move", "a.md")
    # Backdate the output so the newer job appears first.
    os.utime(md, (now - 300, now - 300))
    jobs = {
        "d1": {"status": "done", "ok": True, "skill": "next-move", "account": "Acme", "finished_at": now - 60},
    }
    data = overview_svc.build_overview(jobs)

    assert len(data["recent"]) == 2
    assert data["recent"][0]["type"] == "job_done"
    assert data["recent"][1]["type"] == "output"


def test_recent_activity_limits_and_tie_breaking(tmp_path) -> None:
    """Recent activity is limited to `_MAX_RECENT` and stable for equal timestamps."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    # Same mtime, filenames provide a stable sort tie-break.
    base = time.time()
    _write_output(customers, "Acme", None, "next-move", "z.md")
    _write_output(customers, "Acme", None, "next-move", "a.md")
    for md in (customers / "Acme" / "outputs" / "next-move").glob("*.md"):
        os.utime(md, (base, base))

    data = overview_svc.build_overview()
    assert len(data["recent"]) == 2
    filenames = [r["filename"] for r in data["recent"]]
    # Stable reverse sort preserves original sorted order for equal keys.
    assert filenames == ["a.md", "z.md"]


# ---------------------------------------------------------------------------
# Attention rules
# ---------------------------------------------------------------------------

def test_attention_priority_and_max_limit(tmp_path) -> None:
    """Attention is sorted by level and recency, capped at `_MAX_ATTENTION`."""
    now = time.time()
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    # Write enough outputs to exceed the limit.
    for i in range(15):
        _write_output(customers, "Acme", None, "next-move", f"out-{i:02d}.md")
    jobs = {
        "e1": {"status": "error", "ok": False, "skill": "poc-plan", "account": "Acme", "finished_at": now - 30},
    }
    data = overview_svc.build_overview(jobs)
    assert len(data["attention"]) == 10
    assert data["attention"][0]["level"] == "error"


def test_failed_job_attention_within_window_and_ignored_after(tmp_path) -> None:
    """Failed jobs appear in attention only while inside the failure window."""
    now = time.time()
    jobs_old = {
        "e1": {"status": "error", "ok": False, "skill": "poc-plan", "account": "Acme", "finished_at": now - 30 * 3600},
    }
    data = _overview(tmp_path, jobs=jobs_old)
    assert data["attention"] == []
    assert data["summary"]["recent_failures"] == 0

    jobs_recent = {
        "e1": {"status": "error", "ok": False, "skill": "poc-plan", "account": "Acme", "finished_at": now - 60, "stderr": "boom"},
    }
    data = _overview(tmp_path, jobs=jobs_recent)
    assert len(data["attention"]) == 1
    assert data["attention"][0]["type"] == "failure"
    assert data["attention"][0]["error"] == "boom"


def test_stale_account_attention(tmp_path) -> None:
    """An active account with old output activity is flagged stale."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    md = _write_output(customers, "Acme", None, "next-move", "old.md")
    old = time.time() - 10 * 24 * 3600
    os.utime(md, (old, old))

    data = overview_svc.build_overview()
    assert any(a["type"] == "stale" and a["account"] == "Acme" for a in data["attention"])


def test_attention_links_are_valid_router_hrefs(tmp_path) -> None:
    """Attention and recent items include navigation hrefs."""
    now = time.time()
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(output_svc.customers_dir, "Acme", "general", "next-move", "a.md")
    jobs = {
        "d1": {"status": "done", "ok": True, "skill": "next-move", "account": "Acme", "opp_slug": "general", "opportunity": "General", "finished_at": now - 60},
    }
    data = overview_svc.build_overview(jobs)
    assert any(r["href"].startswith("#/output/") for r in data["recent"] if r["type"] == "output")
    assert any(r["href"].startswith("#/opp/") for r in data["recent"] if r["type"].startswith("job"))


# ---------------------------------------------------------------------------
# Error tolerance
# ---------------------------------------------------------------------------

def test_missing_and_empty_team_configuration_falls_back(tmp_path) -> None:
    """Missing or empty team config uses the 'Me' fallback member."""
    for content in ("", "members:\n"):
        data = _overview(tmp_path, team_yaml=content)
        assert data["summary"]["members"] == 1
        assert data["members"][0]["id"] == "me"


def test_malformed_job_record_does_not_crash_overview(tmp_path) -> None:
    """A job with no status is ignored without breaking aggregation."""
    jobs = {"j1": {"skill": "next-move", "account": "Acme"}}
    data = _overview(tmp_path, jobs=jobs)
    assert data["summary"]["running_jobs"] == 0
    assert data["recent"] == []


def test_malformed_output_record_does_not_crash_overview(tmp_path) -> None:
    """A malformed output sidecar is skipped and the rest of the overview loads."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    _write_output(output_svc.customers_dir, "Acme", None, "next-move", "good.md", sidecar={"valid": True, "validation_status": "valid"})
    md = _write_output(output_svc.customers_dir, "Acme", None, "next-move", "bad.md")
    md.with_suffix(".md.json").write_text("not json", encoding="utf-8")

    data = overview_svc.build_overview()
    assert data["summary"]["outputs"] == 2
    assert data["summary"]["needs_attention"] == 2  # both good.md and bad.md await review


def test_one_malformed_account_directory_does_not_break_overview(tmp_path) -> None:
    """A failure reading one account directory does not prevent reading another."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    _write_output(customers, "Beta", None, "next-move", "b.md", sidecar={"valid": True, "validation_status": "valid"})
    (customers / "BadActor").write_text("not a directory")
    data = overview_svc.build_overview()

    assert data["summary"]["outputs"] == 1
    assert data["summary"]["needs_attention"] == 1
    assert len(data["attention"]) == 1
    assert data["attention"][0]["account"] == "Beta"


def test_no_attention_and_no_recent_activity_empty_states(tmp_path) -> None:
    """Empty attention and recent lists produce correct empty flags."""
    data = _overview(tmp_path)
    assert data["empty"]["attention"] is True
    assert data["empty"]["recent"] is True


# ---------------------------------------------------------------------------
# Latest selection and member attribution
# ---------------------------------------------------------------------------

def test_latest_output_and_activity_selection(tmp_path) -> None:
    """Per-member `last_output` and `last_activity_ts` track the newest output."""
    overview_svc, output_svc, *_ = _make_services(tmp_path)
    customers = output_svc.customers_dir
    old_md = _write_output(customers, "Acme", None, "next-move", "old.md")
    new_md = _write_output(customers, "Acme", None, "next-move", "new.md")
    now = time.time()
    os.utime(old_md, (now - 1000, now - 1000))
    os.utime(new_md, (now - 100, now - 100))

    data = overview_svc.build_overview()
    assert data["members"][0]["last_output"]["filename"] == "new.md"
    assert data["members"][0]["last_activity_ts"] == pytest.approx(now - 100, abs=1)


def test_member_attribution_with_owned_and_unowned_accounts(tmp_path) -> None:
    """Members see accounts they own plus unowned accounts."""
    overview_svc, output_svc, *_ = _make_services(
        tmp_path,
        team_yaml="members:\n  - id: ada\n    name: Ada\n  - id: bob\n    name: Bob\n",
    )
    customers = output_svc.customers_dir
    _write_output(customers, "Owned", None, "next-move", "a.md")
    (customers / "Owned" / ".owner").write_text("ada")
    _write_output(customers, "Shared", None, "next-move", "b.md")

    data = overview_svc.build_overview()
    ada = next(m for m in data["members"] if m["id"] == "ada")
    bob = next(m for m in data["members"] if m["id"] == "bob")
    assert ada["account_count"] == 2
    assert bob["account_count"] == 1  # only the unowned Shared account
