#!/usr/bin/env python3
"""Run original-vs-ported parity scenarios for worker-analysis in parallel.

Uses the real `claude` CLI on the Devin box. Outputs are written to
`eval/fixtures/worker_analysis/` for the final validation report.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "eval" / "fixtures" / "worker_analysis"
ORIGINAL_SKILL = Path.home() / "repos" / "ai-skills" / ".agents" / "skills" / "worker-analysis" / "SKILL.md"
WORKSPACE_FIXTURE = REPO_ROOT / "eval" / "fixtures" / "worker_analysis_workspace.json"

SCENARIOS = {
    "questionnaire_complete": (
        "Use the worker-analysis skill for Acme. "
        "Estimate workers for a prospect with 45 connections: "
        "30 are SaaS APIs (Salesforce, HubSpot, Stripe) and 15 are databases (Postgres, MySQL). "
        "Sync frequencies: 20% every 15 min, 30% hourly, 50% daily. "
        "Average sync duration 10 minutes. Peak window 2-6 AM UTC. "
        "Need data fresh within 1 hour for key reports. "
        "Growth to 80 connections in 6 months. Two environments (prod and staging). "
        "No initial load. Recurring incremental workload."
    ),
    "cadence_preservation": (
        "Use the worker-analysis skill for Acme. "
        "Estimate workers for a prospect. The customer requires hourly synchronization for all critical reports. "
        "50 connections: 35 are SaaS APIs (Salesforce, HubSpot, Stripe, Shopify, Zendesk) and 15 are databases (Postgres, MySQL, Snowflake). "
        "Schedule: 40% hourly, 25% every 15 minutes, 35% daily. "
        "Average sync duration 12 minutes. Peak window 1-5 AM UTC. "
        "Growth to 80 connections in 6 months. Two environments (prod and staging). "
        "No initial load. Recurring incremental workload."
    ),
    "incomplete_evidence": (
        "Use the worker-analysis skill for Acme. "
        "Estimate workers for a prospect. We have about 40 connections, a mix of SaaS APIs and databases, "
        "but we do not have the exact API/DB split or the average sync duration. "
        "Some connections run hourly and some run daily. We expect to grow but do not have a firm number. "
        "What would you estimate and what else do you need to know?"
    ),
    "workspace_oss": (
        "Use the worker-analysis skill for Acme. "
        f"Analyze the OSS export at {WORKSPACE_FIXTURE}. "
        "The export is from workspace ws-synthetic-001. "
        "Identify peak concurrency, long-running jobs, failed or retried jobs, and provide worker recommendations."
    ),
}


async def run_one(
    prompt: str,
    output_path: Path,
    *,
    original: bool = False,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        cmd = [
            "claude",
            "-p",
            prompt,
            "--model",
            "claude-sonnet-4-6",
            "--bare",
            "--dangerously-skip-permissions",
        ]
        if original:
            cmd.extend(["--append-system-prompt-file", str(ORIGINAL_SKILL)])
        print(f"Running: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=REPO_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            returncode = -1
        else:
            returncode = proc.returncode

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(stdout.decode("utf-8", errors="replace"), encoding="utf-8")
        meta_path = output_path.with_suffix(".json")
        meta = {
            "command": cmd,
            "returncode": returncode,
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        print(f"  -> {output_path} (exit {returncode})")


async def main() -> None:
    tasks = []
    semaphore = asyncio.Semaphore(4)
    for name, prompt in SCENARIOS.items():
        for kind in ("se-suite", "original"):
            out = FIXTURE_DIR / kind / f"{name}.md"
            tasks.append(
                asyncio.create_task(
                    run_one(prompt, out, original=(kind == "original"), semaphore=semaphore)
                )
            )
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
