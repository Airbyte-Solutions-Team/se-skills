"""Local-only routes for Create Overview canonical opportunity state."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.account_service import AccountError
from services.opportunity_state_create_service import (
    OpportunityStateCreateError,
    OpportunityStateCreateService,
)
from services.opportunity_state_service import OpportunityStateError, OpportunityStateService
from services.transcription_service import TranscriptionError


router = APIRouter()


class CreateOverviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript_ids: list[str] = Field(min_length=1, max_length=50)


def _create_service(request: Request) -> OpportunityStateCreateService:
    return request.app.state.opportunity_state_create_service


def _state_service(request: Request) -> OpportunityStateService:
    return request.app.state.opportunity_state_service


def _raise_domain(exc: Exception) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/api/accounts/{account}/opportunities/{opp_slug}/overview/evidence")
async def api_opportunity_overview_evidence(account: str, opp_slug: str, request: Request) -> dict:
    try:
        return await _create_service(request).list_eligible_evidence(account, opp_slug)
    except (AccountError, TranscriptionError, OpportunityStateCreateError) as exc:
        _raise_domain(exc)


@router.post("/api/accounts/{account}/opportunities/{opp_slug}/overview/create", status_code=202)
async def api_create_opportunity_overview(
    account: str,
    opp_slug: str,
    body: CreateOverviewBody,
    request: Request,
) -> dict:
    try:
        return await _create_service(request).start_create(account, opp_slug, body.transcript_ids)
    except (AccountError, TranscriptionError, OpportunityStateError, OpportunityStateCreateError) as exc:
        _raise_domain(exc)


@router.get("/api/accounts/{account}/opportunities/{opp_slug}/overview/jobs/{job_id}")
async def api_opportunity_overview_job(
    account: str,
    opp_slug: str,
    job_id: str,
    request: Request,
) -> dict:
    service = _create_service(request)
    try:
        identity = await service.resolve_identity(account, opp_slug)
        return service.get_create_job(identity["safe_account"], identity["safe_opp"], job_id)
    except (AccountError, OpportunityStateCreateError) as exc:
        _raise_domain(exc)


@router.get("/api/accounts/{account}/opportunities/{opp_slug}/overview/state")
async def api_current_opportunity_state(account: str, opp_slug: str, request: Request) -> dict:
    create_service = _create_service(request)
    try:
        identity = await create_service.resolve_identity(account, opp_slug)
        inspected = _state_service(request).inspect_current(identity["safe_account"], identity["safe_opp"])
        if inspected["status"] == "not_created":
            raise HTTPException(404, "No canonical overview exists for this opportunity.")
        if inspected["status"] == "malformed":
            raise HTTPException(409, "Canonical overview storage is malformed.")
        return inspected["current"]
    except (AccountError, OpportunityStateError) as exc:
        _raise_domain(exc)
