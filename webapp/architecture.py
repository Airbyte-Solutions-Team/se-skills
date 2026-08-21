"""Canonical content architecture for SE skill outputs.

This module declares the intended H2/H3 structure for every normal report-style
skill so the validator, reader, and tests share one source of truth. It also
carries the legacy heading aliases needed to keep older outputs openable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _normalize_heading(text: str) -> str:
    key = text.strip().lower()
    key = re.sub(r"&", "and", key)
    key = re.sub(r"[^a-z0-9]+", "-", key)
    return key.strip("-")


@dataclass
class SkillArchitecture:
    """Canonical architecture declaration for one skill."""

    skill: str
    profile: str
    top_summary_name: str
    canonical_h2_order: list[str] = field(default_factory=list)
    h3_groups: dict[str, list[str]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    source_coverage_required: bool = True
    structured_exception: bool = False
    notes: str = ""

    def canonical_key(self, heading: str) -> str | None:
        """Return canonical key for an H2 heading, or None if unknown."""
        normalized = _normalize_heading(heading)
        if normalized in self.aliases:
            normalized = self.aliases[normalized]
        return normalized if normalized in self.canonical_h2_order else None

    def is_valid_h2(self, heading: str) -> bool:
        return self.canonical_key(heading) is not None

    def legacy_headings(self) -> set[str]:
        """Old H2 headings that are not themselves canonical H2s."""
        return set(self.aliases.keys()) - set(self.canonical_h2_order)


_SOURCE_COVERAGE = "source-coverage"

# ---------------------------------------------------------------------------
# Report-style skills
# ---------------------------------------------------------------------------

_PREP_CALL = SkillArchitecture(
    skill="prep-call",
    profile="Call Planning",
    top_summary_name="Meeting Snapshot",
    canonical_h2_order=[
        "account-context",
        "call-strategy",
        "discovery-plan",
        "agenda",
        "watch-outs",
        "desired-next-step",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "meeting-snapshot": [
            "date-time-duration",
            "primary-contact",
            "attendees",
            "call-objective",
            "key-unknown",
        ],
        "account-context": [
            "company-snapshot",
            "why-airbyte",
            "prior-call-context",
            "what-we-already-know",
            "open-threads-from-prior-calls",
        ],
        "call-strategy": [
            "point-of-view-to-test",
            "suggested-opener",
        ],
        "discovery-plan": [
            "must-ask-questions",
            "implication-depth-questions",
            "persona-specific-questions",
        ],
    },
    aliases={
        "company-snapshot": "account-context",
        "why-airbyte-hypothesis": "account-context",
        "why-airbyte": "account-context",
        "what-the-ae-already-learned-from-prior-gong-call": "account-context",
        "what-the-ae-already-learned": "account-context",
        "where-we-left-off-if-follow-up-call": "account-context",
        "where-we-left-off": "account-context",
        "reframe-hypothesis-challenger": "call-strategy",
        "reframe-hypothesis": "call-strategy",
        "upfront-contract-sandler": "call-strategy",
        "upfront-contract": "call-strategy",
        "discovery-questions": "discovery-plan",
        "spin-implication-ladders": "discovery-plan",
        "spin-ladders": "discovery-plan",
        "per-persona-questions": "discovery-plan",
        "persona-questions": "discovery-plan",
        "suggested-agenda-30-min": "agenda",
        "suggested-agenda": "agenda",
        "watch-outs-landmines": "watch-outs",
        "landmines": "watch-outs",
        "suggested-next-step-concrete-date-plus-attendees-plus-agenda": "desired-next-step",
        "suggested-next-step": "desired-next-step",
    },
)

_POST_CALL = SkillArchitecture(
    skill="post-call",
    profile="Call Recap",
    top_summary_name="Call Snapshot",
    canonical_h2_order=[
        "key-takeaways",
        "scope-and-technical-changes",
        "deal-impact",
        "objections-and-open-questions",
        "actions-and-next-step",
        "coaching-observations",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "call-snapshot": [
            "date",
            "call-type",
            "customer-attendees",
            "airbyte-attendees",
            "one-line-deal-impact",
        ],
        "scope-and-technical-changes": [
            "sources-and-destinations",
            "technical-notes",
            "architecture",
            "requirements",
        ],
        "deal-impact": [
            "movement",
            "deal-health",
            "meddpicc-changes",
        ],
        "objections-and-open-questions": [
            "new-objections-concerns-surfaced",
            "open-questions-follow-ups",
        ],
        "actions-and-next-step": [
            "action-items",
            "next-step",
        ],
    },
    aliases={
        "key-takeaways": "key-takeaways",
        "deal-health-signals": "deal-impact",
        "new-objections-concerns-surfaced": "objections-and-open-questions",
        "new-objections": "objections-and-open-questions",
        "action-items": "actions-and-next-step",
        "sources-and-destinations": "scope-and-technical-changes",
        "technical-notes": "scope-and-technical-changes",
        "open-questions-follow-ups": "objections-and-open-questions",
        "attendees": "key-takeaways",
        "next-step": "actions-and-next-step",
        "coaching-observations": "coaching-observations",
        "meddpicc-quick-pass": "deal-impact",
    },
    source_coverage_required=True,
)

_BIZ_QUAL = SkillArchitecture(
    skill="biz-qual",
    profile="Qualification / Decision Assessment",
    top_summary_name="Decision Summary",
    canonical_h2_order=[
        "meddpicc-scorecard",
        "qualification-narrative",
        "movement-and-deal-risks",
        "recommended-next-actions",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "qualification-narrative": [
            "metrics",
            "economic-buyer",
            "decision-criteria",
            "decision-process",
            "paper-process",
            "identify-pain",
            "champion",
            "stakeholder-map",
            "competition",
        ],
        "movement-and-deal-risks": [
            "movement-since-last-qualification",
            "deal-risks",
            "reasons-to-walk-or-deprioritize",
        ],
    },
    aliases={
        "meddpicc-scorecard": "meddpicc-scorecard",
        "no-gap-without-a-close-path": "qualification-narrative",
        "metrics": "qualification-narrative",
        "economic-buyer": "qualification-narrative",
        "decision-criteria": "qualification-narrative",
        "decision-process": "qualification-narrative",
        "paper-process": "qualification-narrative",
        "identify-pain": "qualification-narrative",
        "champion": "qualification-narrative",
        "stakeholder-map": "qualification-narrative",
        "competition": "qualification-narrative",
        "movement-since-last-qualification": "movement-and-deal-risks",
        "deal-risks": "movement-and-deal-risks",
        "reasons-to-walk-deprioritize": "movement-and-deal-risks",
        "reasons-to-walk-or-deprioritize": "movement-and-deal-risks",
        "recommended-next-actions": "recommended-next-actions",
        "recommended-next-action": "recommended-next-actions",
    },
    source_coverage_required=True,
)

_TECH_QUAL = SkillArchitecture(
    skill="tech-qual",
    profile="Qualification / Decision Assessment",
    top_summary_name="Decision Summary",
    canonical_h2_order=[
        "technical-fit-summary",
        "requirements-and-architecture",
        "implementation-readiness",
        "risks-and-open-items",
        "recommended-next-actions",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "requirements-and-architecture": [
            "source-and-destination-landscape",
            "data-volume-latency-frequency",
            "networking-security",
            "transformation-orchestration",
            "operational-expectations",
            "current-stack-and-integration-context",
        ],
        "implementation-readiness": [
            "team-and-implementation-readiness",
            "deployment-entitlement",
        ],
        "risks-and-open-items": [
            "technical-risks-and-open-items",
            "questions-still-needed",
        ],
    },
    aliases={
        "technical-fit-summary": "technical-fit-summary",
        "technical-requirements-and-scope": "requirements-and-architecture",
        "data-sources-and-destinations": "requirements-and-architecture",
        "data-volume-and-scale": "requirements-and-architecture",
        "deployment-model": "requirements-and-architecture",
        "security-and-compliance": "requirements-and-architecture",
        "current-stack-and-integration-context": "requirements-and-architecture",
        "team-and-implementation-readiness": "implementation-readiness",
        "technical-risks-and-open-items": "risks-and-open-items",
        "questions-still-needed": "risks-and-open-items",
        "recommended-next-actions": "recommended-next-actions",
        "sources-destinations": "requirements-and-architecture",
        "technical-notes": "requirements-and-architecture",
    },
    source_coverage_required=True,
)

_DEPLOYMENT_QUAL = SkillArchitecture(
    skill="deployment-model-qual",
    profile="Qualification / Decision Assessment",
    top_summary_name="Decision Summary",
    canonical_h2_order=[
        "deployment-verdict",
        "customer-constraints",
        "remaining-validation",
        "recommended-motion",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "customer-constraints": [
            "the-five-questions",
            "implications-by-answer",
        ],
    },
    aliases={
        "verdict": "deployment-verdict",
        "the-five-questions": "customer-constraints",
        "implications-by-answer": "customer-constraints",
        "discovery-questions-for-next-call": "remaining-validation",
        "recommended-next-action": "recommended-motion",
        "recommended-next-steps": "recommended-motion",
    },
    source_coverage_required=True,
)

_CONNECTOR_FEASIBILITY = SkillArchitecture(
    skill="connector-feasibility",
    profile="Qualification / Decision Assessment",
    top_summary_name="Decision Summary",
    canonical_h2_order=[
        "system-by-system-fit",
        "coverage-gaps-and-custom-work",
        "risks-and-constraints",
        "validation-questions",
        "recommended-next-steps",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "system-by-system-fit": [
            "system",
            "connector",
            "exists",
            "availability",
            "use-case-fit",
            "confidence",
            "top-risk",
        ],
    },
    aliases={
        "fit-verdict": "system-by-system-fit",
        "use-case-summary": "system-by-system-fit",
        "missing-gap-connectors": "coverage-gaps-and-custom-work",
        "constraints-and-edge-cases": "risks-and-constraints",
        "questions-to-ask": "validation-questions",
        "questions-to-ask-next": "validation-questions",
        "recommended-next-steps": "recommended-next-steps",
        "recommended-next-actions": "recommended-next-steps",
    },
    source_coverage_required=True,
)

_DEAL_ASSESSMENT = SkillArchitecture(
    skill="deal-assessment",
    profile="Qualification / Decision Assessment",
    top_summary_name="Decision Summary / Bottom Line",
    canonical_h2_order=[
        "trajectory-and-what-changed",
        "deal-thesis",
        "stakeholders-and-qualification",
        "close-path-blockers-and-loss-risks",
        "recommended-actions-and-coaching",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "trajectory-and-what-changed": [
            "activity-trajectory",
            "what-changed-since-last-assessment",
        ],
        "deal-thesis": [
            "driver",
            "need",
            "urgency",
        ],
        "stakeholders-and-qualification": [
            "stakeholder-read",
            "meddpicc-read",
        ],
        "close-path-blockers-and-loss-risks": [
            "what-would-close-it",
            "deal-blocker",
            "loss-risks",
        ],
    },
    aliases={
        "activity-trajectory": "trajectory-and-what-changed",
        "what-changed-since-last-assessment": "trajectory-and-what-changed",
        "driver": "deal-thesis",
        "need": "deal-thesis",
        "urgency": "deal-thesis",
        "stakeholder-read": "stakeholders-and-qualification",
        "what-would-close-it": "close-path-blockers-and-loss-risks",
        "deal-blocker": "close-path-blockers-and-loss-risks",
        "loss-risks": "close-path-blockers-and-loss-risks",
        "recommended-actions": "recommended-actions-and-coaching",
        "coaching": "recommended-actions-and-coaching",
    },
    source_coverage_required=True,
)

_POC_PLAN = SkillArchitecture(
    skill="poc-plan",
    profile="POC / Execution Plan",
    top_summary_name="POC Summary",
    canonical_h2_order=[
        "poc-objective",
        "success-criteria",
        "scope-and-architecture",
        "mutual-commitments-and-roles",
        "timeline-and-milestones",
        "access-and-prerequisites",
        "risks-and-mitigations",
        "exit-results-review",
        "open-items",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "scope-and-architecture": [
            "sources-and-destinations",
            "technical-notes",
            "poc-architecture",
        ],
    },
    aliases={
        "poc-objective": "poc-objective",
        "mutual-commitments": "mutual-commitments-and-roles",
        "scope": "scope-and-architecture",
        "poc-architecture": "scope-and-architecture",
        "timeline-and-milestones": "timeline-and-milestones",
        "access-prerequisites": "access-and-prerequisites",
        "risks-and-mitigations": "risks-and-mitigations",
        "exit-results-review": "exit-results-review",
        "open-items": "open-items",
    },
    source_coverage_required=True,
)

_ROI_BUSINESS_CASE = SkillArchitecture(
    skill="roi-business-case",
    profile="Business Case",
    top_summary_name="Business-Case Summary",
    canonical_h2_order=[
        "one-slide-eb-view",
        "current-state-baseline",
        "airbyte-cost-projection",
        "payback-and-sensitivity",
        "assumptions-and-confirms",
        _SOURCE_COVERAGE,
    ],
    h3_groups={
        "airbyte-cost-projection": [
            "3-year-tco-comparison",
            "tco-comparison",
        ],
    },
    aliases={
        "one-slide-summary": "one-slide-eb-view",
        "current-state-baseline": "current-state-baseline",
        "airbyte-cost-projection": "airbyte-cost-projection",
        "3-year-tco-comparison": "airbyte-cost-projection",
        "payback-and-sensitivity": "payback-and-sensitivity",
        "assumptions-and-confirms": "assumptions-and-confirms",
        "assumptions": "assumptions-and-confirms",
    },
    source_coverage_required=True,
)

_MUTUAL_CLOSE_PLAN = SkillArchitecture(
    skill="mutual-close-plan",
    profile="Close Plan",
    top_summary_name="Close Summary",
    canonical_h2_order=[
        "path-to-signature",
        "two-sided-responsibilities",
        "critical-path-and-risks",
        "mutual-agreement-ask",
        _SOURCE_COVERAGE,
    ],
    aliases={
        "path-to-signature": "path-to-signature",
        "two-sided-responsibilities": "two-sided-responsibilities",
        "critical-path-and-risks": "critical-path-and-risks",
        "the-mutual-agreement-ask": "mutual-agreement-ask",
        "mutual-agreement-ask": "mutual-agreement-ask",
    },
    source_coverage_required=True,
)

_ACCOUNT_REFRESHER = SkillArchitecture(
    skill="account-refresher",
    profile="Account / Handoff Brief",
    top_summary_name="Account Snapshot",
    canonical_h2_order=[
        "whos-who",
        "story-so-far",
        "current-state",
        "open-items",
        "watch-outs",
        _SOURCE_COVERAGE,
    ],
    aliases={
        "account-refresher-customer": "whos-who",
        "whos-who": "whos-who",
        "whos-who-on-the-account": "whos-who",
        "the-story-so-far": "story-so-far",
        "story-so-far": "story-so-far",
        "where-things-stand-right-now": "current-state",
        "where-things-stand": "current-state",
        "whats-open": "open-items",
    },
    source_coverage_required=True,
)

_NEXT_MOVE = SkillArchitecture(
    skill="next-move",
    profile="Recommendation / Next Move",
    top_summary_name="Recommendation",
    canonical_h2_order=[
        "why-this-move",
        "ranked-next-moves",
        "dont-do-yet",
        "workflow-state",
        "evidence-gaps-and-external-actions",
        _SOURCE_COVERAGE,
    ],
    aliases={
        "next-move": "why-this-move",
        "why-this-move": "why-this-move",
        "ranked-next-moves": "ranked-next-moves",
        "dont-do-yet": "dont-do-yet",
        "workflow-state": "workflow-state",
        "context-inventory": "evidence-gaps-and-external-actions",
        "gaps": "evidence-gaps-and-external-actions",
        "external-actions": "evidence-gaps-and-external-actions",
    },
    source_coverage_required=True,
)

_INTERNAL_PREP = SkillArchitecture(
    skill="internal-prep",
    profile="Recommendation / Next Move",
    top_summary_name="Meeting / Decision Summary",
    canonical_h2_order=[
        "relevant-deal-context",
        "alignment-and-asks",
        "decisions-required",
        _SOURCE_COVERAGE,
    ],
    aliases={
        "ae-sync": "relevant-deal-context",
        "forecast": "relevant-deal-context",
        "exec-readout": "relevant-deal-context",
        "deal-review": "relevant-deal-context",
        "alignment-asks": "alignment-and-asks",
    },
    source_coverage_required=True,
)

_COVERAGE_HANDOFF = SkillArchitecture(
    skill="coverage-handoff",
    profile="Account / Handoff Brief",
    top_summary_name="Coverage Snapshot",
    canonical_h2_order=[
        "deal-snapshot",
        "whos-who",
        "story-so-far",
        "current-state",
        "in-flight-commitments",
        "open-items",
        "technical-threads",
        "access-and-escalation",
        _SOURCE_COVERAGE,
    ],
    aliases={
        "account-snapshot": "deal-snapshot",
        "whos-who": "whos-who",
        "the-story-so-far": "story-so-far",
        "where-things-stand": "current-state",
        "in-flight-commitments": "in-flight-commitments",
        "open-threads": "open-items",
        "technical-threads": "technical-threads",
        "access-and-escalation": "access-and-escalation",
    },
    source_coverage_required=True,
)

_OBJECTION_HANDLER = SkillArchitecture(
    skill="objection-handler",
    profile="Recommendation / Next Move",
    top_summary_name="Severity / Bottom Line",
    canonical_h2_order=[
        "whats-true",
        "talk-track",
        "follow-up-questions",
        "fit-and-route-boundary",
    ],
    aliases={
        "objection": "whats-true",
        "whats-actually-true": "whats-true",
        "talk-track": "talk-track",
        "follow-up-questions": "follow-up-questions",
        "related-context": "fit-and-route-boundary",
    },
    source_coverage_required=False,
    notes="Source Coverage is used when the response references customer-specific evidence. It is not required for generic snippets.",
)

# ---------------------------------------------------------------------------
# Specialized / non-report exceptions
# ---------------------------------------------------------------------------

_FOLLOW_UP_EMAIL = SkillArchitecture(
    skill="follow-up-email",
    profile="Specialized output such as email",
    top_summary_name="Email",
    canonical_h2_order=[],
    structured_exception=True,
    source_coverage_required=False,
    notes="Email is a specialized artifact; do not force Markdown report architecture.",
)

_FULL_QUAL = SkillArchitecture(
    skill="full-qual",
    profile="Specialized orchestration",
    top_summary_name="",
    canonical_h2_order=[],
    structured_exception=True,
    source_coverage_required=False,
    notes="full-qual orchestrates biz-qual and tech-qual; it does not emit a standalone Markdown report.",
)

_POV_GSHEET = SkillArchitecture(
    skill="pov-gsheet",
    profile="Specialized output such as sheet",
    top_summary_name="Receipt",
    canonical_h2_order=["receipt", _SOURCE_COVERAGE],
    source_coverage_required=True,
    notes="Sheet artifact with a lightweight receipt and source coverage; not a normal report.",
)

_WORKER_ANALYSIS = SkillArchitecture(
    skill="worker-analysis",
    profile="Specialized output such as worker-analysis report",
    top_summary_name="",
    canonical_h2_order=[],
    structured_exception=True,
    source_coverage_required=False,
    notes="Worker analysis is a specialized report with its own page architecture.",
)

CANONICAL_ARCHITECTURE: dict[str, SkillArchitecture] = {
    arch.skill: arch
    for arch in [
        _PREP_CALL,
        _POST_CALL,
        _BIZ_QUAL,
        _TECH_QUAL,
        _DEPLOYMENT_QUAL,
        _CONNECTOR_FEASIBILITY,
        _DEAL_ASSESSMENT,
        _POC_PLAN,
        _ROI_BUSINESS_CASE,
        _MUTUAL_CLOSE_PLAN,
        _ACCOUNT_REFRESHER,
        _NEXT_MOVE,
        _INTERNAL_PREP,
        _COVERAGE_HANDOFF,
        _OBJECTION_HANDLER,
        _FOLLOW_UP_EMAIL,
        _FULL_QUAL,
        _POV_GSHEET,
        _WORKER_ANALYSIS,
    ]
}


def get_architecture(skill: str) -> SkillArchitecture | None:
    return CANONICAL_ARCHITECTURE.get(skill)
