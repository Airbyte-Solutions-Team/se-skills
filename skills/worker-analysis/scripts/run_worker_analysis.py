#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy>=1.24",
#   "python-dateutil>=2.8",
#   "pyyaml>=6.0",
#   "reportlab>=4.0",
#   "requests>=2.31",
# ]
# ///
"""
CLI runner for the worker-analysis toolkit.

Used by the `worker-analysis` SE skill to perform deterministic calculations
(questionnaire, OSS export, workspace job overlap) and generate PDF reports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The toolkit modules live alongside this script under ../worker_analysis/src.
# Allow both `from src.X` and `from X` import styles used by the original code.
SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_ANALYSIS_DIR = SCRIPT_DIR.parent / "worker_analysis"
SRC_DIR = WORKER_ANALYSIS_DIR / "src"
sys.path.insert(0, str(WORKER_ANALYSIS_DIR))
sys.path.insert(0, str(SRC_DIR))


def _questionnaire(args: argparse.Namespace) -> int:
    from mode2_prospect_questionnaire import estimate_from_questionnaire

    result = estimate_from_questionnaire(
        total_connections=args.connections,
        db_file_percent=args.db_percent,
        api_percent=args.api_percent,
        sub_hourly_percent=args.sub_hourly_percent,
        hourly_percent=args.hourly_percent,
        daily_percent=args.daily_percent,
        sync_duration_minutes=args.avg_duration,
        maintenance_window_hours=args.maintenance_window,
    )
    print(json.dumps({"mode": "2A_questionnaire", "result": result}, indent=2, default=str))
    return 0


def _custom_estimate(args: argparse.Namespace) -> int:
    from mode2_custom_estimate import quick_estimate

    result = quick_estimate(
        description=args.description,
        connection_count_range=args.size,
        connector_mix=args.mix,
        sync_frequency=args.frequency,
    )
    print(json.dumps(result, indent=2))
    return 0


def _oss_export(args: argparse.Namespace) -> int:
    from connector_classifier import ConnectorClassifier
    from worker_calculator import WorkerCalculator

    path = Path(args.path)
    data = json.loads(path.read_text())

    connections = data.get("connections", [])
    job_history = data.get("job_history", [])

    if job_history:
        from job_overlap_analyzer import analyze_job_overlaps

        conns = {
            c["connection_id"]: {"type": ConnectorClassifier().classify(c.get("source_type", ""))}
            for c in connections
        }
        result = analyze_job_overlaps(job_history, conns)
        print(json.dumps({
            "mode": "2B_oss_workspace_job_overlap",
            "total_connections": len(connections),
            "p99_workers": result.p99_workers,
            "peak_hour": result.peak_hour,
            "peak_concurrent_api": result.peak_concurrent_api,
            "peak_concurrent_db": result.peak_concurrent_db,
        }, indent=2, default=str))
        return 0

    # Fallback: estimate from connection counts and declared schedules.
    schedule_breakdown = {"sub_hourly": 0, "hourly": 0, "daily": 0}
    api_count = 0
    db_count = 0
    classifier = ConnectorClassifier()
    for conn in connections:
        schedule = str(conn.get("schedule", conn.get("schedule_type", ""))).lower()
        if "*/" in schedule or "/5" in schedule or "/10" in schedule or "/15" in schedule:
            schedule_breakdown["sub_hourly"] += 1
        elif "0 * * * *" in schedule or schedule.count("*") <= 4:
            schedule_breakdown["hourly"] += 1
        else:
            schedule_breakdown["daily"] += 1

        if classifier.classify(conn.get("source_type", conn.get("name", ""))) == "API":
            api_count += 1
        else:
            db_count += 1

    total = len(connections) or 1
    api_percent = (api_count / total) * 100
    db_percent = (db_count / total) * 100
    sub_hourly = (schedule_breakdown["sub_hourly"] / total) * 100
    hourly = (schedule_breakdown["hourly"] / total) * 100
    daily = (schedule_breakdown["daily"] / total) * 100

    calc = WorkerCalculator()
    estimate = calc.calculate_from_estimate(
        total_connections=total,
        api_percent=api_percent,
        db_percent=db_percent,
        sub_hourly_percent=sub_hourly,
        hourly_percent=hourly,
        daily_percent=daily,
    )
    print(json.dumps({
        "mode": "2B_oss_workspace_estimate",
        "total_connections": total,
        "schedule_breakdown": schedule_breakdown,
        "estimate": estimate,
    }, indent=2, default=str))
    return 0


def _workspace(args: argparse.Namespace) -> int:
    from analyze_org_workspaces import analyze_workspaces

    if not args.workspace_id and not args.org_id:
        print("error: --workspace-id or --org-id required", file=sys.stderr)
        return 2

    workspace_ids = []
    if args.workspace_id:
        workspace_ids.append(args.workspace_id)
    if args.org_id:
        # The library does not support org-wide workspace enumeration; caller must supply IDs.
        print("error: --org-id is not supported by the CLI; use --workspace-id", file=sys.stderr)
        return 2

    try:
        result = analyze_workspaces(
            workspace_ids=workspace_ids,
            client_id=args.client_id,
            client_secret=args.client_secret,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"error: workspace analysis failed: {exc}", file=sys.stderr)
        return 1


def _report(args: argparse.Namespace) -> int:
    from prospect_estimation_report import generate_prospect_report

    result = generate_prospect_report(
        customer_name=args.customer,
        prepared_by=args.prepared_by,
        prepared_by_title=args.prepared_by_title,
        total_databases=args.connections,
        critical_syncs=max(1, args.connections // 2),
        completion_window_minutes=args.window_minutes,
        avg_sync_duration_minutes=args.avg_duration,
        p90_sync_duration_minutes=args.avg_duration * 1.25,
        connector_type="database" if args.db_percent >= 50 else "api",
        output_dir=args.output_dir,
        growth_notes=args.growth_notes,
    )
    print(json.dumps({"pdf_path": result.get("file_path"), "metrics": result}, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worker analysis toolkit runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Questionnaire-based estimate from CLI args
    q = subparsers.add_parser("questionnaire", help="Estimate from prospect questionnaire answers")
    q.add_argument("--connections", type=int, default=50)
    q.add_argument("--api-percent", type=float, default=50)
    q.add_argument("--db-percent", type=float, default=50)
    q.add_argument("--sub-hourly-percent", type=float, default=20)
    q.add_argument("--hourly-percent", type=float, default=30)
    q.add_argument("--daily-percent", type=float, default=50)
    q.add_argument("--avg-duration", type=float, default=8)
    q.add_argument("--maintenance-window", type=float, default=4)

    # Quick estimate from CLI args
    est = subparsers.add_parser("estimate", help="Quick ballpark estimate")
    est.add_argument("--description", default="Prospect worker estimate")
    est.add_argument("--size", default="small", choices=["small", "medium", "large", "xlarge"])
    est.add_argument("--mix", default="mixed", choices=["mostly_api", "mostly_db", "mixed"])
    est.add_argument("--frequency", default="frequent", choices=["realtime", "frequent", "daily", "mixed"])

    # OSS export
    oss = subparsers.add_parser("oss", help="Analyze an OSS/Cloud JSON export")
    oss.add_argument("path")
    oss.add_argument("--output-dir", default=os.getcwd())

    # Workspace/API mode
    ws = subparsers.add_parser("workspace", help="Analyze an Airbyte Cloud workspace")
    ws.add_argument("--workspace-id")
    ws.add_argument("--org-id")
    ws.add_argument("--client-id", default=os.environ.get("AIRBYTE_CLOUD_CLIENT_ID"))
    ws.add_argument("--client-secret", default=os.environ.get("AIRBYTE_CLOUD_CLIENT_SECRET"))
    ws.add_argument("--days", type=int, default=30)
    ws.add_argument("--output-dir", default=os.getcwd())

    # PDF report from estimate
    rep = subparsers.add_parser("report", help="Generate a prospect estimation PDF")
    rep.add_argument("--customer", default="<Customer>")
    rep.add_argument("--prepared-by", default="<SE Name>")
    rep.add_argument("--prepared-by-title", default="Solutions Engineering, Airbyte")
    rep.add_argument("--connections", type=int, default=50)
    rep.add_argument("--api-percent", type=float, default=50)
    rep.add_argument("--db-percent", type=float, default=50)
    rep.add_argument("--sub-hourly-percent", type=float, default=20)
    rep.add_argument("--hourly-percent", type=float, default=30)
    rep.add_argument("--daily-percent", type=float, default=50)
    rep.add_argument("--window-minutes", type=float, default=30)
    rep.add_argument("--avg-duration", type=float, default=8)
    rep.add_argument("--growth-notes", default="")
    rep.add_argument("--output-dir", default=os.getcwd())

    args = parser.parse_args(argv)
    handlers = {
        "questionnaire": _questionnaire,
        "estimate": _custom_estimate,
        "oss": _oss_export,
        "workspace": _workspace,
        "report": _report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
