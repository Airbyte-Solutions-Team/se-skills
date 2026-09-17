"""Strict, versioned contract for local canonical opportunity state.

The contract intentionally contains conclusions and provenance only. Raw
transcript bodies, generated output prose, prompts, filesystem paths, and
credentials are never valid fields in persisted opportunity state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
StableKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]{0,79}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class KnowledgeState(str, Enum):
    KNOWN = "known"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    CONFLICTING = "conflicting"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    MEDIUM_HIGH = "medium_high"
    HIGH = "high"


class ConfirmationMode(str, Enum):
    EVIDENCE_BACKED = "evidence_backed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class EvidenceSourceType(str, Enum):
    TRANSCRIPT = "transcript"
    OPPORTUNITY_METADATA = "opportunity_metadata"


class EvidenceReference(StrictModel):
    source_type: EvidenceSourceType
    source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=160)]
    locator: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamp must include a timezone")
    return value


class Claim(StrictModel):
    knowledge_state: KnowledgeState
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)] | None = None
    confidence: Confidence
    confirmation: ConfirmationMode
    last_confirmed_at: datetime | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=20)

    _aware_timestamp = field_validator("last_confirmed_at")(_require_aware)

    @model_validator(mode="after")
    def validate_claim(self) -> "Claim":
        if self.knowledge_state in {KnowledgeState.KNOWN, KnowledgeState.PARTIAL, KnowledgeState.CONFLICTING}:
            if self.value is None:
                raise ValueError("known, partial, and conflicting claims require a value")
        elif self.value is not None:
            raise ValueError("unknown and not-applicable claims must not invent a value")
        if self.confirmation == ConfirmationMode.EVIDENCE_BACKED and not self.evidence_refs:
            raise ValueError("evidence-backed claims require at least one evidence reference")
        if self.confirmation == ConfirmationMode.UNKNOWN and self.evidence_refs:
            raise ValueError("unknown claims cannot cite evidence")
        refs = [(ref.source_type.value, ref.source_id, ref.locator) for ref in self.evidence_refs]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate evidence reference")
        return self


class OpportunityBrief(StrictModel):
    customer_objective: Claim
    why_airbyte: Claim
    current_status: Claim
    path_to_decision: Claim
    immediate_priority: Claim


class HealthIndicatorKey(str, Enum):
    DEAL_QUALIFICATION = "deal_qualification"
    TECHNICAL_FIT = "technical_fit"
    TIMELINE = "timeline"
    CUSTOMER_SENTIMENT = "customer_sentiment"


class HealthStatus(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    BLOCKED = "blocked"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    COMFORTABLE = "comfortable"
    TIGHT = "tight"
    AT_RISK = "at_risk"
    UNKNOWN = "unknown"
    POSITIVE = "positive"
    MIXED = "mixed"
    NEGATIVE = "negative"


_HEALTH_STATUSES: dict[HealthIndicatorKey, set[HealthStatus]] = {
    HealthIndicatorKey.DEAL_QUALIFICATION: {
        HealthStatus.STRONG, HealthStatus.MODERATE, HealthStatus.WEAK, HealthStatus.INSUFFICIENT_EVIDENCE,
    },
    HealthIndicatorKey.TECHNICAL_FIT: {
        HealthStatus.STRONG, HealthStatus.MODERATE, HealthStatus.WEAK, HealthStatus.BLOCKED,
        HealthStatus.INSUFFICIENT_EVIDENCE,
    },
    HealthIndicatorKey.TIMELINE: {
        HealthStatus.COMFORTABLE, HealthStatus.TIGHT, HealthStatus.AT_RISK, HealthStatus.UNKNOWN,
    },
    HealthIndicatorKey.CUSTOMER_SENTIMENT: {
        HealthStatus.POSITIVE, HealthStatus.MIXED, HealthStatus.NEGATIVE, HealthStatus.UNKNOWN,
    },
}


class HealthIndicator(StrictModel):
    key: HealthIndicatorKey
    status: HealthStatus
    reason: LongText
    confidence: Confidence
    confirmation: ConfirmationMode
    last_confirmed_at: datetime | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=20)

    _aware_timestamp = field_validator("last_confirmed_at")(_require_aware)

    @model_validator(mode="after")
    def validate_status(self) -> "HealthIndicator":
        if self.status not in _HEALTH_STATUSES[self.key]:
            raise ValueError(f"status {self.status.value} is invalid for {self.key.value}")
        if self.confirmation == ConfirmationMode.EVIDENCE_BACKED and not self.evidence_refs:
            raise ValueError("evidence-backed indicators require evidence")
        return self


class RiskSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskClassification(str, Enum):
    CRITICAL_BLOCKER = "critical_blocker"
    IMPLEMENTATION_RISK = "implementation_risk"
    COMMERCIAL_RISK = "commercial_risk"
    TIMELINE_RISK = "timeline_risk"
    OPEN_VALIDATION_ITEM = "open_validation_item"


class OpportunityRisk(StrictModel):
    key: StableKey
    title: ShortText
    description: LongText
    severity: RiskSeverity
    classification: RiskClassification
    owner: ShortText | None = None
    mitigation: LongText | None = None
    last_updated_at: datetime | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=20)

    _aware_timestamp = field_validator("last_updated_at")(_require_aware)


class ActionStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class RecommendedAction(StrictModel):
    key: StableKey
    action: ShortText
    goal: LongText
    definition_of_done: LongText
    owner: ShortText | None = None
    due_date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    status: ActionStatus
    related_item_key: StableKey | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=20)


class MissingInformation(StrictModel):
    key: StableKey
    description: LongText
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=10)


class OpportunityStateCandidate(StrictModel):
    schema_version: Literal[1]
    brief: OpportunityBrief
    health_indicators: list[HealthIndicator] = Field(min_length=4, max_length=4)
    risks: list[OpportunityRisk] = Field(default_factory=list, max_length=12)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list, max_length=12)
    missing_information: list[MissingInformation] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_stable_keys(self) -> "OpportunityStateCandidate":
        indicator_keys = [item.key for item in self.health_indicators]
        if len(set(indicator_keys)) != 4 or set(indicator_keys) != set(HealthIndicatorKey):
            raise ValueError("health indicators must contain each stable indicator key exactly once")
        for label, values in (
            ("risk", [item.key for item in self.risks]),
            ("recommended action", [item.key for item in self.recommended_actions]),
            ("missing information", [item.key for item in self.missing_information]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} key")
        return self


class OpportunityIdentity(StrictModel):
    account: ShortText
    opportunity_slug: StableKey
    opportunity_name: ShortText


class EvidenceManifestEntry(StrictModel):
    source_type: EvidenceSourceType
    source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=160)]
    sha256: Sha256
    byte_count: int = Field(ge=0, le=2_000_000)
    observed_at: datetime
    display_name: ShortText

    _aware_timestamp = field_validator("observed_at")(_require_aware)


class GenerationProvenance(StrictModel):
    updater_version: ShortText
    model: ShortText
    runtime: ShortText
    cli_version: ShortText


class OpportunityStateVersion(StrictModel):
    schema_version: Literal[1]
    version_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    revision: int = Field(ge=1, le=1_000_000)
    identity: OpportunityIdentity
    created_at: datetime
    evidence_manifest: list[EvidenceManifestEntry] = Field(min_length=2, max_length=51)
    evidence_manifest_hash: Sha256
    provenance: GenerationProvenance
    state: OpportunityStateCandidate

    _aware_timestamp = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def validate_manifest(self) -> "OpportunityStateVersion":
        keys = [(entry.source_type.value, entry.source_id) for entry in self.evidence_manifest]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate evidence manifest identity")
        if self.evidence_manifest_hash != evidence_manifest_hash(self.evidence_manifest):
            raise ValueError("evidence manifest hash mismatch")
        validate_candidate_evidence(self.state, self.evidence_manifest)
        return self


def evidence_manifest_hash(entries: list[EvidenceManifestEntry]) -> str:
    """Return an order-independent digest of the exact authorized evidence set."""
    ordered = sorted(
        (
            {
                "source_type": entry.source_type.value,
                "source_id": entry.source_id,
                "sha256": entry.sha256,
                "byte_count": entry.byte_count,
            }
            for entry in entries
        ),
        key=lambda item: (item["source_type"], item["source_id"], item["sha256"]),
    )
    encoded = json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def iter_evidence_references(candidate: OpportunityStateCandidate):
    for claim in candidate.brief.model_dump().keys():
        yield from getattr(candidate.brief, claim).evidence_refs
    for indicator in candidate.health_indicators:
        yield from indicator.evidence_refs
    for risk in candidate.risks:
        yield from risk.evidence_refs
    for action in candidate.recommended_actions:
        yield from action.evidence_refs
    for missing in candidate.missing_information:
        yield from missing.evidence_refs


def validate_candidate_evidence(
    candidate: OpportunityStateCandidate,
    manifest: list[EvidenceManifestEntry],
) -> None:
    allowed = {(entry.source_type, entry.source_id) for entry in manifest}
    for ref in iter_evidence_references(candidate):
        if (ref.source_type, ref.source_id) not in allowed:
            raise ValueError("candidate contains an unauthorized evidence reference")
