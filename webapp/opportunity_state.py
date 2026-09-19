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
from typing import Annotated, Any, Literal

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


class BusinessCaseArea(StrictModel):
    """One evidence-attributable part of the commercial business case."""

    knowledge: Claim
    missing_information: list[LongText] = Field(max_length=10)


class BusinessCase(StrictModel):
    current_state: BusinessCaseArea
    future_state: BusinessCaseArea
    negative_consequences: BusinessCaseArea
    positive_business_outcomes: BusinessCaseArea


class MeddpiccDimensionKey(str, Enum):
    METRICS = "metrics"
    ECONOMIC_BUYER = "economic_buyer"
    DECISION_CRITERIA = "decision_criteria"
    DECISION_PROCESS = "decision_process"
    PAPER_PROCESS = "paper_process"
    IDENTIFY_PAIN = "identify_pain"
    CHAMPION = "champion"
    COMPETITION = "competition"


class MeddpiccDimension(StrictModel):
    key: MeddpiccDimensionKey
    knowledge: Claim
    missing_information: list[LongText] = Field(max_length=10)
    suggested_discovery: list[LongText] = Field(max_length=10)


class Meddpicc(StrictModel):
    dimensions: list[MeddpiccDimension] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_dimensions(self) -> "Meddpicc":
        keys = [item.key for item in self.dimensions]
        if len(set(keys)) != 8 or set(keys) != set(MeddpiccDimensionKey):
            raise ValueError("MEDDPICC must contain each dimension exactly once")
        return self


def _unknown_claim() -> Claim:
    return Claim(
        knowledge_state=KnowledgeState.UNKNOWN,
        value=None,
        confidence=Confidence.LOW,
        confirmation=ConfirmationMode.UNKNOWN,
        evidence_refs=[],
    )


def _legacy_business_case() -> BusinessCase:
    def area() -> BusinessCaseArea:
        return BusinessCaseArea(
            knowledge=_unknown_claim(),
            missing_information=["Not established from authorized evidence."],
        )

    return BusinessCase(
        current_state=area(),
        future_state=area(),
        negative_consequences=area(),
        positive_business_outcomes=area(),
    )


def _legacy_meddpicc() -> Meddpicc:
    return Meddpicc(dimensions=[
        MeddpiccDimension(
            key=key,
            knowledge=_unknown_claim(),
            missing_information=["Not established from authorized evidence."],
            suggested_discovery=[f"Ask the customer to establish {key.value.replace('_', ' ')}."],
        )
        for key in MeddpiccDimensionKey
    ])


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


class StakeholderCategory(str, Enum):
    CHAMPION = "champion"
    ECONOMIC_BUYER = "economic_buyer"
    TECHNICAL_DECISION_MAKER = "technical_decision_maker"
    SECURITY_APPROVER = "security_approver"
    PROCUREMENT = "procurement"
    END_USER = "end_user"
    OTHER = "other"


class StakeholderInfluence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class StakeholderEngagement(str, Enum):
    ACTIVE = "active"
    ENGAGED = "engaged"
    LIMITED = "limited"
    UNENGAGED = "unengaged"
    UNKNOWN = "unknown"


class StakeholderStance(str, Enum):
    SUPPORTIVE = "supportive"
    NEUTRAL = "neutral"
    SKEPTICAL = "skeptical"
    OPPOSED = "opposed"
    UNKNOWN = "unknown"


class StakeholderBlockerStatus(str, Enum):
    NOT_A_BLOCKER = "not_a_blocker"
    POTENTIAL_BLOCKER = "potential_blocker"
    ACTIVE_BLOCKER = "active_blocker"
    UNKNOWN = "unknown"


class Stakeholder(StrictModel):
    key: StableKey
    name: ShortText
    title_or_role: ShortText | None = None
    category: StakeholderCategory
    influence: StakeholderInfluence
    engagement: StakeholderEngagement
    stance: StakeholderStance
    blocker_status: StakeholderBlockerStatus
    blocker_reason: LongText | None = None
    recommended_next_step: LongText | None = None
    evidence_refs: list[EvidenceReference] = Field(min_length=1, max_length=20)
    missing_information: list[LongText] = Field(default_factory=list, max_length=10)


_KEY_STAKEHOLDER_ROLES = {
    StakeholderCategory.CHAMPION,
    StakeholderCategory.ECONOMIC_BUYER,
    StakeholderCategory.TECHNICAL_DECISION_MAKER,
}


class StakeholderMap(StrictModel):
    stakeholders: list[Stakeholder] = Field(default_factory=list, max_length=24)
    missing_key_roles: list[StakeholderCategory] = Field(max_length=3)
    missing_information: list[LongText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_stakeholder_map(self) -> "StakeholderMap":
        keys = [item.key for item in self.stakeholders]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate stakeholder key")
        if "stakeholder-gaps" in keys:
            raise ValueError("stakeholder-gaps is reserved for deterministic change history")
        if len(self.missing_key_roles) != len(set(self.missing_key_roles)):
            raise ValueError("duplicate missing stakeholder role")
        if not set(self.missing_key_roles) <= _KEY_STAKEHOLDER_ROLES:
            raise ValueError("missing key roles may only identify champion, economic buyer, or technical decision maker")
        established = {item.category for item in self.stakeholders}
        overlap = established & set(self.missing_key_roles)
        if overlap:
            raise ValueError("an established stakeholder category cannot also be listed as a missing key role")
        return self


def _legacy_stakeholder_map() -> StakeholderMap:
    return StakeholderMap(
        stakeholders=[],
        missing_key_roles=sorted(_KEY_STAKEHOLDER_ROLES, key=lambda item: item.value),
        missing_information=["Stakeholders are not established from authorized evidence."],
    )


class OpportunityStateCandidate(StrictModel):
    schema_version: Literal[1]
    brief: OpportunityBrief
    business_case: BusinessCase
    meddpicc: Meddpicc
    stakeholders: StakeholderMap
    health_indicators: list[HealthIndicator] = Field(min_length=4, max_length=4)
    risks: list[OpportunityRisk] = Field(default_factory=list, max_length=12)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list, max_length=12)
    missing_information: list[MissingInformation] = Field(default_factory=list, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_candidate(cls, value: Any) -> Any:
        """Load earlier state while keeping the current executor schema fields required."""
        if isinstance(value, dict):
            value = dict(value)
            value.setdefault("business_case", _legacy_business_case().model_dump(mode="json"))
            value.setdefault("meddpicc", _legacy_meddpicc().model_dump(mode="json"))
            value.setdefault("stakeholders", _legacy_stakeholder_map().model_dump(mode="json"))
        return value

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


class ChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    RESOLVED = "resolved"
    CHANGED = "changed"
    REPLACED = "replaced"


class TypedItemChange(StrictModel):
    """A bounded, deterministic description of one stable-keyed state change."""

    key: StableKey
    change_type: ChangeType
    fields: list[ShortText] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_fields(self) -> "TypedItemChange":
        if self.change_type == ChangeType.CHANGED and not self.fields:
            raise ValueError("changed items require at least one changed field")
        if self.change_type != ChangeType.CHANGED and self.fields:
            raise ValueError("only changed items may list changed fields")
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("duplicate changed field")
        return self


class EvidenceSourceChange(StrictModel):
    source_type: EvidenceSourceType
    source_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=160)]
    change_type: Literal[ChangeType.ADDED, ChangeType.REPLACED]


class OpportunityStateChangeSet(StrictModel):
    """Typed diff computed by the application, never authored by the model."""

    schema_version: Literal[1]
    parent_version_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    parent_revision: int = Field(ge=1, le=999_999)
    child_version_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
    child_revision: int = Field(ge=2, le=1_000_000)
    brief: list[TypedItemChange] = Field(default_factory=list, max_length=5)
    business_case: list[TypedItemChange] = Field(default_factory=list, max_length=4)
    meddpicc: list[TypedItemChange] = Field(default_factory=list, max_length=8)
    stakeholders: list[TypedItemChange] = Field(default_factory=list, max_length=48)
    health_indicators: list[TypedItemChange] = Field(default_factory=list, max_length=4)
    risks: list[TypedItemChange] = Field(default_factory=list, max_length=24)
    recommended_actions: list[TypedItemChange] = Field(default_factory=list, max_length=24)
    missing_information: list[TypedItemChange] = Field(default_factory=list, max_length=48)
    evidence_sources: list[EvidenceSourceChange] = Field(default_factory=list, max_length=50)
    metadata_changed: bool

    @model_validator(mode="after")
    def validate_change_set(self) -> "OpportunityStateChangeSet":
        if self.child_revision != self.parent_revision + 1:
            raise ValueError("change set revisions must be consecutive")
        for label, values in (
            ("brief", self.brief),
            ("business case", self.business_case),
            ("MEDDPICC", self.meddpicc),
            ("stakeholder", self.stakeholders),
            ("health indicator", self.health_indicators),
            ("risk", self.risks),
            ("recommended action", self.recommended_actions),
            ("missing information", self.missing_information),
        ):
            keys = [item.key for item in values]
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} change key")
        evidence_keys = [(item.source_type, item.source_id) for item in self.evidence_sources]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError("duplicate evidence source change")
        return self

    def high_level_counts(self) -> dict[str, int]:
        return {
            "brief": len(self.brief),
            "business_case": len(self.business_case),
            "meddpicc": len(self.meddpicc),
            "stakeholders": len(self.stakeholders),
            "health_indicators": len(self.health_indicators),
            "risks": len(self.risks),
            "recommended_actions": len(self.recommended_actions),
            "missing_information": len(self.missing_information),
            "evidence_sources": len(self.evidence_sources),
            "metadata": int(self.metadata_changed),
        }


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
    parent_version_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")] | None = None
    parent_revision: int | None = Field(default=None, ge=1, le=999_999)
    change_set: OpportunityStateChangeSet | None = None

    _aware_timestamp = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def validate_manifest(self) -> "OpportunityStateVersion":
        keys = [(entry.source_type.value, entry.source_id) for entry in self.evidence_manifest]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate evidence manifest identity")
        if self.evidence_manifest_hash != evidence_manifest_hash(self.evidence_manifest):
            raise ValueError("evidence manifest hash mismatch")
        validate_candidate_evidence(self.state, self.evidence_manifest)
        if self.revision == 1:
            if self.parent_version_id is not None or self.parent_revision is not None or self.change_set is not None:
                raise ValueError("revision 1 must not have a parent or change set")
        else:
            if self.parent_version_id is None or self.parent_revision is None or self.change_set is None:
                raise ValueError("later revisions require a parent and change set")
            if self.parent_revision != self.revision - 1:
                raise ValueError("parent revision must immediately precede child revision")
            if (
                self.change_set.parent_version_id != self.parent_version_id
                or self.change_set.parent_revision != self.parent_revision
                or self.change_set.child_version_id != self.version_id
                or self.change_set.child_revision != self.revision
            ):
                raise ValueError("change set version relationship mismatch")
        return self


def _canonical_change_value(value: Any) -> Any:
    """Normalize semantically unordered evidence references before comparing."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        normalized = {key: _canonical_change_value(item) for key, item in value.items()}
        refs = normalized.get("evidence_refs")
        if isinstance(refs, list):
            normalized["evidence_refs"] = sorted(
                refs,
                key=lambda item: (
                    str(item.get("source_type", "")), str(item.get("source_id", "")),
                    str(item.get("locator") or ""),
                ),
            )
        return normalized
    if isinstance(value, list):
        return [_canonical_change_value(item) for item in value]
    return value


def _changed_fields(before: BaseModel, after: BaseModel) -> list[str]:
    left = _canonical_change_value(before)
    right = _canonical_change_value(after)
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def _keyed_changes(
    before: list[BaseModel],
    after: list[BaseModel],
    *,
    removed_type: ChangeType = ChangeType.REMOVED,
) -> list[TypedItemChange]:
    left = {str(item.key.value if isinstance(item.key, Enum) else item.key): item for item in before}
    right = {str(item.key.value if isinstance(item.key, Enum) else item.key): item for item in after}
    changes: list[TypedItemChange] = []
    for key in sorted(set(left) | set(right)):
        if key not in left:
            changes.append(TypedItemChange(key=key, change_type=ChangeType.ADDED))
        elif key not in right:
            changes.append(TypedItemChange(key=key, change_type=removed_type))
        else:
            fields = _changed_fields(left[key], right[key])
            if fields:
                changes.append(TypedItemChange(key=key, change_type=ChangeType.CHANGED, fields=fields))
    return changes


def deterministic_change_set(
    *,
    parent: OpportunityStateVersion,
    child_version_id: str,
    child_revision: int,
    child_state: OpportunityStateCandidate,
    child_manifest: list[EvidenceManifestEntry],
) -> OpportunityStateChangeSet:
    """Compare validated versions deterministically and without list-order noise."""
    brief_changes: list[TypedItemChange] = []
    for key in sorted(parent.state.brief.model_fields):
        fields = _changed_fields(getattr(parent.state.brief, key), getattr(child_state.brief, key))
        if fields:
            brief_changes.append(TypedItemChange(key=key, change_type=ChangeType.CHANGED, fields=fields))

    parent_manifest = {(item.source_type, item.source_id): item for item in parent.evidence_manifest}
    child_manifest_map = {(item.source_type, item.source_id): item for item in child_manifest}
    evidence_changes: list[EvidenceSourceChange] = []
    for source_type, source_id in sorted(child_manifest_map, key=lambda item: (item[0].value, item[1])):
        previous = parent_manifest.get((source_type, source_id))
        current = child_manifest_map[(source_type, source_id)]
        if previous is None:
            evidence_changes.append(EvidenceSourceChange(
                source_type=source_type, source_id=source_id, change_type=ChangeType.ADDED
            ))
        elif previous.sha256 != current.sha256:
            evidence_changes.append(EvidenceSourceChange(
                source_type=source_type, source_id=source_id, change_type=ChangeType.REPLACED
            ))
    metadata_changed = any(
        item.source_type == EvidenceSourceType.OPPORTUNITY_METADATA
        for item in evidence_changes
    )
    # Metadata has its own flag and is not duplicated in the evidence-source list.
    evidence_changes = [
        item for item in evidence_changes if item.source_type != EvidenceSourceType.OPPORTUNITY_METADATA
    ]
    stakeholder_changes = _keyed_changes(
        parent.state.stakeholders.stakeholders,
        child_state.stakeholders.stakeholders,
    )
    stakeholder_gap_fields = [
        field for field in ("missing_information", "missing_key_roles")
        if _canonical_change_value(getattr(parent.state.stakeholders, field))
        != _canonical_change_value(getattr(child_state.stakeholders, field))
    ]
    if stakeholder_gap_fields:
        stakeholder_changes.append(TypedItemChange(
            key="stakeholder-gaps",
            change_type=ChangeType.CHANGED,
            fields=stakeholder_gap_fields,
        ))

    return OpportunityStateChangeSet(
        schema_version=1,
        parent_version_id=parent.version_id,
        parent_revision=parent.revision,
        child_version_id=child_version_id,
        child_revision=child_revision,
        brief=brief_changes,
        business_case=[
            TypedItemChange(key=key, change_type=ChangeType.CHANGED, fields=fields)
            for key in sorted(parent.state.business_case.model_fields)
            if (fields := _changed_fields(
                getattr(parent.state.business_case, key), getattr(child_state.business_case, key)
            ))
        ],
        meddpicc=_keyed_changes(parent.state.meddpicc.dimensions, child_state.meddpicc.dimensions),
        stakeholders=stakeholder_changes,
        health_indicators=_keyed_changes(parent.state.health_indicators, child_state.health_indicators),
        risks=_keyed_changes(parent.state.risks, child_state.risks),
        recommended_actions=_keyed_changes(
            parent.state.recommended_actions, child_state.recommended_actions
        ),
        missing_information=_keyed_changes(
            parent.state.missing_information,
            child_state.missing_information,
            removed_type=ChangeType.RESOLVED,
        ),
        evidence_sources=evidence_changes,
        metadata_changed=metadata_changed,
    )


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
    for area in candidate.business_case.model_dump().keys():
        yield from getattr(candidate.business_case, area).knowledge.evidence_refs
    for dimension in candidate.meddpicc.dimensions:
        yield from dimension.knowledge.evidence_refs
    for stakeholder in candidate.stakeholders.stakeholders:
        yield from stakeholder.evidence_refs
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
