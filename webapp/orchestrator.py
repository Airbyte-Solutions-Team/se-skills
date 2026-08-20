"""Deterministic prerequisite checker / planner for the SE skill workflow.

Uses the structured output sidecars introduced by STRUCT-003 to decide whether a
selected skill has the upstream artifacts it needs. The planner is advisory by
default: it returns `ready` and a `missing` list, and callers can enforce a
one-click override (`can_override` is always `True` unless the request is
free-form or otherwise unplannable).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

import output_schema

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

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
    choices: list[dict[str, Any]] = Field(default_factory=list)
    upstream: dict[str, UpstreamStatus] = Field(default_factory=dict)
    modes: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Skill prerequisite rules
# ---------------------------------------------------------------------------
# These mirror the "Skill Sequencing Rules" in skills/_se-playbook.md. The
# planner reports deterministic local facts, while source-resolvable conditions
# remain advisory so the skill can resolve Gong or other sources itself.
#
# Legend:
# - "transcript"  : check for a local transcript and optionally warn about a
#   skill-level source fallback.
# - "upstream"    : the listed upstream skills must have a *valid* output.
# - "advisory_upstream": report upstream status and warn, but never block.
# - "full-qual" is a convenience wrapper that runs biz-qual + tech-qual, so it
#   only needs a transcript (it produces the upstream docs itself).
# ---------------------------------------------------------------------------
SKILL_PREREQUISITES: dict[str, list[dict]] = {
    "prep-call": [],
    "post-call": [{"kind": "transcript", "fallback": "gong"}],
    "deployment-model-qual": [{
        "kind": "transcript",
        "warning": (
            "No local transcript found. Deployment Model Qual will check Gong for "
            "customer calls; it needs customer answers to the 5 deployment "
            "questions (or answers you provide directly) and will stop if none are available."
        ),
    }],
    "biz-qual": [{
        "kind": "transcript",
        "warning": (
            "No local transcript found. Biz Qual will check Gong for customer "
            "calls before qualification and will stop if no customer voice is available."
        ),
    }],
    "deal-assessment": [{"kind": "local_evidence"}],
    "tech-qual": [{
        "kind": "transcript",
        "warning": (
            "No local transcript found. Tech Qual will check Gong for calls with "
            "technical discovery and will stop if no technical discovery is available."
        ),
    }],
    "connector-feasibility": [],
    "poc-plan": [
        {
            "kind": "transcript",
            "warning": (
                "No local transcript found. POC Plan will check Gong; it refuses "
                "only if no customer voice exists in any source."
            ),
        },
        {
            "kind": "advisory_upstream",
            "skills": ["biz-qual", "tech-qual"],
            "require": "valid",
            "warning_prefix": "Missing qualification doc(s):",
            "warning_suffix": (
                "POC Plan will offer to run them first, or proceed with a "
                "scope-drift warning if you skip."
            ),
        },
    ],
    "full-qual": [{
        "kind": "transcript",
        "warning": (
            "No local transcript found. Full Qual will check Gong; biz-qual and "
            "tech-qual each apply their own source requirements and report partial "
            "completion if one refuses."
        ),
    }],
    "roi-business-case": [
        {
            "kind": "transcript",
            "warning": (
                "No local transcript found. ROI Business Case will check Gong; it "
                "requires customer-stated inputs and will stop if none exist."
            ),
        },
        {
            "kind": "advisory_upstream",
            "skills": ["biz-qual"],
            "require": "valid",
            "warning": (
                "No biz-qual found — its Metrics are the inputs to the ROI case; "
                "the skill will offer to run it first."
            ),
        },
    ],
    "mutual-close-plan": [
        {
            "kind": "transcript",
            "warning": (
                "No local transcript found. Mutual Close Plan will check Gong; it "
                "requires the customer's actual buying process and will stop if no "
                "customer voice exists."
            ),
        },
        {
            "kind": "advisory_upstream",
            "skills": ["biz-qual"],
            "require": "valid",
            "warning": (
                "No biz-qual found — its Paper Process and Economic Buyer sections "
                "are the backbone of a close plan; the skill will offer to run it first."
            ),
        },
    ],
    # Anytime / router skills have no hard prerequisites.
    "account-refresher": [],
    "follow-up-email": [],
    "objection-handler": [],
    "internal-prep": [],
    "coverage-handoff": [],
    "next-move": [],
    "worker-analysis": [{"kind": "worker_config"}],
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
) -> tuple[Path | None, output_schema.OutputMetadata | None]:
    """Return the parsed sidecar metadata for the most recent Markdown output.

    Checks both account-level and opportunity-level outputs.
    """
    candidates: list[Path] = []
    for slug in (opp_slug, None):
        d = _output_dir(customers_dir, account, skill, slug)
        if d:
            candidates.extend(d.glob("*.md"))
    if not candidates:
        return None, None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return latest, output_schema.read_or_parse_sidecar(latest, skill)
    except (OSError, ValueError, TypeError):
        logger.warning("Failed to parse sidecar for %s", latest)
        return latest, None


def _has_transcript(customers_dir: Path, account: str) -> bool:
    """True if at least one transcript exists for this account."""
    tdir = customers_dir / "_transcripts"
    if not tdir.exists():
        return False
    cust = _titlecase_folder(account)
    for f in tdir.iterdir():
        if f.is_file() and f.suffix in (".txt", ".md", ".rtf") and f.name.startswith(f"{cust}-"):
            return True
    return False


def _has_qualification_doc(
    customers_dir: Path,
    account: str,
    opp_slug: str | None,
) -> bool:
    """Return whether selected/account-level qualification evidence is usable."""
    roots = [customers_dir / account / "outputs"]
    if opp_slug:
        roots.insert(0, customers_dir / account / "opportunities" / opp_slug / "outputs")

    for root in roots:
        for skill in ("biz-qual", "tech-qual", "deployment-qual", "deployment-model-qual"):
            skill_dir = root / skill
            if not skill_dir.is_dir():
                continue
            for path in skill_dir.glob("*.md"):
                if _qualification_doc_usable(path):
                    return True
    return False


def _qualification_doc_usable(path: Path) -> bool:
    """Return whether a qualification Markdown file has usable document shape."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(text.strip()) and any(line.startswith("#") for line in text.splitlines())


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
    latest, meta = _latest_output(customers_dir, account, skill, opp_slug)
    if meta is None:
        return (
            False,
            [f"Missing upstream `{skill}` output."],
            UpstreamStatus(skill=skill),
        )

    status = UpstreamStatus(
        skill=skill,
        status=meta.validation_status,
        path=str(latest) if latest else None,
        mtime=latest.stat().st_mtime if latest else None,
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


def _load_worker_analysis_config(customers_dir: Path) -> dict[str, Any]:
    """Load the `worker_analysis` block from .se-config.yaml (or an env override)."""
    cfg: dict[str, Any] = {}

    # Prefer a config file in the workspace root (parent of customers/)
    candidates = [
        customers_dir.parent / ".se-config.yaml",
        Path.home() / ".se-skills" / ".se-config.yaml",
        Path.home() / "airbyte-work" / ".se-config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists() and yaml is not None:
            try:
                raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    cfg = raw.get("worker_analysis", {}) or {}
                    break
            except (OSError, ValueError):
                pass

    return cfg


def _check_worker_analysis_modes(customers_dir: Path) -> dict[str, Any]:
    """Return mode-aware readiness for worker-analysis.

    Questionnaire mode is always available. Workspace/API and Metabase modes depend
    on credentials/config; Datadog is optional. Missing optional dependencies are
    reported as warnings so the UI can still launch questionnaire mode.
    """
    cfg = _load_worker_analysis_config(customers_dir)

    modes: dict[str, Any] = {
        "questionnaire": {"ready": True, "required": False, "missing": []},
        "workspace": {"ready": False, "required": False, "missing": []},
        "metabase": {"ready": False, "required": False, "missing": []},
        "datadog": {"ready": False, "required": False, "missing": []},
    }

    client_id = cfg.get("airbyte_cloud_client_id") or os.environ.get("AIRBYTE_CLOUD_CLIENT_ID")
    client_secret = cfg.get("airbyte_cloud_client_secret") or os.environ.get("AIRBYTE_CLOUD_CLIENT_SECRET")
    if client_id and client_secret:
        modes["workspace"]["ready"] = True
    else:
        modes["workspace"]["missing"].append(
            "Airbyte Cloud client id/secret are required for workspace analysis. "
            "Set them in .se-config.yaml worker_analysis or env AIRBYTE_CLOUD_CLIENT_ID / AIRBYTE_CLOUD_CLIENT_SECRET."
        )

    bigquery_project = cfg.get("bigquery_project")
    if bigquery_project and bigquery_project != "<your-bigquery-project>":
        modes["metabase"]["ready"] = True
    else:
        modes["metabase"]["missing"].append(
            "BigQuery project/dataset are required for Metabase billing analysis. "
            "Set them in .se-config.yaml worker_analysis."
        )

    datadog_url = cfg.get("datadog_dashboard_url")
    if datadog_url:
        modes["datadog"]["ready"] = True
    else:
        modes["datadog"]["missing"].append(
            "Datadog dashboard URL is optional; set worker_analysis.datadog_dashboard_url to include deep links."
        )

    return modes


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
    choices: list[dict[str, Any]] = []
    upstream: dict[str, UpstreamStatus] = {}

    modes: dict[str, Any] | None = None
    for rule in rules:
        kind = rule["kind"]
        if kind == "transcript":
            if not _has_transcript(customers_dir, account):
                if rule.get("warning"):
                    warnings.append(rule["warning"])
                elif rule.get("fallback") == "gong":
                    warnings.append(
                        "No local transcript found for this account — post-call will search Gong "
                        "for the most recent completed call and save it to _transcripts/ before analysis."
                    )
                else:
                    missing.append("At least one customer transcript is required.")
        elif kind == "local_evidence":
            has_transcript = _has_transcript(customers_dir, account)
            has_qualification_doc = _has_qualification_doc(customers_dir, account, opp_slug)
            if not has_transcript:
                warnings.append(
                    "No local transcript found; using prior qualification doc(s) as source. "
                    "Deal Assessment will flag thin sources."
                    if has_qualification_doc
                    else (
                        "No local transcript or qualification doc found. Deal Assessment will "
                        "check Gong for customer calls before assessing and will stop if no "
                        "customer evidence is available."
                    )
                )
        elif kind == "worker_config":
            modes = _check_worker_analysis_modes(customers_dir)
            if modes:
                if not modes["workspace"]["ready"]:
                    warnings.append("Workspace/API analysis is unavailable without Airbyte Cloud credentials.")
                if not modes["metabase"]["ready"]:
                    warnings.append("Metabase billing analysis is unavailable without a configured BigQuery project.")
                if not modes["datadog"]["ready"]:
                    warnings.append("Datadog deep links are unavailable without a configured dashboard URL.")
        elif kind == "upstream":
            for uskill in rule.get("skills", []):
                ok, msgs, status = _check_upstream(
                    customers_dir, account, opp_slug, uskill, rule.get("require", "valid")
                )
                if status:
                    upstream[uskill] = status
                if not ok:
                    missing.extend(msgs)
        elif kind == "advisory_upstream":
            missing_skills: list[str] = []
            for uskill in rule.get("skills", []):
                ok, _msgs, status = _check_upstream(
                    customers_dir, account, opp_slug, uskill, rule.get("require", "valid")
                )
                if status:
                    upstream[uskill] = status
                if not ok and (
                    rule.get("warn_if", "missing_or_invalid") == "missing_or_invalid"
                    or status is None
                    or status.status == "missing"
                ):
                    missing_skills.append(uskill)
            if missing_skills:
                if rule.get("warning"):
                    message = rule["warning"]
                else:
                    names = ", ".join(sorted(missing_skills))
                    message = (
                        f"{rule.get('warning_prefix', 'Missing upstream output(s):')} "
                        f"{names}. {rule.get('warning_suffix', '')}".strip()
                    )
                ordered_skills = sorted(missing_skills)
                choices.append({
                    "id": "run-quals:" + ",".join(ordered_skills),
                    "skills": ordered_skills,
                    "message": message,
                })

    ready = not missing
    return PlanResult(
        skill=skill,
        ready=ready,
        can_override=True,
        missing=missing,
        warnings=warnings,
        choices=choices,
        upstream=upstream,
        modes=modes,
    )
