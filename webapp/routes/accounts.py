"""Member, account, and opportunity HTTP routes for the SE Skills webapp."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from services.account_service import AccountError, AccountService
from services.output_service import OutputError, OutputService

router = APIRouter()


def _get_account_service(request: Request) -> AccountService:
    return request.app.state.account_service


def _get_output_service(request: Request) -> OutputService:
    return request.app.state.output_service


class CreateMember(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None


class CreateAccount(BaseModel):
    name: str
    owner: str | None = None
    sfdc_name: str | None = None


class BulkCreateAccounts(BaseModel):
    accounts: list[CreateAccount]


class SetOwner(BaseModel):
    owner: str


class BulkBody(BaseModel):
    accounts: list[str]
    owner: str | None = None


@router.get("/api/members")
def api_members(request: Request) -> list[dict]:
    return _get_account_service(request).load_team()


@router.post("/api/members")
def api_create_member(body: CreateMember, request: Request) -> dict:
    try:
        return _get_account_service(request).create_member(body.name, body.role, body.email)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/members/{member_id}/accounts")
def api_member_accounts(member_id: str, request: Request) -> dict:
    try:
        return _get_account_service(request).member_accounts(member_id)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/accounts")
def api_create_account(body: CreateAccount, request: Request) -> dict:
    try:
        return _get_account_service(request).create_account(body.name, body.owner, body.sfdc_name)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/bulk-create-accounts")
def api_bulk_create_accounts(body: BulkCreateAccounts, request: Request) -> dict:
    svc = _get_account_service(request)
    results = []
    for acc in body.accounts:
        try:
            r = svc.create_account(acc.name, acc.owner, acc.sfdc_name)
            results.append({**r, "ok": True})
        except AccountError as e:
            results.append({"name": acc.name, "ok": False, "error": e.detail})
    return {
        "created": sum(1 for r in results if r.get("ok") and r.get("created")),
        "results": results,
    }


@router.get("/api/accounts/{account}")
def api_account(account: str, request: Request) -> dict:
    try:
        return _get_account_service(request).get_account(account)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/accounts/{account}/outputs")
def api_outputs(account: str, request: Request, opp: str | None = None) -> list[dict]:
    output_svc = _get_output_service(request)
    svc = _get_account_service(request)
    try:
        account = svc.safe_name(account)
        if opp:
            opp = svc.safe_name(opp)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    try:
        return output_svc.list_outputs(account, opp)
    except OutputError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/accounts/{account}/opportunities")
async def api_opportunities(account: str, request: Request) -> list[dict]:
    try:
        return await _get_account_service(request).list_opportunities(account)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/accounts/{account}/last-run")
def api_last_run(account: str, opp_slug: str | None = None, request: Request = None):
    """Return the most recent .runs/<name>.json for this account/opportunity."""
    svc = _get_account_service(request)
    output_svc = _get_output_service(request)
    try:
        safe_account = svc.safe_name(account)
        safe_opp = svc.safe_name(opp_slug) if opp_slug else None
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    rec = output_svc.latest_run(safe_account, safe_opp)
    if not rec:
        return Response(status_code=204)
    return rec


@router.post("/api/accounts/{account}/owner")
def api_set_owner(account: str, body: SetOwner, request: Request) -> dict:
    try:
        return _get_account_service(request).set_owner(account, body.owner)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/accounts/{account}/archive")
def api_archive(account: str, request: Request) -> dict:
    try:
        return _get_account_service(request).archive(account)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/accounts/{account}/unarchive")
def api_unarchive(account: str, request: Request) -> dict:
    try:
        return _get_account_service(request).unarchive(account)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/api/accounts/{account}")
def api_delete_account(account: str, request: Request) -> dict:
    try:
        return _get_account_service(request).delete_account(account)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/bulk/{action}")
def api_bulk(action: str, body: BulkBody, request: Request) -> dict:
    try:
        return _get_account_service(request).bulk_action(action, body.accounts, body.owner)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/trash")
def api_list_trash(request: Request) -> list[dict]:
    return _get_account_service(request).list_trash()


@router.post("/api/trash/{trash_id}/restore")
def api_restore(trash_id: str, request: Request) -> dict:
    try:
        return _get_account_service(request).restore_trash(trash_id)
    except AccountError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
