"""Pydantic request/response models for hosted account/opportunity APIs."""
from __future__ import annotations

import json
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
    context_token: str = ""


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


class TranscriptOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    account_id: uuid.UUID
    opportunity_id: uuid.UUID | None
    original_filename: str
    size_bytes: int
    mime_type: str
    uploaded_by: uuid.UUID
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> "TranscriptOut":
        return cls(
            id=record["id"],
            org_id=record["org_id"],
            account_id=record["account_id"],
            opportunity_id=record.get("opportunity_id"),
            original_filename=record["original_filename"],
            size_bytes=record["size_bytes"],
            mime_type=record["mime_type"],
            uploaded_by=record["uploaded_by"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )


class TranscriptList(BaseModel):
    transcripts: list[TranscriptOut]


class JobAttemptOut(BaseModel):
    id: uuid.UUID
    attempt_number: int
    worker_id: str | None
    runtime_version: str | None
    model: str | None
    started_at: datetime
    finished_at: datetime | None
    heartbeat_at: datetime | None
    outcome: str | None
    error_category: str | None
    error: str | None
    token_usage: dict[str, Any] | None
    cost: float | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> "JobAttemptOut":
        def _json(value: Any) -> Any:
            if isinstance(value, str):
                return json.loads(value)
            return value or {}

        return cls(
            id=record["id"],
            attempt_number=record["attempt_number"],
            worker_id=record.get("worker_id"),
            runtime_version=record.get("runtime_version"),
            model=record.get("model"),
            started_at=record["started_at"],
            finished_at=record.get("finished_at"),
            heartbeat_at=record.get("heartbeat_at"),
            outcome=record.get("outcome"),
            error_category=record.get("error_category"),
            error=record.get("error"),
            token_usage=_json(record.get("token_usage")),
            cost=float(record["cost"]) if record.get("cost") is not None else None,
            created_at=record["created_at"],
        )


class JobOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    account_id: uuid.UUID
    transcript_id: uuid.UUID
    opportunity_id: uuid.UUID | None
    requester_id: uuid.UUID
    skill: str
    skill_version: str
    model: str | None
    runtime_version: str | None
    worker_id: str | None
    status: str
    payload: dict[str, Any]
    input_refs: dict[str, Any]
    source_manifest: dict[str, Any]
    result_output_id: uuid.UUID | None
    validation_status: str
    token_usage: dict[str, Any] | None
    cost: float | None
    attempts: int
    max_attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    timeout_at: datetime | None
    cancelled_at: datetime | None
    cancelled_by: uuid.UUID | None
    cancel_requested_at: datetime | None
    next_attempt_after: datetime | None
    dead_lettered: bool
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> "JobOut":
        def _json(value: Any) -> Any:
            if isinstance(value, str):
                return json.loads(value)
            return value or {}

        return cls(
            id=record["id"],
            org_id=record["org_id"],
            account_id=record["account_id"],
            transcript_id=record["transcript_id"],
            opportunity_id=record.get("opportunity_id"),
            requester_id=record["requester_id"],
            skill=record["skill"],
            skill_version=record["skill_version"],
            model=record.get("model"),
            runtime_version=record.get("runtime_version"),
            worker_id=record.get("worker_id"),
            status=record["status"],
            payload=_json(record.get("payload")),
            input_refs=_json(record.get("input_refs")),
            source_manifest=_json(record.get("source_manifest")),
            result_output_id=record.get("result_output_id"),
            validation_status=record.get("validation_status") or "unvalidated",
            token_usage=_json(record.get("token_usage")),
            cost=float(record["cost"]) if record.get("cost") is not None else None,
            attempts=record["attempts"],
            max_attempts=record["max_attempts"],
            started_at=record.get("started_at"),
            finished_at=record.get("finished_at"),
            timeout_at=record.get("timeout_at"),
            cancelled_at=record.get("cancelled_at"),
            cancelled_by=record.get("cancelled_by"),
            cancel_requested_at=record.get("cancel_requested_at"),
            next_attempt_after=record.get("next_attempt_after"),
            dead_lettered=record.get("dead_lettered") or False,
            error=record.get("error"),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )


class JobList(BaseModel):
    jobs: list[JobOut]


class JobDetail(BaseModel):
    job: JobOut
    attempts: list[JobAttemptOut]


class JobCreate(BaseModel):
    account_id: uuid.UUID
    transcript_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None
    skill: str = "post-call"
    skill_version: str = "1.0"
    model: str = "echo"
    runtime_version: str = "slice4"
    max_attempts: int = Field(default=3, ge=1, le=10)
    idempotency_key: str | None = None

    @field_validator("skill", "skill_version", "model", "runtime_version")
    @classmethod
    def _strip_strings(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value or len(value) > 200:
                raise ValueError("idempotency_key must be between 1 and 200 characters")
        return value


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return s[:80] or "unnamed"
