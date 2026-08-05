#!/usr/bin/env python3
"""End-to-end validation of worker-analysis through the SE Skills webapp."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
from httpx import ASGITransport

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "webapp"))

import config
from webapp.app import create_app


def _httpx_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = repo_root / "eval" / "fixtures" / "e2e_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    customers_dir = workspace / "01-customers"
    customers_dir.mkdir(parents=True, exist_ok=True)

    config.WORKSPACE = workspace
    config.CUSTOMERS_DIR = customers_dir

    app = create_app()

    async with _httpx_client(app) as client:
        # 1. Skill discovery
        resp = await client.get("/api/skills")
        assert resp.status_code == 200, resp.text
        skills = resp.json()
        skill_ids = {s["id"] for s in skills}
        assert "worker-analysis" in skill_ids, f"worker-analysis not in skills: {skill_ids}"
        print("[OK] skill discovery includes worker-analysis")

        # 2. Prerequisite plan for questionnaire mode
        resp = await client.get("/api/plan", params={"skill": "worker-analysis", "account": "Synthetic"})
        assert resp.status_code == 200, resp.text
        plan = resp.json()
        print(f"[INFO] plan: {json.dumps(plan, indent=2)}")
        assert plan.get("ready") is True, f"plan not ready: {plan}"
        modes = plan.get("modes", {})
        assert modes.get("questionnaire", {}).get("ready") is True, f"questionnaire mode not ready: {modes}"
        print("[OK] prerequisite plan ready and questionnaire mode available")

        # 3. Permissions
        resp = await client.get("/api/permissions", params={"skill": "worker-analysis"})
        assert resp.status_code == 200, resp.text
        perms = resp.json()
        print(f"[INFO] permissions: {json.dumps(perms, indent=2)}")
        assert perms.get("write") is True and perms.get("shell") is True, f"unexpected permissions: {perms}"
        print("[OK] permission profile includes write and shell")

        # 4. Invoke worker-analysis for a complete questionnaire
        extra = (
            "Estimate workers for a prospect with 20 connections: 14 are SaaS APIs and 6 are databases. "
            "Sync frequencies: 20% every 15 min, 30% hourly, 50% daily. "
            "Average sync duration 8 minutes. Peak window 2-6 AM UTC. "
            "Two environments (prod and staging). No initial load."
        )
        resp = await client.post(
            "/api/invoke",
            json={
                "skill": "worker-analysis",
                "account": "Synthetic",
                "extra": extra,
                "approve_permissions": True,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        print(f"[INFO] invoke response: {json.dumps(body, indent=2)}")
        job_id = body.get("job_id")
        assert job_id, f"no job_id in invoke response: {body}"
        print("[OK] job created")

        # 5. Poll until terminal state
        status = "running"
        for i in range(120):
            await asyncio.sleep(5)
            resp = await client.get(f"/api/jobs/{job_id}")
            if resp.status_code != 200:
                print(f"[WARN] job poll status {resp.status_code}: {resp.text}")
                continue
            job = resp.json()
            status = job.get("status")
            if status in ("done", "error"):
                print(f"[INFO] job terminal after {i * 5}s: {status}")
                break
            print(f"[INFO] job status after {i * 5}s: {status}")
        else:
            print("[FAIL] job did not reach terminal state within 600s")
            return 1

        print(f"[INFO] final job: {json.dumps(job, indent=2, default=str)}")
        assert status == "done" and job.get("ok") is True, f"job failed: {job}"
        assert "Data Worker" in job.get("stdout", "") or "worker" in job.get("stdout", "").lower(), "no worker content in stdout"
        print("[OK] job completed with worker analysis in stdout")

        # 6. Output history / reader
        resp = await client.get("/api/accounts/Synthetic/outputs")
        assert resp.status_code == 200, resp.text
        outputs = resp.json()
        print(f"[INFO] outputs: {json.dumps(outputs, indent=2, default=str)}")

    # 7. Restart persistence check: reload app and ensure job is still done
    app2 = create_app()
    job2 = app2.state.job_service.get_job(job_id)
    assert job2 is not None, "job not persisted across app restart"
    assert job2.get("status") == "done" and job2.get("ok") is True, "job state changed after restart"
    print("[OK] job state survives app restart")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
