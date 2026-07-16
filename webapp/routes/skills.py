"""Skill discovery, help, planning, permission, and invocation HTTP routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.skill_runtime_service import SkillRuntimeError, SkillRuntimeService

router = APIRouter()


class InvokeBody(BaseModel):
    account: str = Field(max_length=120)
    skill: str | None = Field(default=None, max_length=80)
    opportunity: str | None = Field(default=None, max_length=200)
    opp_slug: str | None = Field(default=None, max_length=120)
    extra: str | None = Field(default=None, max_length=10_000)
    freeform: str | None = Field(default=None, max_length=20_000)
    override_prerequisites: bool = Field(default=False)
    approve_permissions: bool = Field(default=False)


def _get_skill_runtime_service(request: Request) -> SkillRuntimeService:
    return request.app.state.skill_runtime_service


@router.get("/api/skills")
def api_skills(request: Request) -> list[dict]:
    return _get_skill_runtime_service(request).skills


@router.post("/api/reload")
def api_reload_skills(request: Request) -> dict:
    return _get_skill_runtime_service(request).reload()


@router.get("/api/skills/help")
def api_skills_help(request: Request) -> list[dict]:
    return _get_skill_runtime_service(request).help()


@router.get("/api/plan")
def api_plan(account: str, skill: str, opp_slug: str | None = None, request: Request = None):
    """Return the prerequisite plan for a proposed skill invocation."""
    try:
        return _get_skill_runtime_service(request).plan(skill, account, opp_slug)
    except SkillRuntimeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


@router.post("/api/invoke")
async def api_invoke(body: InvokeBody, request: Request):
    """Launch a skill as a background job and return a job_id."""
    svc = _get_skill_runtime_service(request)
    try:
        result = await svc.invoke(
            account=body.account,
            skill=body.skill,
            opportunity=body.opportunity,
            opp_slug=body.opp_slug,
            extra=body.extra,
            freeform=body.freeform,
            override_prerequisites=body.override_prerequisites,
            approve_permissions=body.approve_permissions,
        )
    except SkillRuntimeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    return JSONResponse(result)


@router.get("/api/permissions")
def api_permissions(skill: str | None = None, freeform: bool = False, request: Request = None):
    """Return the permission profile for a proposed skill invocation."""
    try:
        return _get_skill_runtime_service(request).permission_for(skill, freeform=freeform)
    except SkillRuntimeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
