"""HTTP routes for the job subsystem.

These routes are thin: they validate parameters, call the `JobService`, and return
the same shapes the UI already expects.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/jobs")


def _get_job_service(request: Request):
    """Return the application's shared `JobService`."""
    return request.app.state.job_service


@router.get("/{job_id}")
def api_job(job_id: str, request: Request):
    """Return a single job, including stdout/stderr but not the dedupe signature."""
    job_service = _get_job_service(request)
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return {k: v for k, v in job.items() if k != "sig"}


@router.get("")
def api_jobs_for(account: str | None = None, opp_slug: str | None = None, request: Request = None):
    """List jobs, optionally filtered by account and/or opportunity slug."""
    job_service = _get_job_service(request)
    return job_service.list_jobs(account, opp_slug)
