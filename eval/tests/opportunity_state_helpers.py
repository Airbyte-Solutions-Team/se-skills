from __future__ import annotations

from datetime import datetime, timezone

from opportunity_state import (
    ActionStatus,
    Claim,
    Confidence,
    ConfirmationMode,
    EvidenceReference,
    EvidenceSourceType,
    HealthIndicator,
    HealthIndicatorKey,
    HealthStatus,
    KnowledgeState,
    OpportunityBrief,
    OpportunityStateCandidate,
    RecommendedAction,
)


TRANSCRIPT_ID = "tr_abcdefghijklmnopqrstuvwxyz123456"
METADATA_ID = "opportunity-metadata-v1"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def evidence_ref(source_id: str = TRANSCRIPT_ID) -> EvidenceReference:
    source_type = (
        EvidenceSourceType.OPPORTUNITY_METADATA
        if source_id == METADATA_ID
        else EvidenceSourceType.TRANSCRIPT
    )
    return EvidenceReference(source_type=source_type, source_id=source_id, locator="00:01:00")


def claim(value: str = "Evidence-backed value") -> Claim:
    return Claim(
        knowledge_state=KnowledgeState.KNOWN,
        value=value,
        confidence=Confidence.HIGH,
        confirmation=ConfirmationMode.EVIDENCE_BACKED,
        last_confirmed_at=NOW,
        evidence_refs=[evidence_ref()],
    )


def candidate() -> OpportunityStateCandidate:
    return OpportunityStateCandidate(
        schema_version=1,
        brief=OpportunityBrief(
            customer_objective=claim("Centralize operational data"),
            why_airbyte=claim("Reliable self-managed data movement"),
            current_status=claim("Technical evaluation planning"),
            path_to_decision=claim("Validate requirements, then security review"),
            immediate_priority=claim("Confirm required connectors"),
        ),
        health_indicators=[
            HealthIndicator(
                key=HealthIndicatorKey.DEAL_QUALIFICATION,
                status=HealthStatus.MODERATE,
                reason="Business objective is known; decision authority is not established.",
                confidence=Confidence.MEDIUM,
                confirmation=ConfirmationMode.EVIDENCE_BACKED,
                last_confirmed_at=NOW,
                evidence_refs=[evidence_ref()],
            ),
            HealthIndicator(
                key=HealthIndicatorKey.TECHNICAL_FIT,
                status=HealthStatus.STRONG,
                reason="The named source and destination are supported.",
                confidence=Confidence.HIGH,
                confirmation=ConfirmationMode.EVIDENCE_BACKED,
                last_confirmed_at=NOW,
                evidence_refs=[evidence_ref()],
            ),
            HealthIndicator(
                key=HealthIndicatorKey.TIMELINE,
                status=HealthStatus.UNKNOWN,
                reason="No decision date was provided.",
                confidence=Confidence.LOW,
                confirmation=ConfirmationMode.UNKNOWN,
                evidence_refs=[],
            ),
            HealthIndicator(
                key=HealthIndicatorKey.CUSTOMER_SENTIMENT,
                status=HealthStatus.POSITIVE,
                reason="The customer expressed interest in proceeding.",
                confidence=Confidence.MEDIUM,
                confirmation=ConfirmationMode.EVIDENCE_BACKED,
                last_confirmed_at=NOW,
                evidence_refs=[evidence_ref()],
            ),
        ],
        risks=[],
        recommended_actions=[RecommendedAction(
            key="confirm-connectors",
            action="Confirm must-have connectors",
            goal="Bound technical feasibility.",
            definition_of_done="Customer confirms the source and destination list.",
            status=ActionStatus.NOT_STARTED,
            evidence_refs=[evidence_ref()],
        )],
        missing_information=[],
    )
