"""Job lifecycle service for the SE Skills webapp.

Owns the in-memory job dictionary, lifecycle transitions, restart recovery,
persistence snapshots, and the `claude -p` background-task runner. This module
intentionally stays free of FastAPI request parsing and route formatting.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import persistence
import security

logger = logging.getLogger(__name__)

_PERSISTENCE_WARNING = "This job will not survive a server restart because state could not be saved."
_RUN_TIMEOUT_SECONDS = 600


class JobService:
    """In-process job service with durable snapshots to `<workspace>/.state/jobs.json`."""

    def __init__(
        self,
        workspace: Path,
        *,
        model_for: Callable[[str], str],
        persist_run: Callable[[str, str | None, str, dict[str, Any]], None],
    ) -> None:
        self.workspace = workspace
        self.model_for = model_for
        self.persist_run = persist_run
        self.jobs: dict[str, dict] = persistence.load_jobs(workspace)

    def get_job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    def list_jobs(
        self,
        account: str | None = None,
        opp_slug: str | None = None,
    ) -> list[dict]:
        """Return job summaries, excluding `sig`, `stdout`, and `stderr`."""
        out: list[dict] = []
        for jid, j in self.jobs.items():
            if account is not None and j.get("account") != account:
                continue
            if opp_slug is not None and j.get("opp_slug") != opp_slug:
                continue
            out.append(
                {"job_id": jid, **{k: v for k, v in j.items() if k not in ("sig", "stdout", "stderr")}}
            )
        return out

    def find_reused_job(self, sig: Any) -> tuple[str, dict] | None:
        """Return (job_id, job) for a running job whose signature matches `sig`."""
        for jid, j in self.jobs.items():
            if j.get("sig") == sig and j.get("status") == "running":
                return jid, j
        return None

    async def save_snapshot(self, source_job_id: str | None = None) -> str | None:
        """Persist the jobs snapshot and manage per-job persistence warnings.

        Returns the warning string if persistence failed, otherwise ``None``.
        """
        ok = await asyncio.to_thread(persistence.save_jobs, self.jobs, self.workspace)
        if ok:
            for job in self.jobs.values():
                job.pop("persistence_warning", None)
            return None
        logger.warning("Jobs snapshot persistence failed for %s", source_job_id or "unknown")
        if source_job_id and source_job_id in self.jobs:
            self.jobs[source_job_id]["persistence_warning"] = _PERSISTENCE_WARNING
        return _PERSISTENCE_WARNING

    async def launch(
        self,
        *,
        account: str,
        opp_slug: str | None,
        skill: str,
        opportunity: str | None,
        sig: Any,
        prompt: str,
        meta: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Create a new running job, persist it, and start the background runner."""
        job_id = uuid.uuid4().hex[:12]
        self.jobs[job_id] = {
            "status": "running",
            "ok": None,
            "stdout": "",
            "stderr": "",
            "skill": skill,
            "account": account,
            "opportunity": opportunity,
            "opp_slug": opp_slug,
            "sig": sig,
            "started_at": datetime.now(timezone.utc).timestamp(),
        }
        persist_warn = await self.save_snapshot(job_id)
        asyncio.create_task(self._run_job(job_id, prompt, meta))
        return job_id, persist_warn

    async def _run_job(self, job_id: str, prompt: str, meta: dict[str, Any]) -> None:
        """Run `claude -p` for `job_id`, updating status, output, and run records."""
        job = self.jobs[job_id]
        model = self.model_for(meta.get("skill", "default"))
        cmd = ["claude", "-p", prompt, "--model", model, "--permission-mode", "acceptEdits"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            job["pid"] = proc.pid
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RUN_TIMEOUT_SECONDS)
            stdout = security.redact_sensitive(stdout.decode(errors="replace"))
            stderr = security.redact_sensitive(stderr.decode(errors="replace"))
            job.update(
                status="done",
                ok=proc.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                finished_at=datetime.now(timezone.utc).timestamp(),
            )
        except FileNotFoundError:
            job.update(
                status="error",
                ok=False,
                stdout="",
                stderr=security.redact_sensitive("`claude` CLI not found on PATH — is Claude Code installed?"),
                finished_at=datetime.now(timezone.utc).timestamp(),
            )
        except asyncio.TimeoutError:
            job.update(
                status="error",
                ok=False,
                stdout="",
                stderr=security.redact_sensitive("Skill run timed out after 10 minutes."),
                finished_at=datetime.now(timezone.utc).timestamp(),
            )
        except Exception as e:  # noqa: BLE001 — surface any launch failure to the UI
            job.update(
                status="error",
                ok=False,
                stdout="",
                stderr=security.redact_sensitive(f"{type(e).__name__}: {e}"),
                finished_at=datetime.now(timezone.utc).timestamp(),
            )

        # Persist the finished result to disk (one file per skill, overwritten).
        try:
            self.persist_run(
                meta["account"],
                meta.get("opp_slug"),
                meta["skill"],
                {
                    "skill": meta["skill"],
                    "opportunity": meta.get("opportunity"),
                    "ok": job.get("ok"),
                    "stdout": job.get("stdout", ""),
                    "stderr": job.get("stderr", ""),
                    "finished_at": datetime.now(timezone.utc).timestamp(),
                },
            )
        except Exception:
            pass  # persistence is best-effort; the in-memory job still works

        # Persist the full jobs snapshot so the job record survives a restart.
        try:
            await self.save_snapshot(job_id)
        except Exception:
            pass  # job-state persistence is best-effort
