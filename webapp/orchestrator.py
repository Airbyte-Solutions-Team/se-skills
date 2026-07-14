"""Deterministic prerequisite checker / planner for the SE skill workflow.

Uses the structured output sidecars introduced by STRUCT-003 to decide whether a
selected skill has the upstream artifacts it needs. The planner is advisory by
default: it returns `ready` and a `missing` list, and callers can enforce a
one-click override (`can_override` is always `True` unless the request is
free-form or otherwise unplannable).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

import output_schema

logger = logging.getLogger(__name__)


class UpstreamStatus(BaseModel):
    """Status of one upstream skill's most recent output."""

    skill: str
    status: str = "missing"  # valid | invalid | unvalidated | missing
    path: str | None = None
    mtime: float | None = None
    validation_errors: list[str] = Field(default_factory=list)
    validation_status: str = "unvalidated"


class PlanResult(BaseModel):
    """Result of a prerequisite check for one skill invocation."""

    skill: str
    ready: bool
    can_override: bool = True
    missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    upstream: dict[str, UpstreamStatus] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Skill prerequisite rules
# ---------------------------------------------------------------------------
# These mirror the "Skill Sequencing Rules" in skills/_se-playbook.md. They are
# intentionally conservative: we only block when the playbook explicitly says a
# downstream skill needs upstream data, and we always allow an override.
#
# Legend:
# - "transcript"  : at least one transcript must exist for the account.
# - "upstream"    : the listed upstream skills must have a *valid* output.
# - "full-qual" is a convenience wrapper that runs biz-qual + tech-qual, so it
#   only needs a transcript (it produces the upstream docs itself).
# ---------------------------------------------------------------------------
SKILL_PREREQUISITES: dict[str, list[dict]] = {
    "prep-call": [],
    "post-call": [{"kind": "transcript"}],
    "deployment-model-qual": [{"kind": "transcript"}],
    "biz-qual": [{"kind": "transcript"}],
    "deal-assessment": [{"kind": "transcript"}],
    "tech-qual": [
        {"kind": "transcript"},
        {"kind": "upstream", "skills": ["biz-qual"], "require": "valid"},
    ],
    "connector-feasibility": [
        {"kind": "upstream", "skills": ["tech-qual"], "require": "valid"},
    ],
    "poc-plan": [
        {"kind": "upstream", "skills": ["biz-qual", "tech-qual"], "require": "valid"},
    ],
    "full-qual": [{"kind": "transcript"}],
    "roi-business-case": [
        {"kind": "upstream", "skills": ["poc-plan"], "require": "valid"},
    ],
    "mutual-close-plan": [
        {"kind": "upstream", "skills": ["roi-business-case"], "require": "valid"},
    ],
    # Anytime / router skills have no hard prerequisites.
    "account-refresher": [],
    "follow-up-email": [],
    "objection-handler": [],
    "internal-prep": [],
    "coverage-handoff": [],
    "next-move": [],
}


def _titlecase_folder(name: str) -> str:
    """Title-Case-Hyphenated, matching the workspace convention (e.g. Build-Manufacturing)."""
    return "-".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name.strip()) if part)


def _output_dir(customers_dir: Path, account: str, skill: str, opp_slug: str | None = None) -> Path | None:
    """Return the outputs directory for a skill, or None if it does not exist."""
    if opp_slug:
        d = customers_dir / account / "opportunities" / opp_slug / "outputs" / skill
    else:
        d = customers_dir / account / "outputs" / skill
    return d if d.exists() else None


def _latest_output(
    customers_dir: Path,
    account: str,
    skill: str,
    opp_slug: str | None = None,
) -> output_schema.OutputMetadata | None:
    """Return the parsed sidecar metadata for the most recent Markdown output.

    Checks both account-level and opportunity-level outputs.
    """
    candidates: list[Path] = []
    for slug in (opp_slug, None):
        d = _output_dir(customers_dir, account, skill, slug)
        if d:
            candidates.extend(d.glob("*.md"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return output_schema.read_or_parse_sidecar(latest, skill)
    except (OSError, ValueError, TypeError):
        logger.warning("Failed to parse sidecar for %s", latest)
        return None


def _has_transcript(customers_dir: Path, account: str) -> bool:
    """True if at least one transcript exists for this account."""
    tdir = customers_dir / "_transcripts"
    if not tdir.exists():
        return False
    cust = _titlecase_folder(account)
    for f in tdir.iterdir():
        if f.is_file() and f.suffix in (".txt", ".md") and f.name.startswith(f"{cust}-"):
            return True
    return False


def _check_upstream(
    customers_dir: Path,
    account: str,
    opp_slug: str | None,
    skill: str,
    require: str,
) -> tuple[bool, list[str], UpstreamStatus | None]:
    """Check whether a single upstream skill has a usable output.

    `require` is currently always "valid" — we keep the parameter so future
    rules can accept "present" if needed.
    """
    meta = _latest_output(customers_dir, account, skill, opp_slug)
    if meta is None:
        return False, [f"Missing upstream `{skill}` output."], None

    status = UpstreamStatus(
        skill=skill,
        status=meta.validation_status,
        path=meta.title,
        validation_errors=meta.validation_errors,
        validation_status=meta.validation_status,
    )

    if require == "valid" and meta.validation_status != "valid":
        if meta.validation_status == "invalid":
            return (
                False,
                [f"Upstream `{skill}` output is invalid: {meta.validation_errors[0] if meta.validation_errors else 'unknown error'}."],
                status,
            )
        return (
            False,
            [f"Upstream `{skill}` output could not be validated (status: {meta.validation_status})."],
            status,
        )

    return True, [], status


def check_prerequisites(
    skill: str,
    account: str,
    opp_slug: str | None,
    customers_dir: Path,
) -> PlanResult:
    """Return a plan result for running `skill` against `account`/`opp_slug`.

    Unknown skills are treated as having no prerequisites (they may be free-form
    or newly added). The result always sets `can_override=True` so the UI can
    present a "Run anyway" option.
    """
    rules = SKILL_PREREQUISITES.get(skill, [])
    missing: list[str] = []
    warnings: list[str] = []
    upstream: dict[str, UpstreamStatus] = {}

    for rule in rules:
        kind = rule["kind"]
        if kind == "transcript":
            if not _has_transcript(customers_dir, account):
                missing.append("At least one customer transcript is required.")
        elif kind == "upstream":
            for uskill in rule.get("skills", []):
                ok, msgs, status = _check_upstream(
                    customers_dir, account, opp_slug, uskill, rule.get("require", "valid")
                )
                if status:
                    upstream[uskill] = status
                if not ok:
                    missing.extend(msgs)

    ready = not missing
    return PlanResult(
        skill=skill,
        ready=ready,
        can_override=True,
        missing=missing,
        warnings=warnings,
        upstream=upstream,
    )
