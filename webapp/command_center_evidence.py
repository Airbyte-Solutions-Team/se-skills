"""Typed contract for the Command Center evidence ledger (local single-user pilot).

The ledger records *where evidence came from* and *what state its processing is
in*. It never stores raw transcript or note bodies in list responses, audit
metadata, or logs; bodies live only in the private content store managed by
`services.evidence_ledger_service`. Every record carries the scope identity of
the local workspace that owns it, and every content revision is immutable.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1

ScopeKind = Literal["local_workspace"]
Provider = Literal["granola", "manual"]
SourceKind = Literal["meeting", "manual_transcript", "crm_snapshot", "email_message"]
ContentAvailability = Literal[
    "metadata_only", "content_available", "pending_unknown", "access_lost", "deleted", "failed"
]
# Once the latest revision is in one of these states the service withholds every
# earlier cached revision as well, until a retention/re-authorization decision
# is recorded (spec §4). `pending_unknown` covers a provider 404, which Granola
# documents for notes that are still processing as well as for deleted notes.
WITHHOLD_CONTENT_AVAILABILITY: frozenset[str] = frozenset({"pending_unknown", "access_lost", "deleted"})
ProcessingStatus = Literal[
    "discovered",
    "awaiting_association",
    "awaiting_content",
    "queued",
    "processing",
    "processed",
    "failed",
    "superseded",
]
AssociationMethod = Literal[
    "explicit", "verified_external_id", "thread_mapping", "contact", "domain", "title", "attendee", "manual"
]
AssociationState = Literal["unassociated", "proposed", "associated"]
ImportTrigger = Literal["manual_import"]

# Methods that may set `associated` without a human decision. Everything else
# is a proposal for review (spec §5.4). The pilot has no verified external
# opportunity ID path yet, so only persisted human confirmations qualify.
AUTO_ASSOCIATION_METHODS: frozenset[str] = frozenset({"explicit", "verified_external_id"})
HUMAN_ASSOCIATION_METHODS: frozenset[str] = frozenset({"explicit", "manual"})

HEX16 = re.compile(r"^[a-f0-9]{16}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
SOURCE_ID = re.compile(r"^src_[a-f0-9]{32}$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

ShortText = Field(min_length=1, max_length=300)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScopeIdentity(_Strict):
    """Identity of the isolated local workspace that owns a record."""

    kind: ScopeKind = "local_workspace"
    workspace_id: str = Field(pattern=HEX16.pattern)


class SourceIdentity(_Strict):
    """Provider-side identity of one source object within a scope."""

    provider: Provider
    connection_id: str = Field(pattern=SAFE_TOKEN.pattern)
    source_workspace_id: str | None = Field(default=None, max_length=120)
    provider_object_id: str = Field(min_length=1, max_length=120)
    kind: SourceKind


class RevisionMetrics(_Strict):
    """Bounded, body-free description of one content revision."""

    title_present: bool
    attendee_count: int = Field(ge=0, le=1000)
    summary_chars: int = Field(ge=0)
    private_notes_chars: int = Field(ge=0)
    transcript_segments: int = Field(ge=0)
    transcript_chars: int = Field(ge=0)


class SourceRevision(_Strict):
    """One immutable observation of a source object's content."""

    revision: int = Field(ge=1, le=1_000_000)
    # Hash of the private snapshot (metadata + content); the dedup key.
    content_hash: str = Field(pattern=HEX64.pattern)
    # Hash of the body only, so metadata-only edits are distinguishable.
    body_hash: str = Field(pattern=HEX64.pattern)
    change: Literal["initial", "content", "metadata", "availability"] = "initial"
    availability: ContentAvailability
    trigger: ImportTrigger
    import_id: str = Field(pattern=HEX16.pattern)
    occurred_at: datetime | None = None
    provider_updated_at: datetime | None = None
    observed_at: datetime
    retrieved_at: datetime
    metrics: RevisionMetrics
    unavailable_reason: str | None = Field(default=None, max_length=300)


class AssociationCandidate(_Strict):
    account: str = Field(pattern=SAFE_TOKEN.pattern)
    opportunity_slug: str | None = Field(default=None, pattern=SAFE_TOKEN.pattern)
    method: AssociationMethod
    reason: str = ShortText


class AssociationDecision(_Strict):
    """One entry in the append-only association history."""

    sequence: int = Field(ge=1)
    state: AssociationState
    account: str | None = Field(default=None, pattern=SAFE_TOKEN.pattern)
    opportunity_slug: str | None = Field(default=None, pattern=SAFE_TOKEN.pattern)
    method: AssociationMethod | None = None
    actor: str = Field(pattern=SAFE_TOKEN.pattern)
    reason: str = ShortText
    candidates: list[AssociationCandidate] = Field(default_factory=list, max_length=20)
    crm_account_id: str | None = Field(default=None, max_length=40)
    crm_opportunity_id: str | None = Field(default=None, max_length=40)
    supersedes_sequence: int | None = Field(default=None, ge=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> "AssociationDecision":
        if self.state == "associated":
            if not (self.account and self.opportunity_slug and self.method):
                raise ValueError("an associated decision needs account, opportunity_slug, and method")
            if self.method not in AUTO_ASSOCIATION_METHODS and self.method not in HUMAN_ASSOCIATION_METHODS:
                raise ValueError(f"method {self.method!r} may only propose, never associate")
        elif self.state == "proposed":
            if not self.candidates:
                raise ValueError("a proposal needs at least one candidate")
            if self.account or self.opportunity_slug:
                raise ValueError("a proposal does not set an effective association")
        else:
            if self.account or self.opportunity_slug or self.method:
                raise ValueError("an unassociated decision carries no target")
        return self


class ProcessingState(_Strict):
    status: ProcessingStatus
    attempts: int = Field(default=0, ge=0, le=1000)
    retry_eligible: bool = False
    last_error_code: str | None = Field(default=None, max_length=80)
    processed_revision: int | None = Field(default=None, ge=1)
    updated_at: datetime


class EvidenceSource(_Strict):
    """Mutable-by-append source record; revisions and history only grow."""

    schema_version: Literal[1] = SCHEMA_VERSION
    source_id: str = Field(pattern=SOURCE_ID.pattern)
    scope: ScopeIdentity
    identity: SourceIdentity
    created_at: datetime
    updated_at: datetime
    latest_revision: int = Field(ge=1, le=1_000_000)
    availability: ContentAvailability
    processing: ProcessingState
    association: AssociationDecision
    association_history: list[AssociationDecision] = Field(min_length=1, max_length=500)
    revisions: list[SourceRevision] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _chain(self) -> "EvidenceSource":
        for index, revision in enumerate(self.revisions, start=1):
            if revision.revision != index:
                raise ValueError("revision chain has a gap or is out of order")
        if self.revisions[-1].revision != self.latest_revision:
            raise ValueError("latest_revision does not match the revision chain")
        for index, decision in enumerate(self.association_history, start=1):
            if decision.sequence != index:
                raise ValueError("association history sequence is inconsistent")
        if self.association != self.association_history[-1]:
            raise ValueError("current association must be the last history entry")
        if self.processing.processed_revision is not None and (
            self.processing.processed_revision > self.latest_revision
        ):
            raise ValueError("processed_revision cannot exceed latest_revision")
        return self


def source_id_for(scope: ScopeIdentity, identity: SourceIdentity) -> str:
    """Deterministic ID: same scope + provider object always maps to one source."""
    digest = sha256_hex(canonical_bytes({
        "scope": scope.model_dump(mode="json"),
        "provider": identity.provider,
        "connection_id": identity.connection_id,
        "source_workspace_id": identity.source_workspace_id,
        "provider_object_id": identity.provider_object_id,
        "kind": identity.kind,
    }))
    return f"src_{digest[:32]}"


# ---------------------------------------------------------------------------
# Normalized provider payload (adapter output)
# ---------------------------------------------------------------------------

class NormalizedParticipant(_Strict):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class NormalizedTranscriptSegment(_Strict):
    speaker_source: Literal["microphone", "speaker"] | None = None
    attribution: Literal["me", "them"] | None = None
    speaker_label: str | None = Field(default=None, max_length=120)
    text: str = Field(min_length=1, max_length=20_000)
    start_time: datetime | None = None
    end_time: datetime | None = None


class NormalizedMeetingContent(_Strict):
    """Private content. Never included in list/detail API responses."""

    summary_text: str | None = Field(default=None, max_length=200_000)
    summary_markdown: str | None = Field(default=None, max_length=400_000)
    private_notes_text: str | None = Field(default=None, max_length=200_000)
    private_notes_markdown: str | None = Field(default=None, max_length=400_000)
    transcript: list[NormalizedTranscriptSegment] = Field(default_factory=list, max_length=20_000)

    def is_empty(self) -> bool:
        return not (
            self.summary_text or self.summary_markdown or self.private_notes_text
            or self.private_notes_markdown or self.transcript
        )

    def content_hash(self) -> str:
        return sha256_hex(canonical_bytes(self.model_dump(mode="json")))

    def metrics(self, *, title_present: bool, attendee_count: int) -> RevisionMetrics:
        return RevisionMetrics(
            title_present=title_present,
            attendee_count=attendee_count,
            summary_chars=len(self.summary_text or "") + len(self.summary_markdown or ""),
            private_notes_chars=len(self.private_notes_text or "") + len(self.private_notes_markdown or ""),
            transcript_segments=len(self.transcript),
            transcript_chars=sum(len(segment.text) for segment in self.transcript),
        )


class NormalizedMeeting(_Strict):
    """Adapter output: provider identity + body-free metadata + private content."""

    identity: SourceIdentity
    title: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None
    provider_updated_at: datetime | None = None
    attendees: list[NormalizedParticipant] = Field(default_factory=list, max_length=1000)
    availability: ContentAvailability
    unavailable_reason: str | None = Field(default=None, max_length=300)
    content: NormalizedMeetingContent = Field(default_factory=NormalizedMeetingContent)
    provider_web_url: str | None = Field(default=None, max_length=1000)

    @field_validator("provider_web_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("provider_web_url must be https")
        return value

    def private_metadata(self) -> dict[str, Any]:
        """Body-free but still private metadata, versioned alongside content."""
        return {
            "title": self.title,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "provider_updated_at": (
                self.provider_updated_at.isoformat() if self.provider_updated_at else None
            ),
            "attendees": [person.model_dump(mode="json") for person in self.attendees],
            "provider_web_url": self.provider_web_url,
        }

    def snapshot(self) -> dict[str, Any]:
        """The private per-revision record persisted by the ledger."""
        return {
            "availability": self.availability,
            "unavailable_reason": self.unavailable_reason,
            "metadata": self.private_metadata(),
            "content": self.content.model_dump(mode="json"),
        }

    @model_validator(mode="after")
    def _availability_matches_content(self) -> "NormalizedMeeting":
        has_content = not self.content.is_empty()
        if self.availability == "content_available" and not has_content:
            raise ValueError("content_available requires some content")
        if self.availability != "content_available" and has_content:
            raise ValueError(f"availability {self.availability!r} cannot carry content")
        return self
