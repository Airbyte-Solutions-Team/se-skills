"""Output HTTP routes for the SE Skills webapp."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from services.output_service import OutputError, OutputService

router = APIRouter()


def _get_output_service(request: Request) -> OutputService:
    return request.app.state.output_service


class OutputPdf(BaseModel):
    path: str = Field(max_length=500)
    append_md: str = Field(default="", max_length=20000)


class OutputRender(BaseModel):
    md: str = Field(max_length=2_000_000)


class OutputRenderResponse(BaseModel):
    html: str


class OutputDiff(BaseModel):
    left: str = Field(max_length=500)
    right: str = Field(max_length=500)


class OutputGolden(BaseModel):
    path: str = Field(max_length=500)
    scenario: str = Field(default="", max_length=100)
    text: str | None = Field(default=None, max_length=2_000_000)
    confirm_synthetic: bool = False


class PushToRepo(BaseModel):
    path: str = Field(..., max_length=500)
    account: str = Field(..., max_length=120)
    member: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2_000)
    meta: str = Field(default="", max_length=1_000)


@router.get("/api/output", response_class=PlainTextResponse)
def api_output_content(path: str, request: Request) -> str:
    try:
        return _get_output_service(request).read_output_content(path)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/output/meta")
def api_output_meta(path: str, request: Request) -> dict:
    try:
        return _get_output_service(request).read_output_meta(path)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/output/html", response_class=HTMLResponse)
def api_output_html(path: str, request: Request) -> str:
    try:
        return _get_output_service(request).read_output_html(path)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/output/repo-path")
def api_output_repo_path(account: str, request: Request, member: str = "") -> dict:
    try:
        return _get_output_service(request).repo_path(account, member)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/output/push-to-repo")
async def api_push_to_repo(body: PushToRepo, request: Request) -> dict:
    try:
        return await _get_output_service(request).push_to_repo(body)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/output/push-status")
async def api_push_status(account: str, request: Request) -> dict:
    try:
        return await _get_output_service(request).push_status(account)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/output/pdf")
def api_output_pdf(path: str, request: Request) -> Response:
    try:
        data, filename = _get_output_service(request).export_pdf(path)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/output/internal-html")
def api_output_internal_html(path: str, request: Request) -> Response:
    try:
        doc, filename = _get_output_service(request).export_internal_html(path)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(
        content=doc,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/output/pdf")
def api_output_pdf_post(body: OutputPdf, request: Request) -> Response:
    try:
        data, filename = _get_output_service(request).export_pdf(body.path, append_md=body.append_md)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/output/render")
def api_output_render(body: OutputRender) -> OutputRenderResponse:
    # This route is stateless; the service is not needed.
    from services.output_service import OutputService
    html = OutputService.render_markdown(body.md)
    return OutputRenderResponse(html=html)


@router.delete("/api/output")
def api_delete_output(path: str, request: Request) -> dict:
    try:
        return _get_output_service(request).delete_output(path)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/golden/manifests")
def api_golden_manifests(skill: str, request: Request) -> dict:
    return _get_output_service(request).golden_manifests(skill)


@router.post("/api/output/golden")
def api_output_golden_post(body: OutputGolden, request: Request) -> dict:
    try:
        return _get_output_service(request).promote_to_golden(body)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/output/diff")
def api_output_diff(body: OutputDiff, request: Request) -> dict:
    try:
        return _get_output_service(request).diff_outputs(body)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
