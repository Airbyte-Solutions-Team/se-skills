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


@router.post("/{job_id}/cancel")
async def api_cancel_job(job_id: str, request: Request):
    """Stop a running skill run. 404 unknown job; 409 if it is finished or not a skill run."""
    job_service = _get_job_service(request)
    try:
        job = await job_service.cancel_job(job_id)
    except KeyError:
        raise HTTPException(404, "Unknown job") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {k: v for k, v in job.items() if k != "sig"}
