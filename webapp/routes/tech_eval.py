"""Local-only Tech Eval / POV Readiness tracker routes."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.account_service import AccountError
from services.tech_eval_service import TechEvalError, TechEvalService


router = APIRouter()


class UpdateTechEvalItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["not_started", "in_progress", "blocked", "done", "not_applicable"] | None = None
    owner: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_manual_change(self) -> "UpdateTechEvalItemBody":
        if not self.model_fields_set:
            raise ValueError("A status, owner, or note change is required.")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        return self


def _raise_domain(exc: Exception) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _identity(request: Request, account: str, opp_slug: str) -> dict:
    return await request.app.state.opportunity_workspace_service.resolve_identity(account, opp_slug)


def _service(request: Request) -> TechEvalService:
    return request.app.state.tech_eval_service


@router.get("/api/accounts/{account}/opportunities/{opp_slug}/tech-eval")
async def api_tech_eval_tracker(account: str, opp_slug: str, request: Request) -> dict:
    try:
        identity = await _identity(request, account, opp_slug)
        return _service(request).get_tracker(identity["safe_account"], identity["safe_opp"])
    except (AccountError, TechEvalError) as exc:
        _raise_domain(exc)


@router.patch("/api/accounts/{account}/opportunities/{opp_slug}/tech-eval/items/{item_id}")
async def api_update_tech_eval_item(
    account: str,
    opp_slug: str,
    item_id: str,
    body: UpdateTechEvalItemBody,
    request: Request,
) -> dict:
    try:
        identity = await _identity(request, account, opp_slug)
        return _service(request).update_item(
            identity["safe_account"],
            identity["safe_opp"],
            item_id,
            body.model_dump(exclude_unset=True),
        )
    except (AccountError, TechEvalError) as exc:
        _raise_domain(exc)
