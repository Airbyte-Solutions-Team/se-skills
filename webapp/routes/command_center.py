"""Local-only Command Center evidence routes (PR B: intake + unprocessed sources).

Every import here is user-triggered. Nothing on this router contacts Granola,
Salesforce, or any other provider during a request; association confirmation
resolves opportunity identity through the existing workspace service only.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from command_center_evidence import AssociationCandidate
from integrations.granola import MAX_NOTES_PER_IMPORT, GranolaImportError, GranolaSourceAdapter
from services.account_service import AccountError
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService


router = APIRouter()

_UNPROCESSED_STATUSES = (
    "discovered", "awaiting_association", "awaiting_content", "queued", "processing", "failed"
)


class GranolaImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(default="local-manual", pattern=r"^[A-Za-z0-9._-]{1,120}$")
    notes: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_NOTES_PER_IMPORT)


class ConfirmAssociationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=120)
    opportunity_slug: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=300)


class ProposeAssociationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=300)
    candidates: list[AssociationCandidate] = Field(min_length=1, max_length=20)


class ClearAssociationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=300)


def _ledger(request: Request) -> EvidenceLedgerService:
    return request.app.state.evidence_ledger_service


def _adapter(request: Request) -> GranolaSourceAdapter:
    return request.app.state.granola_adapter


def _raise_domain(exc: Exception) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/api/command-center/adapters")
async def api_command_center_adapters(request: Request) -> dict:
    return {"adapters": [_adapter(request).describe().model_dump(mode="json")]}


@router.get("/api/command-center/sources")
async def api_command_center_sources(
    request: Request,
    status: Literal[
        "discovered", "awaiting_association", "awaiting_content", "queued",
        "processing", "processed", "failed",
    ] | None = None,
) -> dict:
    try:
        return _ledger(request).list_sources(status=status)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.get("/api/command-center/sources/unprocessed")
async def api_command_center_unprocessed(request: Request) -> dict:
    try:
        payload = _ledger(request).unprocessed_sources()
    except EvidenceLedgerError as exc:
        _raise_domain(exc)
    payload["statuses"] = list(_UNPROCESSED_STATUSES)
    return payload


@router.get("/api/command-center/sources/{source_id}")
async def api_command_center_source(source_id: str, request: Request) -> dict:
    try:
        return _ledger(request).get_source(source_id)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.post("/api/command-center/imports/granola", status_code=201)
async def api_command_center_import_granola(body: GranolaImportBody, request: Request) -> dict:
    adapter = _adapter(request)
    meetings = []
    rejected: list[dict[str, Any]] = []
    for index, payload in enumerate(body.notes):
        try:
            meetings.append(adapter.normalize(payload, connection_id=body.connection_id))
        except GranolaImportError as exc:
            rejected.append({"index": index, "code": exc.code, "detail": exc.detail})
    if rejected and not meetings:
        raise HTTPException(status_code=400, detail={"rejected": rejected})
    try:
        result = _ledger(request).import_meetings(meetings)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)
    result["adapter"] = adapter.describe().model_dump(mode="json")
    result["rejected"] = rejected
    return result


@router.post("/api/command-center/sources/{source_id}/association/proposals")
async def api_command_center_propose(source_id: str, body: ProposeAssociationBody, request: Request) -> dict:
    try:
        return _ledger(request).propose_association(source_id, body.candidates, reason=body.reason)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.put("/api/command-center/sources/{source_id}/association")
async def api_command_center_confirm(source_id: str, body: ConfirmAssociationBody, request: Request) -> dict:
    workspace = request.app.state.opportunity_workspace_service
    try:
        return await _ledger(request).confirm_association(
            source_id,
            account=body.account,
            opportunity_slug=body.opportunity_slug,
            reason=body.reason,
            resolve_identity=workspace.resolve_identity,
        )
    except (AccountError, EvidenceLedgerError) as exc:
        _raise_domain(exc)


@router.delete("/api/command-center/sources/{source_id}/association")
async def api_command_center_clear(source_id: str, body: ClearAssociationBody, request: Request) -> dict:
    try:
        return _ledger(request).clear_association(source_id, reason=body.reason)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.post("/api/command-center/sources/{source_id}/retry")
async def api_command_center_retry(source_id: str, request: Request) -> dict:
    try:
        return _ledger(request).retry(source_id)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)
