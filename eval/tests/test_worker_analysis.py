"""Parity and behavior tests for the ported `worker-analysis` skill and toolkit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import orchestrator
from connector_classifier import ConnectorClassifier
from job_overlap_analyzer import analyze_job_overlaps
from queuing_calculator import calculate_drain_time
from services.skill_runtime_service import SKILL_PERMISSIONS, SkillRuntimeService
from worker_calculator import WorkerCalculator


def _make_jobs_peak_window() -> list[dict[str, Any]]:
    """A fixture-style list of jobs with a clear 14:00 UTC peak."""
    base = datetime(2026, 7, 10, 13, 55, tzinfo=timezone.utc)
    jobs: list[dict[str, Any]] = []
    # 6 API jobs that overlap at 14:00
    for i in range(6):
        jobs.append(
            {
                "connection_id": f"api-conn-{i}",
                "connection_name": f"API Source {i}",
                "source_type": "salesforce",
                "start_time": (base + timedelta(minutes=i)).isoformat(),
                "end_time": (base + timedelta(minutes=15 + i)).isoformat(),
            }
        )
    # 4 DB jobs that overlap at 14:00
    for i in range(4):
        jobs.append(
            {
                "connection_id": f"db-conn-{i}",
                "connection_name": f"DB Source {i}",
                "source_type": "postgres",
                "start_time": (base + timedelta(minutes=i)).isoformat(),
                "end_time": (base + timedelta(minutes=20 + i)).isoformat(),
            }
        )
    # A long-running initial load that started at 13:00 and runs until 15:30
    jobs.append(
        {
            "connection_id": "db-initial-load",
            "connection_name": "Postgres initial load",
            "source_type": "postgres",
            "start_time": (base - timedelta(hours=1)).isoformat(),
            "end_time": (base + timedelta(hours=1, minutes=35)).isoformat(),
        }
    )
    # Staggered hourly recurring jobs that do NOT overlap the 14:00 peak
    for i in range(3):
        jobs.append(
            {
                "connection_id": f"hourly-{i}",
                "connection_name": f"Hourly connector {i}",
                "source_type": "hubspot",
                "start_time": (base + timedelta(hours=1 + i, minutes=10)).isoformat(),
                "end_time": (base + timedelta(hours=1 + i, minutes=20)).isoformat(),
            }
        )
    return jobs


def _make_connections() -> dict[str, dict[str, Any]]:
    return {
        "api-conn-0": {"type": "API"},
        "db-conn-0": {"type": "DATABASE"},
        "db-initial-load": {"type": "DATABASE"},
    }


def test_worker_calculator_questionnaire_sizing_mixed_saas_and_db() -> None:
    calc = WorkerCalculator()
    result = calc.calculate_from_estimate(
        total_connections=50,
        api_percent=60,
        db_percent=40,
        sub_hourly_percent=10,
        hourly_percent=60,
        daily_percent=30,
        sync_duration_minutes=30,
    )
    assert result["workers_required"] >= 1
    assert result["expected_concurrency"]["total_concurrent"] > 0
    assert result["connection_breakdown"]["api_connections"] == 30
    assert result["connection_breakdown"]["db_connections"] == 20
    assert result["input_parameters"]["hourly_percent"] == 60


def test_worker_calculator_cadence_preservation_hourly_is_primary() -> None:
    calc = WorkerCalculator()
    hourly = calc.calculate_from_estimate(
        total_connections=30,
        api_percent=70,
        db_percent=30,
        sub_hourly_percent=0,
        hourly_percent=80,
        daily_percent=20,
        sync_duration_minutes=30,
    )
    daily = calc.calculate_from_estimate(
        total_connections=30,
        api_percent=70,
        db_percent=30,
        sub_hourly_percent=0,
        hourly_percent=20,
        daily_percent=80,
        sync_duration_minutes=30,
    )
    assert hourly["workers_required"] > daily["workers_required"]


def test_worker_calculator_incomplete_evidence_uses_defaults() -> None:
    calc = WorkerCalculator()
    result = calc.calculate_from_estimate(
        total_connections=10,
        api_percent=50,
        db_percent=50,
        sub_hourly_percent=0,
        hourly_percent=100,
        daily_percent=0,
    )
    assert result["input_parameters"]["sync_duration_minutes"] == 30
    assert result["calculation_details"]["model"] == "statistical"


def test_job_overlap_peak_window_and_worker_formula() -> None:
    jobs = _make_jobs_peak_window()
    connections = _make_connections()
    result = analyze_job_overlaps(jobs, connections, analysis_period_days=10)

    assert result.peak_hour == 14
    # 6 API jobs + 1 long-running DB = 7 DB at 14:00? Actually 4 DB + 1 initial load = 5 DB
    assert result.peak_concurrent_api == 6
    assert result.peak_concurrent_db == 5
    assert result.p99_workers > 0
    assert result.total_jobs_analyzed == len(jobs)


def test_job_overlap_incomplete_history_uses_default_duration() -> None:
    jobs = [
        {
            "connection_id": "api-1",
            "connection_name": "API 1",
            "source_type": "stripe",
            "start_time": datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc).isoformat(),
        }
    ]
    result = analyze_job_overlaps(jobs)
    assert result.total_jobs_analyzed == 1
    assert result.p99_workers >= 0


@pytest.mark.parametrize(
    "name,expected",
    [
        pytest.param("postgres", "DATABASE", id="database_connector"),
        pytest.param("stripe", "API", id="api_connector"),
        pytest.param("s3", "DATABASE", id="file_connector"),
        pytest.param("unknown-vendor", "UNKNOWN", id="unknown_connector"),
    ],
)
def test_connector_classifier(name: str, expected: str) -> None:
    classifier = ConnectorClassifier()
    assert classifier.classify(name) == expected


@pytest.mark.parametrize(
    "syncs,slots,duration,finite",
    [
        pytest.param(10, 3, 5, True, id="within_window"),
        pytest.param(10, 0, 5, False, id="zero_slots_infinite"),
    ],
)
def test_queuing_calculator(syncs: int, slots: int, duration: int, finite: bool) -> None:
    minutes = calculate_drain_time(syncs, slots, duration)
    if finite:
        assert 0 < minutes <= 20
    else:
        assert minutes == float("inf")


def test_prerequisites_questionnaire_ready_without_config(tmp_path: Path, monkeypatch) -> None:
    for var in ("AIRBYTE_CLOUD_CLIENT_ID", "AIRBYTE_CLOUD_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    customers = tmp_path / "customers"
    customers.mkdir()
    plan = orchestrator.check_prerequisites("worker-analysis", "Acme", None, customers)
    assert plan.ready is True
    assert plan.modes is not None
    assert plan.modes["questionnaire"]["ready"] is True
    assert plan.modes["workspace"]["ready"] is False
    assert plan.modes["metabase"]["ready"] is False


def test_prerequisites_workspace_ready_with_env_credentials(tmp_path: Path, monkeypatch) -> None:
    customers = tmp_path / "customers"
    customers.mkdir()
    monkeypatch.setenv("AIRBYTE_CLOUD_CLIENT_ID", "cid")
    monkeypatch.setenv("AIRBYTE_CLOUD_CLIENT_SECRET", "csec")
    plan = orchestrator.check_prerequisites("worker-analysis", "Acme", None, customers)
    assert plan.modes["workspace"]["ready"] is True


def test_prerequisites_metabase_ready_with_config(tmp_path: Path) -> None:
    customers = tmp_path / "customers"
    customers.mkdir()
    config = tmp_path / ".se-config.yaml"
    config.write_text(
        "worker_analysis:\n  bigquery_project: airbyte-data-prod\n  bigquery_dataset: airbyte_warehouse\n"
    )
    plan = orchestrator.check_prerequisites("worker-analysis", "Acme", None, customers)
    assert plan.modes["metabase"]["ready"] is True


def test_run_worker_analysis_estimate_subcommand(repo_root: Path) -> None:
    script = repo_root / "skills" / "worker-analysis" / "scripts" / "run_worker_analysis.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "estimate",
            "--description",
            "Parity test",
            "--size",
            "small",
            "--mix",
            "mixed",
            "--frequency",
            "frequent",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    # The script interleaves human-readable printouts with the final JSON object.
    # Find the first line that starts a JSON object and parse from there.
    lines = result.stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("{"))
    data = json.loads("\n".join(lines[start:]))
    assert data["estimates"]["mid"]["workers"] > 0


def _parse_script_json(stdout: str) -> Any:
    """Find the final JSON object emitted by a script that also prints human-readable text."""
    lines = stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("{"))
    return json.loads("\n".join(lines[start:]))


def test_run_worker_analysis_oss_export(repo_root: Path, tmp_path: Path) -> None:
    export = tmp_path / "oss-export.json"
    export.write_text(
        json.dumps(
            {
                "workspace_id": "ws-test",
                "connections": [
                    {
                        "connection_id": "c1",
                        "name": "Salesforce",
                        "source_type": "salesforce",
                        "schedule": "0 * * * *",
                    },
                    {
                        "connection_id": "c2",
                        "name": "Postgres",
                        "source_type": "postgres",
                        "schedule": "0 1 * * *",
                    },
                ],
            }
        )
    )
    script = repo_root / "skills" / "worker-analysis" / "scripts" / "run_worker_analysis.py"
    result = subprocess.run(
        [sys.executable, str(script), "oss", str(export), "--output-dir", str(tmp_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    data = _parse_script_json(result.stdout)
    assert data["total_connections"] == 2
    assert data["estimate"]["workers_required"] > 0


def test_skill_discovery_lists_worker_analysis(repo_root: Path) -> None:
    from unittest.mock import MagicMock

    service = SkillRuntimeService(
        customers_dir=repo_root / "eval" / "fixtures",
        workspace=repo_root,
        output_service=MagicMock(),
        job_service=MagicMock(),
        se_config=lambda: {},
        se_config_clear=lambda: None,
        safe_name=lambda n: n,
        skills_dir=repo_root / "skills",
        skills_dirs=[repo_root / "skills"],
    )
    assert "worker-analysis" in service.skill_ids


def test_permission_profile_includes_shell() -> None:
    profile = SKILL_PERMISSIONS["worker-analysis"]
    assert profile.write is True
    assert profile.shell is True
