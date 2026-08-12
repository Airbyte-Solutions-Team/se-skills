"""Organization-scoped account and opportunity API routes."""
from __future__ import annotations

import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from . import models
from .auth import require_org, tenant_connection
from .models import AccountCreate, AccountList, AccountOut, OpportunityCreate, OpportunityList, OpportunityOut, OrgContext

router = APIRouter(prefix="/api/hosted", tags=["hosted"])


async def _require_assigned_in_org(
    conn: asyncpg.Connection, org_id: uuid.UUID, assigned_to: uuid.UUID | None
) -> None:
    """Verify that an assigned_to user belongs to the active organization."""
    if assigned_to is None:
        return
    row = await conn.fetchrow(
        "SELECT public.is_active_org_member($1, $2) AS ok",
        assigned_to,
        org_id,
    )
    if row is None or not row["ok"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assigned_to user is not an active member of this organization",
        )


async def _ensure_account_slug(
    conn: asyncpg.Connection, org_id: uuid.UUID, slug: str, suffix: int = 0
) -> str:
    """Deduplicate account slugs within an organization."""
    candidate = f"{slug}-{suffix}" if suffix else slug
    existing = await conn.fetchval(
        "SELECT 1 FROM public.accounts WHERE org_id = $1 AND slug = $2",
        org_id,
        candidate,
    )
    if existing is None:
        return candidate
    return await _ensure_account_slug(conn, org_id, slug, suffix + 1)


async def _ensure_opportunity_slug(
    conn: asyncpg.Connection, account_id: uuid.UUID, slug: str, suffix: int = 0
) -> str:
    """Deduplicate opportunity slugs within a single account."""
    candidate = f"{slug}-{suffix}" if suffix else slug
    existing = await conn.fetchval(
        "SELECT 1 FROM public.opportunities WHERE account_id = $1 AND slug = $2",
        account_id,
        candidate,
    )
    if existing is None:
        return candidate
    return await _ensure_opportunity_slug(conn, account_id, slug, suffix + 1)


@router.get("/accounts", response_model=AccountList)
async def list_accounts(
    request: Request,
    org: Annotated[OrgContext, Depends(require_org)],
) -> AccountList:
    async with tenant_connection(request, org) as conn:
        rows = await conn.fetch(
            "SELECT * FROM public.accounts WHERE org_id = $1 ORDER BY name",
            org.org_id,
        )
    return AccountList(accounts=[AccountOut.from_record(r) for r in rows])


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    request: Request,
    data: AccountCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> AccountOut:
    slug = models.slugify(data.name)
    async with tenant_connection(request, org) as conn:
        await _require_assigned_in_org(conn, org.org_id, data.assigned_to)
        slug = await _ensure_account_slug(conn, org.org_id, slug)
        row = await conn.fetchrow(
            """
            INSERT INTO public.accounts (org_id, name, slug, created_by, assigned_to)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            org.org_id,
            data.name,
            slug,
            org.user.id,
            data.assigned_to,
        )
    return AccountOut.from_record(row)


@router.get("/accounts/{account_id}/opportunities", response_model=OpportunityList)
async def list_opportunities(
    request: Request,
    account_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require_org)],
) -> OpportunityList:
    async with tenant_connection(request, org) as conn:
        rows = await conn.fetch(
            "SELECT * FROM public.opportunities WHERE org_id = $1 AND account_id = $2 ORDER BY name",
            org.org_id,
            account_id,
        )
    return OpportunityList(opportunities=[OpportunityOut.from_record(r) for r in rows])


@router.post(
    "/accounts/{account_id}/opportunities",
    response_model=OpportunityOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_opportunity(
    request: Request,
    account_id: uuid.UUID,
    data: OpportunityCreate,
    org: Annotated[OrgContext, Depends(require_org)],
) -> OpportunityOut:
    slug = models.slugify(data.name)
    async with tenant_connection(request, org) as conn:
        await _require_assigned_in_org(conn, org.org_id, data.assigned_to)
        # Verify the account belongs to the active org. This also acts as a
        # second guard before the composite FK enforces the same org at the DB.
        account = await conn.fetchrow(
            "SELECT id FROM public.accounts WHERE id = $1 AND org_id = $2",
            account_id,
            org.org_id,
        )
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Account not found",
            )
        slug = await _ensure_opportunity_slug(conn, account_id, slug)
        row = await conn.fetchrow(
            """
            INSERT INTO public.opportunities (org_id, account_id, name, slug, created_by, assigned_to)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            org.org_id,
            account_id,
            data.name,
            slug,
            org.user.id,
            data.assigned_to,
        )
    return OpportunityOut.from_record(row)
