"""Ask HTTP routes for the SE Skills webapp."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.ask_service import AskError, AskResult, AskService

router = APIRouter()


class OutputAsk(BaseModel):
    path: str = Field(max_length=500)             # output file, relative to CUSTOMERS_DIR
    question: str = Field(max_length=5_000)
    account: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)


def _get_ask_service(request: Request) -> AskService:
    return request.app.state.ask_service


@router.post("/api/output/ask")
async def api_output_ask(body: OutputAsk, request: Request):
    """Follow-up Q&A against an opened output doc. Quick → Claude API (doc as
    context); deep (codebase/connectors) → claude -p. Mirrors the live ask."""
    try:
        result = await _get_ask_service(request).output_ask(
            path=body.path,
            question=body.question,
            account=body.account,
            opportunity=body.opportunity,
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
def api_ai_status(request: Request):
    """Report whether the fast ask-bar path is available."""
    return {"quick_path": _get_ask_service(request).ai_status()}
