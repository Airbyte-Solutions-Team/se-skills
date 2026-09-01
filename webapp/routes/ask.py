"""Ask HTTP routes for the SE Skills webapp."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.account_service import AccountService
from services.ask_service import (
    AskError,
    AskResult,
    AskService,
    clear_member_api_key,
    member_api_key_configured,
    save_member_api_key,
)

router = APIRouter()


class OutputAsk(BaseModel):
    path: str = Field(max_length=500)             # output file, relative to CUSTOMERS_DIR
    question: str = Field(max_length=5_000)
    account: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)
    force_deep: bool = False                      # manual "Go deeper" escalation
    prior_answer: str | None = Field(default=None, max_length=8_000)


class MemberApiKey(BaseModel):
    api_key: str = Field(min_length=1, max_length=200)


def _get_ask_service(request: Request) -> AskService:
    return request.app.state.ask_service


def _get_account_service(request: Request) -> AccountService:
    return request.app.state.account_service


@router.post("/api/output/ask")
async def api_output_ask(body: OutputAsk, request: Request):
    """Follow-up Q&A against an opened output doc. Quick → Claude API (doc as
    context); deep (codebase/connectors) → claude -p. Mirrors the live ask."""
    member_id = _get_account_service(request).owner_for_account(body.account)
    try:
        result = await _get_ask_service(request).output_ask(
            path=body.path,
            question=body.question,
            account=body.account,
            opportunity=body.opportunity,
            force_deep=body.force_deep,
            prior_answer=body.prior_answer,
            member_id=member_id,
        )
    except AskError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    if result.kind == "quick":
        from sse_starlette.sse import EventSourceResponse
        return EventSourceResponse(result.stream)

    if result.kind == "deep":
        payload = {"mode": "deep", "job_id": result.job_id}
        if result.persistence_warning:
            payload["persistence_warning"] = result.persistence_warning
        return JSONResponse(payload)

    # needs_deep
    return JSONResponse({"mode": "needs_deep", "reason": result.reason})


@router.get("/api/ai-status")
def api_ai_status(request: Request, account: str | None = None):
    """Report whether the fast ask-bar path is available for this account's owner."""
    member_id = _get_account_service(request).owner_for_account(account)
    return {"quick_path": _get_ask_service(request).ai_status(member_id)}


@router.get("/api/members/{member_id}/anthropic-key")
def api_get_member_anthropic_key(member_id: str, request: Request):
    """Never returns the raw key — only whether one is configured."""
    if not _get_account_service(request).member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    return {"configured": member_api_key_configured(member_id)}


@router.post("/api/members/{member_id}/anthropic-key")
def api_save_member_anthropic_key(member_id: str, body: MemberApiKey, request: Request):
    if not _get_account_service(request).member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    try:
        save_member_api_key(member_id, body.api_key)
    except AskError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    return {"configured": True}


@router.delete("/api/members/{member_id}/anthropic-key")
def api_clear_member_anthropic_key(member_id: str, request: Request):
    if not _get_account_service(request).member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    try:
        clear_member_api_key(member_id)
    except AskError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    return {"configured": False}
