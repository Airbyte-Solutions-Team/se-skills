"""Pydantic schemas and Markdown extraction for SE skill outputs.

Each saving skill is expected to follow the shared output-document format in
`skills/_se-playbook.md` (H1 title, one-line meta, `### At a Glance`, H2 body
sections, `## Source Coverage` last). This module turns a generated `.md` file
into a typed `OutputMetadata` sidecar and reports which required sections are
missing so the UI can warn the SE when an output looks incomplete.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from itertools import zip_longest
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from reference_freshness import ReferenceChange, ReferenceFreshness, compute_reference_freshness

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class SkillOutputSchema(BaseModel):
    """Schema definition for one skill's Markdown output."""

    skill: str
    required_sections: list[str] = Field(default_factory=list)
    required_at_a_glance_labels: list[str] = Field(default_factory=list)


class OutputMetadata(BaseModel):
    """Typed sidecar for a generated skill output."""

    skill: str
    title: str | None = None
    date: str | None = None
    at_a_glance: dict[str, str] = Field(default_factory=dict)
    sections: dict[str, str] = Field(default_factory=dict)
    required_sections: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    valid: bool = True
    schema_version: int = SCHEMA_VERSION
    validation_status: str = "unvalidated"  # "valid" | "invalid" | "unvalidated"
    reference_freshness_at_generation: list[ReferenceFreshness] | None = None
    reference_changed_since_generation: list[ReferenceChange] | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_reference_freshness(cls, data: Any) -> Any:
        """Old sidecars stored the snapshot as `reference_freshness`; migrate it."""
        if isinstance(data, dict) and "reference_freshness_at_generation" not in data:
            legacy = data.get("reference_freshness")
            if isinstance(legacy, list):
                data = dict(data)
                data["reference_freshness_at_generation"] = legacy
                data["reference_changed_since_generation"] = []
        return data


# ---------------------------------------------------------------------------
# Per-skill required sections (normalized H2 heading keys).
# These mirror the "Jump to" indices in each skill's SKILL.md.
# ---------------------------------------------------------------------------

_SKILL_SCHEMAS: dict[str, SkillOutputSchema] = {
    # Core = the decision-critical sections and At a Glance labels that must be
    # present even in brief mode. Extended sections are still parsed and stored,
    # but missing them does not fail validation.
    "biz-qual": SkillOutputSchema(
        skill="biz-qual",
        required_sections=["meddpicc-scorecard", "source-coverage"],
        required_at_a_glance_labels=["overall", "recommended-motion"],
    ),
    "tech-qual": SkillOutputSchema(
        skill="tech-qual",
        required_sections=["technical-fit-summary", "source-coverage"],
        required_at_a_glance_labels=["technical-fit", "primary-risk"],
    ),
    "deployment-model-qual": SkillOutputSchema(
        skill="deployment-model-qual",
        required_sections=["verdict", "source-coverage"],
        required_at_a_glance_labels=["verdict", "recommended-motion"],
    ),
    "poc-plan": SkillOutputSchema(
        skill="poc-plan",
        required_sections=["success-criteria", "source-coverage"],
        required_at_a_glance_labels=["poc-proves", "timeline", "success-criteria"],
    ),
    "connector-feasibility": SkillOutputSchema(
        skill="connector-feasibility",
        required_sections=["fit-verdict", "source-coverage"],
        required_at_a_glance_labels=["feasibility", "recommended-motion"],
    ),
    "pov-gsheet": SkillOutputSchema(
        skill="pov-gsheet",
        required_sections=["receipt", "source-coverage"],
        required_at_a_glance_labels=["google-sheet-url", "status"],
    ),
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize_heading(text: str) -> str:
    """Convert a Markdown heading to a stable section key."""
    key = text.strip().lower()
    key = re.sub(r"&", "and", key)
    key = re.sub(r"[^a-z0-9]+", "-", key)
    key = key.strip("-")
    return key


def _normalize_label(text: str) -> str:
    """Normalize an At-a-Glance label to a lookup key."""
    key = text.strip().lower()
    key = re.sub(r"&", "and", key)
    key = re.sub(r"[^a-z0-9]+", "-", key)
    key = key.strip("-")
    return key


def _extract_title(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _extract_date(text: str) -> str | None:
    """Pull the `**Date:** ...` value from the title meta line."""
    for line in text.splitlines()[:10]:
        match = re.search(r"\*\*Date:\*\*\s*([^·\n]+)", line)
        if match:
            return match.group(1).strip()
    return None


def _strip_markup_inline(text: str) -> str:
    """Remove lightweight Markdown emphasis so raw values are readable."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"==(.+?)==", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return text.strip()


def _split_meta_chunks(line: str) -> list[tuple[str, str]]:
    """Parse one At-a-Glance bullet that may contain multiple `**Label:** value` chunks."""
    chunks: list[tuple[str, str]] = []
    # Remove leading dash
    line = re.sub(r"^\s*[-*]\s+", "", line)
    parts = re.split(r"\s*·\s*", line)
    for part in parts:
        # Allow the colon either inside the bold (`**Verdict:** viable`) or
        # immediately after it (`**Technical Fit:** Strong`).
        match = re.match(r"^\s*\*\*(.+?)\s*:?\*\*\s*(.*)$", part.strip())
        if match:
            label = _normalize_label(match.group(1))
            value = _strip_markup_inline(match.group(2))
            if label and value:
                chunks.append((label, value))
    return chunks


def _extract_at_a_glance(text: str) -> dict[str, str]:
    """Find the `## At a Glance` or `### At a Glance` block and parse `- **Label:** value` lines."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    start_idx: int | None = None
    start_level = 3
    for i, line in enumerate(lines):
        match = re.match(r"^(#{2,3})\s+At a Glance\s*$", line, re.IGNORECASE)
        if match:
            start_idx = i
            start_level = len(match.group(1))
            break
    if start_idx is None:
        return out

    for line in lines[start_idx + 1:]:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= start_level:
                break
        if line.strip() == "":
            continue
        chunks = _split_meta_chunks(line)
        for label, value in chunks:
            if label and label not in out:
                out[label] = value
    return out


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

def _extract_sections(text: str) -> dict[str, str]:
    """Split a Markdown doc by H2 headings and map normalized key -> body text."""
    sections: dict[str, str] = {}
    current_key = None
    current_lines: list[str] = []
    in_code_fence = False

    for line in text.splitlines():
        fence_match = re.match(r"^(```|~~~)", line)
        if fence_match:
            in_code_fence = not in_code_fence

        if not in_code_fence and line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = _normalize_heading(line[3:])
            current_lines = []
            continue

        if current_key is not None:
            current_lines.append(line)
        else:
            # Lead content before the first H2 is ignored as a section; H1/At a
            # Glance are captured separately.
            pass

    if current_key is not None and current_key not in sections:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_output(
    skill: str,
    text: str,
    reference_freshness_at_generation: list[ReferenceFreshness] | None = None,
) -> OutputMetadata:
    """Parse a generated Markdown output and validate it against the skill schema.

    Returns an `OutputMetadata` object with extracted fields, missing required
    sections, a `valid` flag, and a `validation_status`. The parser is defensive:
    malformed or non-conforming documents are reported rather than raised.

    `validation_status` can be:
    - `"valid"` — the output follows the current contract and no required
      sections are missing.
    - `"invalid"` — the output follows the current contract but is missing
      one or more required sections (including a missing `**Date:**` line or
      `Source Coverage`).
    - `"unvalidated"` — the output does not have enough current-format markers
      (title, At a Glance, and at least one non-source-coverage required
      section) for us to confidently validate it. This is used for legacy
      outputs that predate the current required-section contract so they are not
      presented as definitively broken.
    """
    schema = _SKILL_SCHEMAS.get(skill)
    title = _extract_title(text)
    date = _extract_date(text)
    at_a_glance = _extract_at_a_glance(text)
    sections = _extract_sections(text)

    required: list[str] = list(schema.required_sections) if schema else []

    def _section_present(required_key: str) -> bool:
        """A required section is present if a heading or At a Glance label covers it."""
        parts = set(required_key.split("-"))
        if any(parts <= set(found.split("-")) for found in sections):
            return True
        if any(parts <= set(label.split("-")) for label in at_a_glance):
            return True
        return False

    # If we have no schema for this skill, we cannot validate it.
    if schema is None:
        return OutputMetadata(
            skill=skill,
            title=title,
            date=date,
            at_a_glance=at_a_glance,
            sections={k: v[:5000] for k, v in sections.items()},
            required_sections=[],
            missing_sections=[],
            validation_errors=[],
            valid=True,
            schema_version=SCHEMA_VERSION,
            validation_status="unvalidated",
            reference_freshness_at_generation=reference_freshness_at_generation,
        )

    missing: list[str] = [s for s in required if not _section_present(s)]

    # A document must have enough current-format markers for us to confidently
    # say it is incomplete. Legacy outputs may use older headings; without a
    # title, At a Glance block, and at least one non-source-coverage required
    # section, we treat the result as "unvalidated" rather than "invalid". A
    # missing **Date:** line, by contrast, is a concrete validation error once
    # we have recognized the current format.
    non_source_required = [s for s in required if s != "source-coverage"]
    has_non_source_required = any(_section_present(s) for s in non_source_required)
    has_current_markers = bool(title and at_a_glance and has_non_source_required)

    if not has_current_markers:
        return OutputMetadata(
            skill=skill,
            title=title,
            date=date,
            at_a_glance=at_a_glance,
            sections={k: v[:5000] for k, v in sections.items()},
            required_sections=required,
            missing_sections=[],
            validation_errors=[],
            valid=True,
            schema_version=SCHEMA_VERSION,
            validation_status="unvalidated",
            reference_freshness_at_generation=reference_freshness_at_generation,
        )

    errors: list[str] = []
    if missing:
        errors.append(f"Missing required sections: {', '.join(missing)}.")
    if not date:
        errors.append("No **Date:** line found in the title block.")
    if "source-coverage" not in sections:
        errors.append("Missing Source Coverage section.")

    valid = not errors
    validation_status = "valid" if valid else "invalid"

    return OutputMetadata(
        skill=skill,
        title=title,
        date=date,
        at_a_glance=at_a_glance,
        sections={k: v[:5000] for k, v in sections.items()},
        required_sections=required,
        missing_sections=missing,
        validation_errors=errors,
        valid=valid,
        schema_version=SCHEMA_VERSION,
        validation_status=validation_status,
        reference_freshness_at_generation=reference_freshness_at_generation,
    )


def write_sidecar(md_path: Path, metadata: OutputMetadata) -> None:
    """Write the metadata sidecar as `<md_path>.json`."""
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    sidecar.write_text(json.dumps(metadata.model_dump(), indent=2), encoding="utf-8")


def read_or_parse_sidecar(md_path: Path, skill: str) -> OutputMetadata:
    """Return metadata from the sidecar if fresh, otherwise parse the Markdown and write it."""
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    if sidecar.exists():
        try:
            md_mtime = md_path.stat().st_mtime
            sc_mtime = sidecar.stat().st_mtime
            if sc_mtime >= md_mtime:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                if data.get("schema_version") == SCHEMA_VERSION:
                    return OutputMetadata(**data)
                logger.info("Sidecar schema version %s != %s; reparsing", data.get("schema_version"), SCHEMA_VERSION)
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to read sidecar %s; reparsing", sidecar)

    text = md_path.read_text(encoding="utf-8")
    metadata = parse_output(skill, text)
    try:
        write_sidecar(md_path, metadata)
    except OSError:
        logger.warning("Failed to write sidecar for %s", md_path)
    return metadata


# ---------------------------------------------------------------------------
# Semantic comparison helpers
# ---------------------------------------------------------------------------

_RISK_SECTION_KEYS = {
    "deal-blocker",
    "what-would-lose-it",
    "probability-verdict",
    "bottom-line",
    "sfdc-vs-reality",
    "top-risks",
}
_RISK_SECTION_SUBSTRINGS = ("risk", "probability", "verdict")

_ACTION_SECTION_KEYS = {
    "what-would-close-it",
    "recommended-actions",
    "next-steps",
    "action-items",
    "recommended-next-steps",
    "action-plan",
}
_ACTION_SECTION_SUBSTRINGS = ("action", "next-step", "close-criteria", "what-would-close")

_DISPLAY_TITLE_OVERRIDES = {
    "at-a-glance": "At a Glance",
    "meddpicc-pre-scorecard": "MEDDPICC Pre-Scorecard",
    "probability-verdict": "Probability Verdict",
    "what-would-close-it": "What Would Close It",
    "what-would-lose-it": "What Would Lose It",
    "deal-blocker": "Deal Blocker",
    "bottom-line": "Bottom Line",
    "source-coverage": "Source Coverage",
    "coaching-observations": "Coaching Observations",
    "what-changed-since-last-assessment": "What Changed Since Last Assessment",
    "stakeholder-read": "Stakeholder Read",
    "sfdc-vs-reality": "SFDC vs. Reality",
    "activity-trajectory": "Activity Trajectory",
}


def _display_title(key: str) -> str:
    """Convert a normalized section key to a readable title."""
    if key in _DISPLAY_TITLE_OVERRIDES:
        return _DISPLAY_TITLE_OVERRIDES[key]
    return key.replace("-", " ").title()


def _is_risk_section(key: str) -> bool:
    """True if this section typically contains risk-oriented content."""
    if key in _RISK_SECTION_KEYS:
        return True
    return any(hint in key for hint in _RISK_SECTION_SUBSTRINGS)


def _is_action_section(key: str) -> bool:
    """True if this section typically contains recommended actions or next steps."""
    if key in _ACTION_SECTION_KEYS:
        return True
    return any(hint in key for hint in _ACTION_SECTION_SUBSTRINGS)


def _extract_list_items(text: str) -> list[tuple[str, str]]:
    """Return bullet/numeric list items as (normalized, display) tuples.

    Table rows and plain paragraphs are ignored so the diff stays at the
    item level where the output uses lists.
    """
    items: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:[-*•]|\d+\.)\s+(?:\[[ xX]\]\s*)?(.*)$", line)
        if match:
            display = match.group(1).strip()
            norm = _strip_markup_inline(display).lower()
            if norm:
                items.append((norm, display))
    return items


def _item_diff(
    left: list[tuple[str, str]],
    right: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Diff two ordered lists of (normalized, display) items.

    Returns change objects with `type` in {"unchanged", "added", "removed",
    "changed"}. A "changed" row preserves the before/after display text.
    """
    sm = difflib.SequenceMatcher(None, [i[0] for i in left], [i[0] for i in right])
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for (ln, ld), (rn, rd) in zip(left[i1:i2], right[j1:j2]):
                changes.append({"type": "unchanged", "left": ld, "right": rd})
        elif tag == "delete":
            for _n, display in left[i1:i2]:
                changes.append({"type": "removed", "left": display, "right": None})
        elif tag == "insert":
            for _n, display in right[j1:j2]:
                changes.append({"type": "added", "left": None, "right": display})
        elif tag == "replace":
            for (ln, ld), (rn, rd) in zip_longest(left[i1:i2], right[j1:j2]):
                if ld is None:
                    changes.append({"type": "added", "left": None, "right": rd})
                elif rd is None:
                    changes.append({"type": "removed", "left": ld, "right": None})
                elif ln == rn:
                    changes.append({"type": "unchanged", "left": ld, "right": rd})
                else:
                    changes.append({"type": "changed", "left": ld, "right": rd})
    return changes


def _section_body_norm(text: str | None) -> str:
    """Normalize a section body for equality comparisons."""
    if not text:
        return ""
    return _strip_markup_inline(text).lower().strip()


def semantic_diff(left_meta: OutputMetadata, right_meta: OutputMetadata) -> dict[str, Any]:
    """Compare two parsed deal-assessment (or similar) outputs semantically.

    Uses the structured sidecar data (`at_a_glance` and `sections`) when
    available and falls back to whole-section before/after for prose-heavy
    sections. The result is deterministic and does not use an LLM.
    """
    left_at = left_meta.at_a_glance or {}
    right_at = right_meta.at_a_glance or {}
    all_labels = sorted(set(left_at.keys()) | set(right_at.keys()))

    at_a_glance: list[dict[str, Any]] = []
    at_a_glance_changed = 0
    for label in all_labels:
        left_val = left_at.get(label, "")
        right_val = right_at.get(label, "")
        left_norm = _strip_markup_inline(left_val).lower().strip() if left_val else ""
        right_norm = _strip_markup_inline(right_val).lower().strip() if right_val else ""
        if label in left_at and label not in right_at:
            change = "removed"
            at_a_glance_changed += 1
        elif label in right_at and label not in left_at:
            change = "added"
            at_a_glance_changed += 1
        elif left_norm != right_norm:
            change = "changed"
            at_a_glance_changed += 1
        else:
            change = "unchanged"
        at_a_glance.append({
            "label": label,
            "display_label": _display_title(label),
            "change": change,
            "left": left_val,
            "right": right_val,
        })

    left_sections = left_meta.sections or {}
    right_sections = right_meta.sections or {}
    all_keys = sorted(set(left_sections.keys()) | set(right_sections.keys()))

    sections: list[dict[str, Any]] = []
    sections_changed = 0
    sections_added = 0
    sections_removed = 0
    risks_added = 0
    risks_removed = 0
    risks_changed = 0
    actions_added = 0
    actions_removed = 0
    actions_changed = 0

    for key in all_keys:
        in_left = key in left_sections
        in_right = key in right_sections
        left_body = left_sections.get(key)
        right_body = right_sections.get(key)
        is_risk = _is_risk_section(key)
        is_action = _is_action_section(key)

        if not in_left:
            change = "added"
            sections_added += 1
        elif not in_right:
            change = "removed"
            sections_removed += 1
        elif _section_body_norm(left_body) != _section_body_norm(right_body):
            change = "changed"
            sections_changed += 1
        else:
            change = "unchanged"

        left_items = _extract_list_items(left_body or "")
        right_items = _extract_list_items(right_body or "")
        item_changes = _item_diff(left_items, right_items)

        # Count risk / action list-level changes.
        if is_risk:
            if change == "added":
                risks_added += len(right_items)
            elif change == "removed":
                risks_removed += len(left_items)
            else:
                for ic in item_changes:
                    if ic["type"] == "added":
                        risks_added += 1
                    elif ic["type"] == "removed":
                        risks_removed += 1
                    elif ic["type"] == "changed":
                        risks_changed += 1
        if is_action:
            if change == "added":
                actions_added += len(right_items)
            elif change == "removed":
                actions_removed += len(left_items)
            else:
                for ic in item_changes:
                    if ic["type"] == "added":
                        actions_added += 1
                    elif ic["type"] == "removed":
                        actions_removed += 1
                    elif ic["type"] == "changed":
                        actions_changed += 1

        sections.append({
            "key": key,
            "title": _display_title(key),
            "change": change,
            "is_risk": is_risk,
            "is_action": is_action,
            "left_body": left_body,
            "right_body": right_body,
            "left_items": [d for _n, d in left_items],
            "right_items": [d for _n, d in right_items],
            "item_changes": item_changes,
        })

    structured_changes = any(
        [sections_changed, sections_added, sections_removed, at_a_glance_changed,
         risks_added, risks_removed, risks_changed, actions_added, actions_removed, actions_changed]
    )

    if structured_changes:
        parts = [f"Sections changed: {sections_changed}"]
        if sections_added:
            parts.append(f"added: {sections_added}")
        if sections_removed:
            parts.append(f"removed: {sections_removed}")
        if at_a_glance_changed:
            parts.append(f"At a Glance changed: {at_a_glance_changed}")
        if risks_added or risks_removed or risks_changed:
            parts.append(f"Risks added: {risks_added}, removed: {risks_removed}, changed: {risks_changed}")
        if actions_added or actions_removed or actions_changed:
            parts.append(f"Actions changed: {actions_added + actions_removed + actions_changed} ({actions_added} added, {actions_removed} removed, {actions_changed} changed)")
        message = " · ".join(parts)
    else:
        message = "No material structured changes found."

    summary = {
        "sections_changed": sections_changed,
        "sections_added": sections_added,
        "sections_removed": sections_removed,
        "at_a_glance_changed": at_a_glance_changed,
        "risks_added": risks_added,
        "risks_removed": risks_removed,
        "risks_changed": risks_changed,
        "actions_added": actions_added,
        "actions_removed": actions_removed,
        "actions_changed": actions_added + actions_removed + actions_changed,
        "actions_changed_only": actions_changed,
        "structured_changes": structured_changes,
        "message": message,
    }

    return {
        "summary": summary,
        "at_a_glance": at_a_glance,
        "sections": sections,
    }


def skill_has_schema(skill: str) -> bool:
    return skill in _SKILL_SCHEMAS


def list_schemas() -> list[str]:
    return list(_SKILL_SCHEMAS.keys())
