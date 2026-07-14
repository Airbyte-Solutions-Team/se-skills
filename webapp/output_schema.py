"""Pydantic schemas and Markdown extraction for SE skill outputs.

Each saving skill is expected to follow the shared output-document format in
`skills/_se-playbook.md` (H1 title, one-line meta, `### At a Glance`, H2 body
sections, `## Source Coverage` last). This module turns a generated `.md` file
into a typed `OutputMetadata` sidecar and reports which required sections are
missing so the UI can warn the SE when an output looks incomplete.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


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

def parse_output(skill: str, text: str) -> OutputMetadata:
    """Parse a generated Markdown output and validate it against the skill schema.

    Returns an `OutputMetadata` object with extracted fields, missing required
    sections, and a `valid` flag. The parser is defensive: malformed or
    non-conforming documents are reported rather than raised.
    """
    schema = _SKILL_SCHEMAS.get(skill)
    title = _extract_title(text)
    date = _extract_date(text)
    at_a_glance = _extract_at_a_glance(text)
    sections = _extract_sections(text)

    required = list(schema.required_sections) if schema else []

    def _section_present(required: str) -> bool:
        """A required section is present if a heading or At a Glance label covers it."""
        parts = set(required.split("-"))
        if any(parts <= set(found.split("-")) for found in sections):
            return True
        if any(parts <= set(label.split("-")) for label in at_a_glance):
            return True
        return False

    missing: list[str] = [s for s in required if not _section_present(s)]

    errors: list[str] = []
    if missing:
        errors.append(f"Missing required sections: {', '.join(missing)}.")
    if not date:
        errors.append("No **Date:** line found in the title block.")
    if "source-coverage" not in sections:
        errors.append("Missing Source Coverage section.")

    valid = not errors

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
                return OutputMetadata(**data)
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to read sidecar %s; reparsing", sidecar)

    text = md_path.read_text(encoding="utf-8")
    metadata = parse_output(skill, text)
    try:
        write_sidecar(md_path, metadata)
    except OSError:
        logger.warning("Failed to write sidecar for %s", md_path)
    return metadata


def skill_has_schema(skill: str) -> bool:
    return skill in _SKILL_SCHEMAS


def list_schemas() -> list[str]:
    return list(_SKILL_SCHEMAS.keys())
