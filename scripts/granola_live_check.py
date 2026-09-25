#!/usr/bin/env -S uv run --script
"""Local, user-run check of the Granola retrieval path through the real app code.

Runs the same `ClaudeCodeMcpRelay` → `GranolaRetrievalService` → adapter → ledger
chain the web app uses, against a throwaway ledger directory, for ONE synthetic
meeting whose title contains `--marker` (default `SE-SKILLS-CAPCHECK`):

    connection check → metadata list for one day → retrieve the marked meeting
    → ledger outcome → retrieve again (expect `already_known`)

Output is a single JSON object containing step statuses, safe error codes,
counts, outcome codes, and a SHA-256 prefix of the workspace id. It never
prints titles, summaries, transcripts, tokens, or raw tool output. The
throwaway ledger is deleted on exit. Nothing is written to your customers
directory or to the app's own ledger.

    uv run scripts/granola_live_check.py --date 2026-09-24

Prerequisites: `claude` on PATH, the `granola` MCP server configured at user
scope (`claude mcp list` shows it as connected), and a synthetic meeting on
that date whose title contains the marker.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "webapp"))

from integrations.granola import ManualGranolaImportAdapter
from integrations.granola_mcp_relay import ClaudeCodeMcpRelay, GranolaRelayError
from services.evidence_ledger_service import EvidenceLedgerService
from services.granola_retrieval_service import (
    GranolaRetrievalError,
    GranolaRetrievalService,
)
from services.job_service import JobService

import config

DEFAULT_MARKER = "SE-SKILLS-CAPCHECK"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Granola retrieval live check (same code path as the app)")
    parser.add_argument("--date", required=True, help="Day of the synthetic meeting, YYYY-MM-DD")
    parser.add_argument("--marker", default=DEFAULT_MARKER, help="Substring the synthetic meeting title must contain")
    parser.add_argument(
        "--model", default=config._model_for("quick-ask"), help="Model for the restricted relay subprocess (app default)"
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds per relay call")
    return parser.parse_args()


def _digest(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe_outcome(row: dict[str, Any]) -> dict[str, Any]:
    keep = ("outcome", "error_code", "revision", "change", "availability", "processing_status", "association_state")
    return {key: row.get(key) for key in keep if key in row}


async def _wait(jobs: JobService, job_id: str) -> dict[str, Any]:
    while True:
        job = jobs.get_job(job_id)
        if job is None:
            return {"status": "missing"}
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.2)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    day = date.fromisoformat(args.date)
    scratch = Path(tempfile.mkdtemp(prefix="se-granola-live-check-"))
    report: dict[str, Any] = {"marker": args.marker, "date": day.isoformat(), "steps": {}}
    try:
        relay = ClaudeCodeMcpRelay(
            model=args.model,
            forbidden_roots=[repo_root],
            timeout_seconds=args.timeout,
        )
        report["relay_contract"] = relay.describe()
        report["relay_argv_shape"] = [
            arg if not arg.startswith("/") else "<path>" for arg in relay.command("get_account_info", executable="claude")
        ]
        jobs = JobService(scratch, model_for=lambda _use: args.model, persist_run=lambda *_a: None)
        service = GranolaRetrievalService(
            transport=relay,
            adapter=ManualGranolaImportAdapter(),
            ledger=EvidenceLedgerService(scratch / "customers"),
            job_service=jobs,
        )

        check = await service.check_connection()
        report["steps"]["connection_check"] = {
            "connected": check.get("connected"),
            "error_code": check.get("error_code"),
            "retryable": check.get("retryable"),
            "workspace_digest": _digest((check.get("workspace") or {}).get("id")),
            "note_access_scope": check.get("note_access_scope"),
        }
        if not check.get("connected"):
            return report

        listing = await service.list_meetings(time_range="custom", custom_start=day, custom_end=day)
        marked = [row for row in listing["meetings"] if args.marker.lower() in (row.get("title") or "").lower()]
        report["steps"]["list"] = {
            "returned": listing["returned"],
            "shown": listing["shown"],
            "rejected": listing["rejected"],
            "truncated": listing["truncated"],
            "marked_matches": len(marked),
        }
        if not marked:
            return report
        target = marked[0]["meeting_id"]
        report["steps"]["list"]["target_digest"] = _digest(target)

        for attempt in ("first_retrieval", "second_retrieval"):
            started = await service.start_retrieval([target])
            job = await _wait(jobs, started["job_id"])
            step: dict[str, Any] = {"status": job.get("status"), "error_code": job.get("error_code")}
            result = job.get("result") if isinstance(job.get("result"), dict) else None
            if result:
                step["counts"] = result.get("counts")
                step["outcomes"] = [_safe_outcome(row) for row in result.get("results", [])]
            report["steps"][attempt] = step
            if job.get("status") != "done":
                break
        return report
    except GranolaRelayError as exc:
        report["error"] = {"kind": "relay", "code": exc.code, "retryable": exc.retryable}
        return report
    except GranolaRetrievalError as exc:
        report["error"] = {"kind": "retrieval", "code": exc.code, "status": exc.status}
        return report
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    report = asyncio.run(run(_args()))
    print(json.dumps(report, indent=2, sort_keys=True))
    ok = report.get("error") is None and report["steps"].get("connection_check", {}).get("connected") is True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
