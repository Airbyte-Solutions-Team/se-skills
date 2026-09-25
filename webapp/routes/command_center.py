"""Local-only Command Center routes (PR B intake + PR C reconciliation/actions).

Every import and every reconciliation here is user-triggered. Nothing on this
router contacts Granola, Salesforce, or any other provider during a request;
association confirmation resolves opportunity identity through the existing
workspace service only, and reconciliation runs the existing local overview
runtime as a background job whose status is polled.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from command_center_evidence import AssociationCandidate
from command_center_operations import Actor, ChangeType, DurableActionStatus, ResponsibleParty
from integrations.granola import MAX_NOTES_PER_IMPORT, GranolaImportError, GranolaSourceAdapter
from services.account_service import AccountError
from services.command_center_operations_service import (
    RECONCILE_JOB_KIND,
    CommandCenterOperationsError,
    CommandCenterOperationsService,
)
from services.command_center_read_service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    CommandCenterReadError,
    CommandCenterReadService,
)
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService
from services.gmail_intake_service import (
    MAX_QUERY_TERMS,
    GmailIntakeError,
    GmailIntakeService,
)
from services.gmail_intake_service import MAX_SELECTION as GMAIL_MAX_SELECTION
from services.granola_retrieval_service import (
    MAX_SELECTION,
    GranolaRetrievalError,
    GranolaRetrievalService,
    TimeRange,
)


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


class ReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    base_revision: int = Field(ge=1, le=1_000_000)


class ActionTransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_status: DurableActionStatus
    reason: str = Field(min_length=1, max_length=300)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    due_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    clear_due_date: bool = False


class ActionUndoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=300)


def _ledger(request: Request) -> EvidenceLedgerService:
    return request.app.state.evidence_ledger_service


def _operations(request: Request) -> CommandCenterOperationsService:
    return request.app.state.command_center_operations_service


def _adapter(request: Request) -> GranolaSourceAdapter:
    return request.app.state.granola_adapter


def _reads(request: Request) -> CommandCenterReadService:
    return request.app.state.command_center_read_service


def _safe_token_query() -> Any:
    return Query(default=None, pattern=r"^[A-Za-z0-9._-]{1,120}$")


def _raise_domain(exc: Exception) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _retrieval(request: Request) -> GranolaRetrievalService:
    return request.app.state.granola_retrieval_service


def _raise_retrieval(exc: GranolaRetrievalError) -> None:
    raise HTTPException(
        status_code=exc.status, detail={"code": exc.code, "message": f"{exc.detail} [{exc.code}]"}
    ) from exc


def _gmail(request: Request) -> GmailIntakeService:
    service = getattr(request.app.state, "gmail_intake_service", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail={"code": "gmail_not_configured", "message": "Gmail intake is not configured."}
        )
    return service


def _raise_gmail(exc: GmailIntakeError) -> None:
    raise HTTPException(
        status_code=exc.status, detail={"code": exc.code, "message": f"{exc.detail} [{exc.code}]"}
    ) from exc


@router.get("/api/command-center/adapters")
async def api_command_center_adapters(request: Request) -> dict:
    adapters = [
        _adapter(request).describe().model_dump(mode="json"),
        _retrieval(request).describe(),
    ]
    gmail = getattr(request.app.state, "gmail_intake_service", None)
    if gmail is not None:
        adapters.append(gmail.describe())
    return {"adapters": adapters}


# ---------------------------------------------------------------------------
# User-triggered Granola retrieval. Each POST below performs exactly the
# provider calls the user asked for, through their own Claude Code MCP
# connection; GETs read persisted state only. Responses are metadata and
# outcome codes; no meeting text or tool output is ever returned or logged.
# ---------------------------------------------------------------------------


class GranolaConnectionCheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GranolaListBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_range: TimeRange = "this_week"
    custom_start: date | None = None
    custom_end: date | None = None
    workspace_only: bool = False


class GranolaRetrievalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meeting_ids: list[str] = Field(min_length=1, max_length=MAX_SELECTION)


@router.get("/api/command-center/granola/connection")
async def api_granola_connection(request: Request) -> dict:
    return _retrieval(request).connection()


@router.post("/api/command-center/granola/connection/check")
async def api_granola_connection_check(body: GranolaConnectionCheckBody, request: Request) -> dict:
    try:
        return await _retrieval(request).check_connection()
    except GranolaRetrievalError as exc:
        _raise_retrieval(exc)


@router.post("/api/command-center/granola/meetings/list")
async def api_granola_list_meetings(body: GranolaListBody, request: Request) -> dict:
    try:
        return await _retrieval(request).list_meetings(
            time_range=body.time_range,
            custom_start=body.custom_start,
            custom_end=body.custom_end,
            workspace_only=body.workspace_only,
        )
    except GranolaRetrievalError as exc:
        _raise_retrieval(exc)


@router.post("/api/command-center/granola/retrievals", status_code=202)
async def api_granola_start_retrieval(body: GranolaRetrievalBody, request: Request) -> dict:
    try:
        return await _retrieval(request).start_retrieval(body.meeting_ids)
    except GranolaRetrievalError as exc:
        _raise_retrieval(exc)


@router.get("/api/command-center/granola/retrievals/{job_id}")
async def api_granola_retrieval(job_id: str, request: Request) -> dict:
    job = _retrieval(request).job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown retrieval job")
    return job


# ---------------------------------------------------------------------------
# PR E: user-triggered, read-only Gmail intake. Same shape as Granola: an
# explicit access check, a bounded metadata-only thread listing, and a
# retrieval of exactly the message ids the user selected. No route returns,
# stores in job metadata, or logs a message body. Whether a live transport is
# wired is reported by `connection` (`transport.live_retrieval_available`).
# ---------------------------------------------------------------------------


class GmailAccessCheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GmailListBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_range: TimeRange = "last_30_days"
    custom_start: date | None = None
    custom_end: date | None = None
    participants: list[str] = Field(default_factory=list, max_length=MAX_QUERY_TERMS)


class GmailRetrievalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ids: list[str] = Field(min_length=1, max_length=GMAIL_MAX_SELECTION)


@router.get("/api/command-center/gmail/connection")
async def api_gmail_connection(request: Request) -> dict:
    return _gmail(request).connection()


@router.post("/api/command-center/gmail/connection/check")
async def api_gmail_connection_check(body: GmailAccessCheckBody, request: Request) -> dict:
    try:
        return await _gmail(request).check_access()
    except GmailIntakeError as exc:
        _raise_gmail(exc)


@router.post("/api/command-center/gmail/connection/revoke")
async def api_gmail_connection_revoke(body: GmailAccessCheckBody, request: Request) -> dict:
    try:
        return _gmail(request).revoke_access()
    except (GmailIntakeError,) as exc:
        _raise_gmail(exc)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.post("/api/command-center/gmail/threads/list")
async def api_gmail_list_threads(body: GmailListBody, request: Request) -> dict:
    try:
        return await _gmail(request).list_threads(
            time_range=body.time_range,
            custom_start=body.custom_start,
            custom_end=body.custom_end,
            participants=body.participants,
        )
    except GmailIntakeError as exc:
        _raise_gmail(exc)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.post("/api/command-center/gmail/retrievals", status_code=202)
async def api_gmail_start_retrieval(body: GmailRetrievalBody, request: Request) -> dict:
    try:
        return await _gmail(request).start_retrieval(body.message_ids)
    except GmailIntakeError as exc:
        _raise_gmail(exc)


@router.get("/api/command-center/gmail/retrievals/{job_id}")
async def api_gmail_retrieval(job_id: str, request: Request) -> dict:
    job = _gmail(request).job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown retrieval job")
    return job


# ---------------------------------------------------------------------------
# PR D aggregate reads: persisted local state only, bounded and paginated.
# ---------------------------------------------------------------------------


@router.get("/api/command-center/today")
async def api_command_center_today(
    request: Request,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return _reads(request).today(limit=limit, offset=offset)
    except (CommandCenterReadError, CommandCenterOperationsError, EvidenceLedgerError) as exc:
        _raise_domain(exc)


@router.get("/api/command-center/portfolio")
async def api_command_center_portfolio(
    request: Request,
    account: str | None = _safe_token_query(),
    attention_only: bool = False,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return _reads(request).portfolio(account=account, attention_only=attention_only, limit=limit, offset=offset)
    except (CommandCenterReadError, CommandCenterOperationsError, EvidenceLedgerError) as exc:
        _raise_domain(exc)


@router.get("/api/command-center/opportunities")
async def api_command_center_opportunities(request: Request) -> dict:
    """Locally known opportunities (association targets); never consults Salesforce."""
    try:
        items = _reads(request).local_opportunities()
    except (CommandCenterReadError, CommandCenterOperationsError, EvidenceLedgerError) as exc:
        _raise_domain(exc)
    return {"total": len(items), "opportunities": items}


@router.get("/api/command-center/actions")
async def api_command_center_all_actions(
    request: Request,
    account: str | None = _safe_token_query(),
    opportunity_slug: str | None = _safe_token_query(),
    status: DurableActionStatus | None = None,
    party: ResponsibleParty | None = None,
    overdue: bool | None = None,
    include_retracted: bool = False,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return _reads(request).actions(
            account=account, opportunity_slug=opportunity_slug, status=status, party=party, overdue=overdue,
            include_retracted=include_retracted, limit=limit, offset=offset,
        )
    except (CommandCenterReadError, CommandCenterOperationsError, EvidenceLedgerError) as exc:
        _raise_domain(exc)


@router.get("/api/command-center/changes")
async def api_command_center_all_changes(
    request: Request,
    account: str | None = _safe_token_query(),
    opportunity_slug: str | None = _safe_token_query(),
    change_type: ChangeType | None = None,
    actor: Actor | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return _reads(request).changes(
            account=account, opportunity_slug=opportunity_slug, change_type=change_type, actor=actor,
            limit=limit, offset=offset,
        )
    except (CommandCenterReadError, CommandCenterOperationsError, EvidenceLedgerError) as exc:
        _raise_domain(exc)


@router.get("/api/command-center/sources/{source_id}/review")
async def api_command_center_source_review(source_id: str, request: Request) -> dict:
    try:
        return _reads(request).source_review(source_id)
    except (CommandCenterReadError, CommandCenterOperationsError, EvidenceLedgerError) as exc:
        _raise_domain(exc)


@router.get("/api/command-center/sources")
async def api_command_center_sources(
    request: Request,
    status: Literal[
        "discovered", "awaiting_association", "awaiting_content", "queued",
        "processing", "processed", "failed",
    ] | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return _ledger(request).list_sources(status=status, limit=limit, offset=offset)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)


@router.get("/api/command-center/sources/unprocessed")
async def api_command_center_unprocessed(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        payload = _ledger(request).unprocessed_sources(limit=limit, offset=offset)
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
        return await _operations(request).confirm_association(
            source_id,
            account=body.account,
            opportunity_slug=body.opportunity_slug,
            reason=body.reason,
            resolve_identity=workspace.resolve_identity,
        )
    except (AccountError, EvidenceLedgerError, CommandCenterOperationsError) as exc:
        _raise_domain(exc)


@router.delete("/api/command-center/sources/{source_id}/association")
async def api_command_center_clear(source_id: str, body: ClearAssociationBody, request: Request) -> dict:
    try:
        return _operations(request).clear_association(source_id, reason=body.reason)
    except (EvidenceLedgerError, CommandCenterOperationsError) as exc:
        _raise_domain(exc)


@router.post("/api/command-center/sources/{source_id}/reconcile", status_code=202)
async def api_command_center_reconcile(source_id: str, body: ReconcileBody, request: Request) -> dict:
    try:
        return await _operations(request).start_reconciliation(
            source_id, base_version_id=body.base_version_id, base_revision=body.base_revision
        )
    except CommandCenterOperationsError as exc:
        _raise_domain(exc)


@router.get("/api/command-center/reconciliations/{job_id}")
async def api_command_center_reconciliation(job_id: str, request: Request) -> dict:
    job = request.app.state.job_service.get_job(job_id)
    if job is None or job.get("kind") != RECONCILE_JOB_KIND:
        raise HTTPException(status_code=404, detail="Unknown reconciliation job")
    return {"job_id": job_id, **{key: value for key, value in job.items() if key != "sig"}}


@router.get("/api/command-center/sources/{source_id}/runs")
async def api_command_center_runs(source_id: str, request: Request) -> dict:
    try:
        return {"source_id": source_id, "runs": _operations(request).list_runs(source_id)}
    except CommandCenterOperationsError as exc:
        _raise_domain(exc)


@router.get("/api/command-center/opportunities/{account}/{opp_slug}/actions")
async def api_command_center_actions(
    account: str,
    opp_slug: str,
    request: Request,
    status: DurableActionStatus | None = None,
    include_retracted: bool = False,
) -> dict:
    try:
        return _operations(request).list_actions(account, opp_slug, status=status, include_retracted=include_retracted)
    except CommandCenterOperationsError as exc:
        _raise_domain(exc)


@router.get("/api/command-center/opportunities/{account}/{opp_slug}/changes")
async def api_command_center_changes(
    account: str,
    opp_slug: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return _operations(request).list_changes(account, opp_slug, limit=limit, offset=offset)
    except CommandCenterOperationsError as exc:
        _raise_domain(exc)


@router.get("/api/command-center/actions/{action_id}")
async def api_command_center_action(action_id: str, request: Request) -> dict:
    """Action detail: the PR C record plus the derived list-row fields (overdue, links, provenance, allowed transitions)."""
    try:
        return _reads(request).action(action_id)
    except (CommandCenterReadError, CommandCenterOperationsError) as exc:
        _raise_domain(exc)


@router.post("/api/command-center/actions/{action_id}/transitions")
async def api_command_center_action_transition(action_id: str, body: ActionTransitionBody, request: Request) -> dict:
    try:
        return _operations(request).transition_action(
            action_id,
            to_status=body.to_status,
            reason=body.reason,
            owner=body.owner,
            due_date=body.due_date,
            clear_due_date=body.clear_due_date,
        )
    except CommandCenterOperationsError as exc:
        _raise_domain(exc)


@router.post("/api/command-center/actions/{action_id}/undo")
async def api_command_center_action_undo(action_id: str, body: ActionUndoBody, request: Request) -> dict:
    try:
        return _operations(request).undo_last_transition(action_id, reason=body.reason)
    except CommandCenterOperationsError as exc:
        _raise_domain(exc)


@router.post("/api/command-center/sources/{source_id}/retry")
async def api_command_center_retry(source_id: str, request: Request) -> dict:
    try:
        return _ledger(request).retry(source_id)
    except EvidenceLedgerError as exc:
        _raise_domain(exc)
