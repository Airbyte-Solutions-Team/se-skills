"""Pydantic request/response models for hosted account/opportunity APIs."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

import config


class User(BaseModel):
    id: uuid.UUID
    email: str


class OrgContext(BaseModel):
    user: User
    org_id: uuid.UUID
    membership_id: uuid.UUID | None = None
    role: str = "member"


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    assigned_to: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 200:
            raise ValueError("Account name must be between 1 and 200 characters")
        if not config.SAFE_NAME.match(value) or ".." in value or "/" in value or "\\" in value:
            raise ValueError("Account name contains invalid characters")
        return value

    @field_validator("assigned_to")
    @classmethod
    def validate_assigned_to(cls, value: uuid.UUID | None) -> uuid.UUID | None:
        return value


class AccountOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str
    created_by: uuid.UUID | None
    assigned_to: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> "AccountOut":
        return cls(
            id=record["id"],
            org_id=record["org_id"],
            name=record["name"],
            slug=record["slug"],
            created_by=record.get("created_by"),
            assigned_to=record.get("assigned_to"),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )


class AccountList(BaseModel):
    accounts: list[AccountOut]


class OpportunityCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    assigned_to: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 200:
            raise ValueError("Opportunity name must be between 1 and 200 characters")
        if not config.SAFE_NAME.match(value) or ".." in value or "/" in value or "\\" in value:
            raise ValueError("Opportunity name contains invalid characters")
        return value


class OpportunityOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    slug: str
    stage: str | None
    close_date: datetime | None
    amount: int | None
    created_by: uuid.UUID | None
    assigned_to: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> "OpportunityOut":
        return cls(
            id=record["id"],
            org_id=record["org_id"],
            account_id=record["account_id"],
            name=record["name"],
            slug=record["slug"],
            stage=record.get("stage"),
            close_date=record.get("close_date"),
            amount=record.get("amount"),
            created_by=record.get("created_by"),
            assigned_to=record.get("assigned_to"),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )


class OpportunityList(BaseModel):
    opportunities: list[OpportunityOut]


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return s[:80] or "unnamed"
