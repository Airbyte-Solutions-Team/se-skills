"""Typed contract for Command Center reconciliation (PR C of `docs/COMMAND_CENTER.md`).

Durable operational objects derived from an associated, authorized source
revision: trusted-layer `Observation`s, first-class `ActionRecord`s with an
append-only transition history, deterministic `ChangeEntry`s, and
`ReconciliationRun` receipts. `recommended_actions` in the Opportunity Overview
stay suggestions; only the policy in `services.command_center_operations_service`
turns an observation into an action, and a human transition always wins over a
later analysis. No record here carries transcript, summary, or note bodies.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from command_center_evidence import HEX16, SAFE_TOKEN, SOURCE_ID, canonical_bytes, sha256_hex


SCHEMA_VERSION = 1

ACTION_ID = re.compile(r"^act_[a-f0-9]{32}$")
RUN_ID = re.compile(r"^run_[a-f0-9]{32}$")
CHANGE_ID = re.compile(r"^chg_[a-f0-9]{32}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ResponsibleParty = Literal["Airbyte", "Customer", "Engineering", "Security", "Partner", "Unknown"]
DurableActionStatus = Literal["proposed", "open", "blocked", "completed", "dismissed"]
ObservationKind = Literal["commitment", "completion_suggestion"]
Actor = Literal["analysis", "user"]
ChangeType = Literal[
    "overview_revision",
    "action_created",
    "action_linked",
    "action_transition",
    "completion_suggested",
    "possible_duplicate_flagged",
    "association_corrected",
    "evidence_retracted",
    "completion_suggestion_retracted",
    "overview_reverted",
    "overview_retired",
]
RunStatus = Literal["succeeded", "failed", "stale_base", "superseded", "interrupted", "apply_incomplete"]
Attribution = Literal["source_verified", "model_only"]

_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)

PARTY_KEYWORDS: dict[str, ResponsibleParty] = {
    "airbyte": "Airbyte",
    "customer": "Customer",
    "engineering": "Engineering",
    "security": "Security",
    "partner": "Partner",
}

# Human transitions the pilot accepts. Reopening is explicit and only from a
# terminal state; `proposed -> open` is the user accepting a review candidate.
HUMAN_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("proposed", "open"),
    ("proposed", "dismissed"),
    ("open", "blocked"),
    ("open", "completed"),
    ("open", "dismissed"),
    ("blocked", "open"),
    ("blocked", "completed"),
    ("blocked", "dismissed"),
    ("completed", "open"),
    ("dismissed", "open"),
})

ShortText = Field(min_length=1, max_length=300)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def normalize_text(value: str) -> str:
    """Case/whitespace/punctuation-insensitive form used for observation identity."""
    lowered = value.casefold()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return " ".join(lowered.split())


def derive_party(owner: str | None) -> ResponsibleParty:
    """Only a literal party word in the owner text resolves a party; anything else is Unknown."""
    if not owner:
        return "Unknown"
    words = set(normalize_text(owner).split())
    matches = {party for word, party in PARTY_KEYWORDS.items() if word in words}
    if len(matches) == 1:
        return matches.pop()
    return "Unknown"


def date_renderings(iso_date: str) -> set[str]:
    """Normalized spellings a note could plausibly use for an ISO date."""
    year, month, day = (int(part) for part in iso_date.split("-"))
    if not 1 <= month <= 12:
        return {normalize_text(iso_date)}
    name = _MONTHS[month - 1]
    short = name[:3]
    forms = {
        iso_date, f"{month}/{day}/{year}", f"{month:02d}/{day:02d}/{year}", f"{day}/{month}/{year}",
        f"{name} {day}", f"{name} {day} {year}", f"{day} {name}", f"{day} {name} {year}",
        f"{short} {day}", f"{short} {day} {year}", f"{day} {short}", f"{day} {short} {year}",
    }
    return {normalize_text(form) for form in forms}


_COMMITMENT_STOPWORDS = frozenset({
    "the", "and", "for", "with", "will", "our", "their", "them", "this", "that", "from", "into",
    "about", "over", "before", "after", "should", "would", "could", "must", "need", "needs",
    "customer", "airbyte", "team", "please",
})


def commitment_terms(commitment: str) -> set[str]:
    """Content words of a commitment that a source passage must contain to attribute it."""
    return {
        token for token in normalize_text(commitment).split()
        if len(token) >= 3 and token not in _COMMITMENT_STOPWORDS and not token.isdigit()
    }


def source_passages(source_text: str) -> list[str]:
    """Attributable passages of a rendered note: one per non-heading line, split at sentence ends.

    Heading/metadata lines (`# title`, `Occurred:`, `Attendees:`) are excluded so an
    attendee list can never supply the owner of a commitment.
    """
    passages: list[str] = []
    for raw in source_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(("Occurred:", "Attendees:")):
            continue
        line = re.sub(r"^\[[^\]]{1,80}\]\s*", "", line)  # speaker label is not part of the passage
        for sentence in re.split(r"(?<=[.!?;])\s+", line):
            normalized = normalize_text(sentence)
            if normalized:
                passages.append(normalized)
    return passages


def verify_attribution(
    source_text: str, *, owner: str | None, due_date: str | None, commitment: str | None = None
) -> Attribution:
    """`source_verified` only when one passage of the source carries the owner, the due date and the commitment.

    The model's owner/due/action fields are never taken as a customer commitment on
    their own: an attendee name somewhere plus a date mentioned for another matter
    does not attribute a promise the note never made.
    """
    if not owner or not due_date:
        return "model_only"
    owner_norm = f" {normalize_text(owner)} "
    dates = {f" {form} " for form in date_renderings(due_date)}
    terms = commitment_terms(commitment) if commitment is not None else set()
    if commitment is not None and not terms:
        return "model_only"
    for passage in source_passages(source_text):
        haystack = f" {passage} "
        if owner_norm not in haystack or not any(form in haystack for form in dates):
            continue
        words = set(passage.split())
        if terms <= words:
            return "source_verified"
    return "model_only"


class SourceRef(_Strict):
    source_id: str = Field(pattern=SOURCE_ID.pattern)
    revision: int = Field(ge=1, le=1_000_000)
    evidence_id: str = Field(min_length=8, max_length=160)
    locator: str | None = Field(default=None, max_length=200)


class Observation(_Strict):
    """Trusted-layer derivation from one source revision; the key is not model-authored."""

    observation_key: str = Field(pattern=HEX16.pattern)
    kind: ObservationKind
    commitment: str = ShortText
    definition_of_done: str | None = Field(default=None, max_length=2_000)
    party: ResponsibleParty
    owner: str | None = Field(default=None, max_length=200)
    due_date: str | None = Field(default=None, pattern=DATE.pattern)
    explicit: bool
    attribution: Attribution = "model_only"
    source: SourceRef

    @staticmethod
    def key_for(*, source_id: str, kind: str, commitment: str, party: str) -> str:
        payload = {
            "source_id": source_id,
            "kind": kind,
            "commitment": normalize_text(commitment),
            "party": party,
        }
        return sha256_hex(canonical_bytes(payload))[:16]


class ActionTransition(_Strict):
    sequence: int = Field(ge=1, le=10_000)
    from_status: DurableActionStatus | None
    to_status: DurableActionStatus
    actor: Actor
    actor_id: str = Field(pattern=SAFE_TOKEN.pattern)
    reason: str = ShortText
    source: SourceRef | None = None
    undoes_sequence: int | None = Field(default=None, ge=1)
    prior_owner: str | None = Field(default=None, max_length=200)
    prior_due_date: str | None = Field(default=None, pattern=DATE.pattern)
    recorded_at: datetime


class CompletionSuggestion(_Strict):
    source: SourceRef
    observation_key: str = Field(pattern=HEX16.pattern)
    recorded_at: datetime
    retracted_at: datetime | None = None


class EvidenceRetraction(_Strict):
    """Evidence from a source that is no longer authorized for this opportunity; the ref stays in history."""

    source_id: str = Field(pattern=SOURCE_ID.pattern)
    reason: str = ShortText
    recorded_at: datetime


class Retraction(_Strict):
    """Derived state superseded by an association correction; history is kept."""

    from_account: str = Field(pattern=SAFE_TOKEN.pattern)
    from_opportunity_slug: str = Field(pattern=SAFE_TOKEN.pattern)
    to_account: str | None = Field(default=None, pattern=SAFE_TOKEN.pattern)
    to_opportunity_slug: str | None = Field(default=None, pattern=SAFE_TOKEN.pattern)
    reason: str = ShortText
    recorded_at: datetime


class ActionRecord(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    action_id: str = Field(pattern=ACTION_ID.pattern)
    workspace_id: str = Field(pattern=HEX16.pattern)
    account: str = Field(pattern=SAFE_TOKEN.pattern)
    opportunity_slug: str = Field(pattern=SAFE_TOKEN.pattern)
    commitment: str = ShortText
    definition_of_done: str | None = Field(default=None, max_length=2_000)
    party: ResponsibleParty
    owner: str | None = Field(default=None, max_length=200)
    due_date: str | None = Field(default=None, pattern=DATE.pattern)
    status: DurableActionStatus
    origin: SourceRef
    observation_key: str = Field(pattern=HEX16.pattern)
    evidence: list[SourceRef] = Field(min_length=1, max_length=200)
    transitions: list[ActionTransition] = Field(min_length=1, max_length=10_000)
    completion_suggestions: list[CompletionSuggestion] = Field(default_factory=list, max_length=200)
    possible_duplicate_of: str | None = Field(default=None, pattern=ACTION_ID.pattern)
    retraction: Retraction | None = None
    evidence_retractions: list[EvidenceRetraction] = Field(default_factory=list, max_length=200)
    created_at: datetime
    updated_at: datetime

    @property
    def human_touched(self) -> bool:
        return any(item.actor == "user" for item in self.transitions)

    @property
    def retracted_source_ids(self) -> frozenset[str]:
        return frozenset(item.source_id for item in self.evidence_retractions)

    @property
    def effective_evidence(self) -> list[SourceRef]:
        retracted = self.retracted_source_ids
        return [ref for ref in self.evidence if ref.source_id not in retracted]

    @model_validator(mode="after")
    def validate_chain(self) -> "ActionRecord":
        expected = 1
        previous: str | None = None
        for item in self.transitions:
            if item.sequence != expected:
                raise ValueError("transition sequence must be contiguous")
            if item.from_status != previous:
                raise ValueError("transition chain is broken")
            previous = item.to_status
            expected += 1
        if previous != self.status:
            raise ValueError("status must equal the last transition")
        return self


class ChangeEntry(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    change_id: str = Field(pattern=CHANGE_ID.pattern)
    sequence: int = Field(ge=1)
    workspace_id: str = Field(pattern=HEX16.pattern)
    account: str = Field(pattern=SAFE_TOKEN.pattern)
    opportunity_slug: str = Field(pattern=SAFE_TOKEN.pattern)
    change_type: ChangeType
    subject_id: str = Field(min_length=1, max_length=160)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    source: SourceRef | None = None
    actor: Actor
    actor_id: str = Field(pattern=SAFE_TOKEN.pattern)
    occurred_at: datetime
    applied_at: datetime
    link: str = Field(min_length=1, max_length=300)


class ReconciliationRun(_Strict):
    """Receipt for one processing attempt of one source revision against one base."""

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str = Field(pattern=RUN_ID.pattern)
    workspace_id: str = Field(pattern=HEX16.pattern)
    source_id: str = Field(pattern=SOURCE_ID.pattern)
    revision: int = Field(ge=1, le=1_000_000)
    account: str = Field(pattern=SAFE_TOKEN.pattern)
    opportunity_slug: str = Field(pattern=SAFE_TOKEN.pattern)
    base_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    base_revision: int | None = Field(default=None, ge=1, le=1_000_000)
    status: RunStatus
    error_code: str | None = Field(default=None, max_length=80)
    promoted_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    promoted_revision: int | None = Field(default=None, ge=1, le=1_000_000)
    observation_count: int = Field(default=0, ge=0)
    actions_created: list[str] = Field(default_factory=list, max_length=200)
    actions_linked: list[str] = Field(default_factory=list, max_length=200)
    completion_suggestions: list[str] = Field(default_factory=list, max_length=200)
    possible_duplicates: list[str] = Field(default_factory=list, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    runtime: str | None = Field(default=None, max_length=200)
    started_at: datetime
    finished_at: datetime


class PendingApplication(_Strict):
    """Written before an Overview promotion so a crash after it can be completed deterministically.

    Everything needed to finish (actions, changes, processed mark, receipt) is
    re-derived from the promoted version; the model is never rerun on resume.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    workspace_id: str = Field(pattern=HEX16.pattern)
    source_id: str = Field(pattern=SOURCE_ID.pattern)
    revision: int = Field(ge=1, le=1_000_000)
    account: str = Field(pattern=SAFE_TOKEN.pattern)
    opportunity_slug: str = Field(pattern=SAFE_TOKEN.pattern)
    base_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    base_revision: int | None = Field(default=None, ge=1, le=1_000_000)
    association_sequence: int | None = Field(default=None, ge=1)
    evidence_id: str = Field(min_length=8, max_length=160)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: datetime
    started_at: datetime


def action_id_for(*, workspace_id: str, account: str, opportunity_slug: str, source_id: str, observation_key: str) -> str:
    """Deterministic: the same observation from the same source is the same action."""
    payload = {
        "workspace_id": workspace_id,
        "account": account,
        "opportunity_slug": opportunity_slug,
        "source_id": source_id,
        "observation_key": observation_key,
    }
    return "act_" + sha256_hex(canonical_bytes(payload))[:32]
