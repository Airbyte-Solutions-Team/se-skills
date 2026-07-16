"""Landing-page overview aggregation for the SE Skills webapp.

Builds a calm operational summary by combining team members, accounts,
opportunities, outputs, and job state. It reads data through the existing
focused services and does not mutate account, job, or output state.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone

from services.account_service import AccountService
from services.job_service import JobService
from services.output_service import OutputService

logger = logging.getLogger(__name__)


class OverviewService:
    """Aggregate filesystem and job state into the landing-page overview."""

    _LONG_RUNNING_MINUTES = 5
    _ATTENTION_FAILURE_HOURS = 24
    _STALE_ACTIVITY_DAYS = 7
    _MAX_ATTENTION = 10
    _MAX_RECENT = 12

    def __init__(
        self,
        account_service: AccountService,
        output_service: OutputService,
        job_service: JobService,
    ) -> None:
        self._account_service = account_service
        self._output_service = output_service
        self._job_service = job_service

    def build_overview(self, jobs: dict[str, dict] | None = None) -> dict:
        """Return the full overview response.

        The optional `jobs` argument lets deterministic tests inject job state
        without instantiating a real `JobService`. When omitted, the service
        reads a shallow-copied snapshot from `JobService`.
        """
        now = datetime.now(timezone.utc).timestamp()

        members = self._account_service.load_team()
        all_accounts = self._account_service.list_accounts()
        active_accounts = [a for a in all_accounts if not a["archived"]]
        archived_count = len(all_accounts) - len(active_accounts)

        recent_outputs, needs_attention_outputs, account_meta = (
            self._output_service.walk_all_outputs()
        )

        job_records = jobs if jobs is not None else self._job_service.overview_jobs()
        running_jobs, failed_jobs, done_jobs = self._classify_jobs(job_records)

        self._apply_job_timestamps(account_meta, running_jobs, failed_jobs, done_jobs)

        member_rows = self._build_member_rows(
            members, active_accounts, account_meta, running_jobs, failed_jobs, done_jobs, now
        )
        summary = self._build_summary(
            members,
            active_accounts,
            archived_count,
            account_meta,
            running_jobs,
            failed_jobs,
            done_jobs,
            now,
        )
        attention = self._build_attention(
            running_jobs,
            failed_jobs,
            needs_attention_outputs,
            active_accounts,
            account_meta,
            now,
        )
        recent = self._build_recent(recent_outputs, running_jobs, failed_jobs, done_jobs, now)
        empty = {
            "members": not members,
            "accounts": not active_accounts,
            "attention": not attention,
            "recent": not recent,
        }

        return {
            "summary": summary,
            "attention": attention,
            "recent": recent,
            "members": member_rows,
            "empty": empty,
        }

    def _classify_jobs(self, jobs: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """Split jobs into running, failed, and completed groups.

        Each copy carries its `job_id` so downstream builders can reference it.
        """
        running_jobs: list[dict] = []
        failed_jobs: list[dict] = []
        done_jobs: list[dict] = []

        for job_id, job in jobs.items():
            status = job.get("status")
            job_copy = dict(job)
            job_copy["job_id"] = job_id

            if status == "running":
                running_jobs.append(job_copy)
            elif status == "error" or (status == "done" and job.get("ok") is False):
                failed_jobs.append(job_copy)
            elif status == "done":
                done_jobs.append(job_copy)

        return running_jobs, failed_jobs, done_jobs

    def _apply_job_timestamps(
        self,
        account_meta: dict[str, dict],
        running_jobs: list[dict],
        failed_jobs: list[dict],
        done_jobs: list[dict],
    ) -> None:
        """Promote per-account `last_updated_ts` based on job timestamps."""
        for job in [*running_jobs, *failed_jobs, *done_jobs]:
            account = job.get("account")
            if not account:
                continue
            when = job.get("finished_at") or job.get("started_at") or 0
            meta = account_meta.get(account)
            if meta and when > meta["last_updated_ts"]:
                meta["last_updated_ts"] = when

    def _build_member_rows(
        self,
        members: list[dict],
        active_accounts: list[dict],
        account_meta: dict[str, dict],
        running_jobs: list[dict],
        failed_jobs: list[dict],
        done_jobs: list[dict],
        now: float,
    ) -> list[dict]:
        """Return one enriched row per team member."""
        rows: list[dict] = []
        for member in members:
            visible = [
                a for a in active_accounts if a["owner"] == member["id"] or a["owner"] is None
            ]
            names = [a["name"] for a in visible]

            row: dict = {
                "id": member["id"],
                "name": member["name"],
                "role": member.get("role"),
                "email": member.get("email"),
                "account_count": len(names),
                "output_count": 0,
                "needs_attention": 0,
                "opp_count": 0,
                "running_jobs": 0,
                "recent_failures": 0,
                "last_activity_ts": 0.0,
                "last_output": None,
            }
            last_output_entry: dict | None = None

            for name in names:
                am = account_meta.get(name)
                if not am:
                    continue
                row["output_count"] += am["output_count"]
                row["needs_attention"] += am["needs_attention"]
                row["opp_count"] += am["opp_count"]
                if am["last_updated_ts"] > row["last_activity_ts"]:
                    row["last_activity_ts"] = am["last_updated_ts"]
                last = am["last_output"]
                if last and (
                    last_output_entry is None or last["mtime"] > last_output_entry["mtime"]
                ):
                    last_output_entry = last

            for job in running_jobs:
                if job.get("account") in names:
                    row["running_jobs"] += 1
                    started = job.get("started_at") or 0
                    if started > row["last_activity_ts"]:
                        row["last_activity_ts"] = started

            for job in failed_jobs:
                if job.get("account") in names:
                    finished = job.get("finished_at")
                    if finished is None or now - finished <= self._ATTENTION_FAILURE_HOURS * 3600:
                        row["recent_failures"] += 1
                    if finished and finished > row["last_activity_ts"]:
                        row["last_activity_ts"] = finished

            for job in done_jobs:
                if job.get("account") in names:
                    finished = job.get("finished_at") or 0
                    if finished > row["last_activity_ts"]:
                        row["last_activity_ts"] = finished

            row["last_output"] = last_output_entry
            rows.append(row)

        return rows

    def _build_summary(
        self,
        members: list[dict],
        active_accounts: list[dict],
        archived_count: int,
        account_meta: dict[str, dict],
        running_jobs: list[dict],
        failed_jobs: list[dict],
        done_jobs: list[dict],
        now: float,
    ) -> dict:
        """Return the top-level summary counts."""
        total_outputs = sum(m["output_count"] for m in account_meta.values())
        total_needs_attention = sum(m["needs_attention"] for m in account_meta.values())
        recent_failure_count = sum(
            1
            for job in failed_jobs
            if job.get("finished_at") is None
            or now - job["finished_at"] <= self._ATTENTION_FAILURE_HOURS * 3600
        )

        all_activity_ts = (
            [m["last_updated_ts"] for m in account_meta.values()]
            + [j.get("started_at") or 0 for j in running_jobs]
            + [j.get("finished_at") or 0 for j in [*failed_jobs, *done_jobs]]
        )
        global_last_activity = max(all_activity_ts) if all_activity_ts else 0.0

        return {
            "members": len(members),
            "active_accounts": len(active_accounts),
            "archived_accounts": archived_count,
            "opportunities": sum(
                account_meta[a["name"]]["opp_count"]
                for a in active_accounts
                if a["name"] in account_meta
            ),
            "outputs": total_outputs,
            "running_jobs": len(running_jobs),
            "recent_failures": recent_failure_count,
            "needs_attention": total_needs_attention,
            "last_activity": global_last_activity,
        }

    def _build_attention(
        self,
        running_jobs: list[dict],
        failed_jobs: list[dict],
        needs_attention_outputs: list[dict],
        active_accounts: list[dict],
        account_meta: dict[str, dict],
        now: float,
    ) -> list[dict]:
        """Return attention items sorted by severity and recency."""
        attention: list[dict] = []

        for job in running_jobs:
            account = job.get("account", "unknown")
            opp_slug = job.get("opp_slug")
            opp_name = job.get("opportunity") or (
                opp_slug.capitalize() if opp_slug else account
            )
            started = job.get("started_at")
            duration_min = int((now - started) / 60) if started else None
            long_running = (
                started and duration_min is not None and duration_min >= self._LONG_RUNNING_MINUTES
            )
            attention.append({
                "type": "long-running" if long_running else "running",
                "level": "warn" if long_running else "info",
                "skill": job.get("skill", "skill"),
                "account": account,
                "opp_slug": opp_slug,
                "opp_name": opp_name,
                "when": started or now,
                "duration_min": duration_min,
                "job_id": job.get("job_id"),
                "href": (
                    f"#/opp/{urllib.parse.quote(account)}/{urllib.parse.quote(opp_slug or '')}/{urllib.parse.quote(opp_name)}"
                    if opp_slug
                    else f"#/account/{urllib.parse.quote(account)}"
                ),
            })

        for job in failed_jobs:
            finished = job.get("finished_at")
            if finished and now - finished > self._ATTENTION_FAILURE_HOURS * 3600:
                continue
            account = job.get("account", "unknown")
            opp_slug = job.get("opp_slug")
            opp_name = job.get("opportunity") or (
                opp_slug.capitalize() if opp_slug else account
            )
            attention.append({
                "type": "failure",
                "level": "error",
                "skill": job.get("skill", "skill"),
                "account": account,
                "opp_slug": opp_slug,
                "opp_name": opp_name,
                "when": finished or now,
                "error": (job.get("stderr") or "").splitlines()[0][:120]
                if job.get("stderr")
                else "",
                "job_id": job.get("job_id"),
                "href": (
                    f"#/opp/{urllib.parse.quote(account)}/{urllib.parse.quote(opp_slug or '')}/{urllib.parse.quote(opp_name)}"
                    if opp_slug
                    else f"#/account/{urllib.parse.quote(account)}"
                ),
            })

        for out in sorted(
            needs_attention_outputs, key=lambda x: x["mtime"], reverse=True
        )[: self._MAX_ATTENTION]:
            # Validation issues are more urgent than review queue items; stale and
            # incomplete outputs still need attention but are less severe than invalid.
            if out["type"] == "review":
                level = "warn"
            else:
                level = "error" if out["validation_status"] == "invalid" else "warn"
            attention.append({
                "type": out["type"],
                "level": level,
                "skill": out["skill"],
                "account": out["account"],
                "opp_slug": out["opp_slug"],
                "opp_name": out["opp_name"],
                "filename": out["filename"],
                "when": out["mtime"],
                "status": out["status"],
                "validation_status": out["validation_status"],
                "review_status": out["review_status"],
                "href": self._output_service.output_href(
                    out["account"], out["opp_slug"], out["opp_name"], out["path"]
                ),
            })

        for account in active_accounts:
            am = account_meta.get(account["name"])
            if not am:
                continue
            last = am["last_updated_ts"]
            if last and now - last > self._STALE_ACTIVITY_DAYS * 24 * 3600 and am["output_count"] > 0:
                attention.append({
                    "type": "stale",
                    "level": "info",
                    "account": account["name"],
                    "when": last,
                    "href": f"#/account/{urllib.parse.quote(account['name'])}",
                })

        attention.sort(
            key=lambda x: (
                0 if x["level"] == "error" else (1 if x["level"] == "warn" else 2),
                -x["when"],
            )
        )
        return attention[: self._MAX_ATTENTION]

    def _build_recent(
        self,
        recent_outputs: list[dict],
        running_jobs: list[dict],
        failed_jobs: list[dict],
        done_jobs: list[dict],
        now: float,
    ) -> list[dict]:
        """Return recent output and job activity sorted newest first."""
        recent: list[dict] = []

        for out in recent_outputs:
            recent.append({
                "type": "output",
                "skill": out["skill"],
                "account": out["account"],
                "opp_slug": out["opp_slug"],
                "opp_name": out["opp_name"],
                "filename": out["filename"],
                "when": out["mtime"],
                "needs_attention": out["needs_attention"],
                "href": self._output_service.output_href(
                    out["account"], out["opp_slug"], out["opp_name"], out["path"]
                ),
            })

        for job in [*running_jobs, *failed_jobs, *done_jobs]:
            account = job.get("account", "unknown")
            opp_slug = job.get("opp_slug")
            opp_name = job.get("opportunity") or (
                opp_slug.capitalize() if opp_slug else account
            )
            status = job.get("status")
            ok = job.get("ok")

            if status == "running":
                when = job.get("started_at") or now
                event_type = "job_started"
            elif status == "error" or ok is False:
                when = job.get("finished_at") or now
                event_type = (
                    "job_recovered" if "Server restarted" in (job.get("stderr") or "") else "job_error"
                )
            else:
                when = job.get("finished_at") or now
                event_type = "job_done"

            recent.append({
                "type": event_type,
                "skill": job.get("skill", "skill"),
                "account": account,
                "opp_slug": opp_slug,
                "opp_name": opp_name,
                "when": when,
                "ok": job.get("ok"),
                "job_id": job.get("job_id"),
                "href": (
                    f"#/opp/{urllib.parse.quote(account)}/{urllib.parse.quote(opp_slug or '')}/{urllib.parse.quote(opp_name)}"
                    if opp_slug
                    else f"#/account/{urllib.parse.quote(account)}"
                ),
            })

        recent.sort(key=lambda x: x["when"], reverse=True)
        return recent[: self._MAX_RECENT]
