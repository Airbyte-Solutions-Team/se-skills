"""Salesforce integration HTTP routes for the SE Skills webapp."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from integrations.salesforce import SalesforceIntegration
from services.account_service import AccountError, AccountService

router = APIRouter()


def _get_account_service(request: Request) -> AccountService:
    return request.app.state.account_service


def _get_salesforce(request: Request) -> SalesforceIntegration:
    return request.app.state.salesforce_integration


class SelectedAes(BaseModel):
    selected: list[str] = []


class PullAccounts(BaseModel):
    aes: list[str] = []


@router.post("/api/sfdc/stage-amount")
async def api_sfdc_stage_amount(body: dict, request: Request):
    """Batched SFDC stage+amount for a list of account names. Best-effort; the UI
    calls this after rendering cards and fills in the line if it resolves."""
    names = body.get("accounts", [])
    if not isinstance(names, list):
        raise HTTPException(400, "accounts must be a list")

    svc = _get_account_service(request)
    safe_names = []
    for n in names:
        if not isinstance(n, str):
            continue
        try:
            safe_names.append(svc.safe_name(n))
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    salesforce = _get_salesforce(request)
    return await salesforce.stage_and_amount_for_accounts(safe_names)


@router.get("/api/members/{member_id}/sfdc-aes")
async def api_sfdc_aes(member_id: str, request: Request):
    """AE names (from live SFDC) selectable for this member's account pull,
    plus the member's previously-saved selection."""
    account_svc = _get_account_service(request)
    if not account_svc.member_by_id(member_id):
        raise HTTPException(404, "Unknown member")

    aes = await _get_salesforce(request).list_account_executives()
    selected = account_svc.read_member_prefs(member_id).get("selected_aes", [])
    return {"aes": aes, "selected": selected}


@router.post("/api/members/{member_id}/sfdc-aes")
def api_save_sfdc_aes(member_id: str, body: SelectedAes, request: Request):
    """Persist which AEs this member wants to pull accounts for."""
    account_svc = _get_account_service(request)
    if not account_svc.member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    prefs = account_svc.read_member_prefs(member_id)
    prefs["selected_aes"] = [a for a in body.selected if isinstance(a, str) and a.strip()]
    account_svc.save_member_prefs(member_id, prefs)
    return {"ok": True, "selected": prefs["selected_aes"]}


@router.post("/api/members/{member_id}/sfdc-accounts")
async def api_sfdc_accounts(member_id: str, body: PullAccounts, request: Request):
    """Open, future-dated opps for this member, split new_business / renewals."""
    account_svc = _get_account_service(request)
    member = account_svc.member_by_id(member_id)
    if not member:
        raise HTTPException(404, "Unknown member")

    aes = [a for a in body.aes if isinstance(a, str) and a.strip()]
    return await _get_salesforce(request).accounts_for_member(member, aes)
