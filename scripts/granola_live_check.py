#!/usr/bin/env -S uv run --script
r"""Local, user-run check of the Granola retrieval path through the real app code.

Runs the same `ClaudeCodeMcpRelay` → `GranolaRetrievalService` → adapter → ledger
chain the web app uses, against a throwaway ledger directory. Four modes, each a
strict gate that exits 0 only when it passes:

  --probe-only        claude version preflight → MCP scope probe (no subprocess)
  --account-shape     …→ one restricted `get_account_info` call, reported as a
                      value-free SHAPE (allow-listed key names, value classes,
                      presence flags, safe error codes); diagnostic only
  --connection-only   …→ the app's connection check (records a check marker in
                      the throwaway ledger; no workspace identity is read)
  --date YYYY-MM-DD   …→ connection check → metadata list for that day →
                      retrieve the ONE meeting whose title contains --marker →
                      retrieve again (expects `imported` then `already_known`)

Verdict (`verdict.ok`, also the exit code) requires, cumulatively per mode:
  * `claude --version` is at least MIN_CLAUDE_VERSION (`--permission-prompts` exists),
  * the `granola` server is found at local scope for a trusted project path
    (this checkout, or `--claude-project <original checkout>` when running from
    a sibling worktree) or at user scope,
  * (--account-shape) the tool returned a successful, parseable result,
  * (--connection-only / --date) the connection check succeeds,
  * (--date) exactly one meeting on that day carries the marker, the first
    retrieval yields the single outcome `imported`, the second `already_known`.
Anything else is reported under `verdict.failures` and exits 1.

Output is a single JSON object containing step statuses, safe error codes,
counts, outcome codes from the fixed outcome allow-list, and a SHA-256 prefix
of the meeting id. It never prints titles, summaries,
transcripts, tokens, emails, URLs, raw tool output or model output. The
throwaway ledger is deleted on exit. Nothing is written to your customers
directory or to the app's ledger.

    uv run scripts/granola_live_check.py --probe-only --claude-project C:\path\to\original\checkout
    uv run scripts/granola_live_check.py --account-shape --claude-project C:\path\to\original\checkout
    uv run scripts/granola_live_check.py --connection-only --claude-project C:\path\to\original\checkout
    uv run scripts/granola_live_check.py --date 2026-09-24 --claude-project C:\path\to\original\checkout

Prerequisites: `claude` on PATH and the `granola` MCP server configured in your
own Claude Code (`claude mcp add` default local scope in the project you ran it
from, or `--scope user`). The `--date` mode additionally needs a Granola note
that YOU created in YOUR Granola account for this purpose (synthetic content,
no customer material) whose title contains the marker. The default marker
`SE-SKILLS-CAPCHECK` is only a string this repo's test fixtures use — no such
Granola note exists unless you make one; pass `--marker` to match a note you
already own instead.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "webapp"))

from integrations.granola import ManualGranolaImportAdapter
from integrations.granola_mcp_relay import (
    ClaudeCodeMcpRelay,
    GranolaRelayError,
    GranolaRetrievalTransport,
    probe_server_scope,
    scoped_server_definition,
)
from services.evidence_ledger_service import EvidenceLedgerService
from services.granola_retrieval_service import (
    GranolaRetrievalError,
    GranolaRetrievalService,
    account_shape_report,
)
from services.job_service import JobService

import config

DEFAULT_MARKER = "SE-SKILLS-CAPCHECK"
MIN_CLAUDE_VERSION = (2, 1, 259)  # `--permission-prompts` (official CLI reference)
SAFE_OUTCOMES = frozenset(
    {"imported", "already_known", "edited", "pending_content", "inaccessible", "failed_retryable", "rejected"}
)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")

VersionProbe = Callable[[], str | None]
TransportFactory = Callable[[argparse.Namespace], GranolaRetrievalTransport]


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Granola retrieval live check (same code path as the app)")
    parser.add_argument("--date", help="Day of the synthetic meeting, YYYY-MM-DD (required unless --probe-only)")
    parser.add_argument(
        "--claude-project",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional trusted checkout whose local-scope Claude MCP servers may be used "
        "(the original checkout, when running from a sibling worktree). Repeatable.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe-only", action="store_true", help="Only report claude version and MCP scope, then exit")
    mode.add_argument(
        "--account-shape",
        action="store_true",
        help="One restricted get_account_info call; print its value-free shape (key names, value classes) and exit",
    )
    mode.add_argument("--connection-only", action="store_true", help="Stop after the app's connection check")
    parser.add_argument(
        "--marker",
        default=DEFAULT_MARKER,
        help="Substring the title of YOUR synthetic Granola note must contain (default is the repo fixture marker; "
        "no such note exists in Granola unless you create one)",
    )
    parser.add_argument(
        "--model", default=config._model_for("quick-ask"), help="Model for the restricted relay subprocess (app default)"
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds per relay call")
    args = parser.parse_args(argv)
    preflight_only = args.probe_only or args.account_shape or args.connection_only
    if not preflight_only and not args.date:
        parser.error("--date is required unless --probe-only, --account-shape or --connection-only is given")
    if preflight_only and args.date:
        parser.error("--date cannot be combined with --probe-only, --account-shape or --connection-only")
    return args


def trusted_projects(args: argparse.Namespace) -> list[Path]:
    return [repo_root, *(Path(p) for p in args.claude_project)]


def _digest(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe_outcome(row: dict[str, Any]) -> dict[str, Any]:
    outcome = row.get("outcome")
    safe: dict[str, Any] = {"outcome": outcome if outcome in SAFE_OUTCOMES else "unknown"}
    code = row.get("error_code")
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code):
        safe["error_code"] = code
    for key in ("revision", "change", "availability", "processing_status", "association_state"):
        value = row.get(key)
        if isinstance(value, (int, str)) and (not isinstance(value, str) or re.fullmatch(r"[a-z0-9_]{1,64}", value)):
            safe[key] = value
    return safe


def claude_version_string() -> str | None:
    executable = shutil.which("claude")
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (completed.stdout or completed.stderr or "").strip() or None


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION.search(text or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def default_transport(args: argparse.Namespace) -> GranolaRetrievalTransport:
    return ClaudeCodeMcpRelay(
        model=args.model,
        forbidden_roots=[repo_root],
        timeout_seconds=args.timeout,
        server_definition=scoped_server_definition(trusted_projects(args)),
    )


async def _wait(jobs: JobService, job_id: str) -> dict[str, Any]:
    for _ in range(3000):
        job = jobs.get_job(job_id)
        if job is None:
            return {"status": "missing"}
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.2)
    return {"status": "timeout"}


def _retrieval_step(job: dict[str, Any]) -> dict[str, Any]:
    # JobService merges the runner's result into the job record, so `counts` and
    # `results` are top-level fields; there is no nested `result`.
    step: dict[str, Any] = {"status": job.get("status")}
    code = job.get("error_code")
    if isinstance(code, str):
        step["error_code"] = code
    counts = job.get("counts")
    if isinstance(counts, dict):
        step["counts"] = {k: v for k, v in counts.items() if k in SAFE_OUTCOMES and isinstance(v, int)}
    results = job.get("results")
    if isinstance(results, list):
        step["outcomes"] = [_safe_outcome(row) for row in results if isinstance(row, dict)]
    return step


def _expect_single(step: dict[str, Any], outcome: str, label: str, failures: list[str]) -> None:
    if step.get("status") != "done":
        failures.append(f"{label}: job status {step.get('status')!r}, error_code {step.get('error_code')!r}")
        return
    outcomes = [row.get("outcome") for row in step.get("outcomes", [])]
    if outcomes != [outcome]:
        failures.append(f"{label}: expected outcomes ['{outcome}'], got {outcomes!r}")


async def run(
    args: argparse.Namespace,
    *,
    transport_factory: TransportFactory = default_transport,
    version_probe: VersionProbe = claude_version_string,
) -> dict[str, Any]:
    day = date.fromisoformat(args.date) if args.date else None
    scratch = Path(tempfile.mkdtemp(prefix="se-granola-live-check-"))
    report: dict[str, Any] = {
        "marker": args.marker,
        "date": day.isoformat() if day else None,
        "trusted_project_count": len(trusted_projects(args)),
        "steps": {},
    }
    failures: list[str] = []
    report["verdict"] = {"ok": False, "failures": failures}
    try:
        version = parse_version(version_probe())
        report["steps"]["claude_version"] = {
            "found": version is not None,
            "version": ".".join(map(str, version)) if version else None,
            "minimum": ".".join(map(str, MIN_CLAUDE_VERSION)),
            "sufficient": version is not None and version >= MIN_CLAUDE_VERSION,
        }
        if version is None:
            failures.append("claude not found on PATH or `claude --version` unreadable")
            return report
        if version < MIN_CLAUDE_VERSION:
            failures.append("claude older than minimum; run `claude update` (needs --permission-prompts)")
            return report

        scope = probe_server_scope("granola", project_paths=trusted_projects(args))
        report["steps"]["mcp_scope"] = scope
        if not scope["configured"]:
            failures.append(
                "granola MCP server not found at local scope for a trusted project or at user scope; "
                "pass --claude-project <original checkout> if it was added there"
            )
            return report
        if args.probe_only:
            return report

        relay = transport_factory(args)
        if args.account_shape:
            try:
                raw = await relay.call("get_account_info", {})
            except GranolaRelayError as exc:
                report["steps"]["account_shape"] = {"ok": False, "error_code": exc.code, "retryable": exc.retryable}
                failures.append(f"get_account_info failed: {exc.code!r}")
                return report
            shape = account_shape_report(raw)
            report["steps"]["account_shape"] = {"ok": True, **shape}
            if shape["looks_like_error_envelope"]:
                failures.append("get_account_info returned an error-shaped object without the error flag")
            return report
        report["relay_contract"] = relay.describe()
        if isinstance(relay, ClaudeCodeMcpRelay):
            report["relay_argv_shape"] = [
                arg if not arg.startswith("/") else "<path>"
                for arg in relay.command("get_account_info", executable="claude")
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
            "workspace_guarantee": check.get("workspace_guarantee"),
            "note_access_scope": check.get("note_access_scope"),
        }
        if not check.get("connected"):
            failures.append(f"connection check failed: {check.get('error_code')!r}")
            return report
        if args.connection_only or day is None:
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
        if len(marked) != 1:
            failures.append(
                f"expected exactly 1 meeting titled with the marker on {day.isoformat()}, found {len(marked)}; "
                "the marker must match a synthetic note you created in your own Granola account"
            )
            return report
        target = marked[0]["meeting_id"]
        report["steps"]["list"]["target_digest"] = _digest(target)

        for attempt, expected in (("first_retrieval", "imported"), ("second_retrieval", "already_known")):
            started = await service.start_retrieval([target])
            step = _retrieval_step(await _wait(jobs, started["job_id"]))
            report["steps"][attempt] = step
            _expect_single(step, expected, attempt, failures)
            if step.get("status") != "done":
                break
        return report
    except GranolaRelayError as exc:
        report["error"] = {"kind": "relay", "code": exc.code, "retryable": exc.retryable}
        failures.append(f"relay error {exc.code}")
        return report
    except GranolaRetrievalError as exc:
        report["error"] = {"kind": "retrieval", "code": exc.code, "status": exc.status}
        failures.append(f"retrieval error {exc.code}")
        return report
    finally:
        report["verdict"]["ok"] = not failures
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    report = asyncio.run(run(_args(argv)))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["verdict"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
