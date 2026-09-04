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
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from reference_freshness import ReferenceChange, ReferenceFreshness, compute_reference_freshness
from architecture import CANONICAL_ARCHITECTURE, get_architecture

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

Mode = Literal["full", "brief"]


class SkillOutputSchema(BaseModel):
    """Schema definition for one skill's Markdown output."""

    skill: str
    required_sections: list[str] = Field(default_factory=list)
    required_at_a_glance_labels: list[str] = Field(default_factory=list)
    brief_required_sections: list[str] | None = None
    brief_required_at_a_glance_labels: list[str] | None = None
    conditional_sections: list[str] = Field(default_factory=list)
    canonical_h2_order: list[str] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    source_coverage_required: bool = False
    forbid_placeholders: bool = False
    strict_at_a_glance: bool = False
    strict_sections: bool = False
    strict: bool = False
    validation_enforced: bool = False


class OutputMetadata(BaseModel):
    """Typed sidecar for a generated skill output."""

    skill: str
    title: str | None = None
    date: str | None = None
    mode: str = "full"  # "full" | "brief"
    at_a_glance: dict[str, str] = Field(default_factory=dict)
    sections: dict[str, str] = Field(default_factory=dict)
    required_sections: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    valid: bool = True
    schema_version: int = SCHEMA_VERSION
    validation_status: str = "unvalidated"  # "valid" | "invalid" | "unvalidated"
    is_legacy: bool = False
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
# Per-skill schemas.
#
# `CANONICAL_ARCHITECTURE` provides canonical H2 order, legacy heading aliases,
# and Source Coverage rules for every report-style skill. The overrides below
# preserve per-skill At-a-Glance labels, conditional sections, and strictness
# flags. The canonical H2 order is used for sidebar ordering and the
# Source- Coverage-last invariant; `required_sections` is derived from it but
# may exclude optional/conditional H2s.
# ---------------------------------------------------------------------------

_SKILL_SCHEMA_OVERRIDES: dict[str, dict[str, Any]] = {
    "post-call": {
        "required_sections": [
            "key-takeaways",
            "deal-impact",
            "objections-and-open-questions",
            "actions-and-next-step",
            "source-coverage",
        ],
        "required_at_a_glance_labels": [
            "call-type",
            "call-date",
            "attendees",
            "action-items",
            "next-step",
            "deal-assessment-update-needed",
        ],
        "brief_required_sections": [
            "key-takeaways",
            "actions-and-next-step",
            "source-coverage",
        ],
        "brief_required_at_a_glance_labels": [
            "call-type",
            "call-date",
            "attendees",
            "action-items",
            "next-step",
            "deal-assessment-update-needed",
        ],
        # Transcript-entity triggers add these to the required list when the
        # transcript contains matching content. If present, they must be
        # non-empty.
        "conditional_sections": [
            "scope-and-technical-changes",
            "coaching-observations",
        ],
        "source_coverage_required": True,
        "forbid_placeholders": True,
        "strict_at_a_glance": True,
        "strict_sections": True,
        "strict": True,
        "validation_enforced": True,
    },
    "biz-qual": {
        "required_sections": ["meddpicc-scorecard", "source-coverage"],
        "brief_required_sections": ["meddpicc-scorecard", "source-coverage"],
        "required_at_a_glance_labels": ["overall", "recommended-motion"],
        "validation_enforced": True,
    },
    "tech-qual": {
        "required_sections": ["technical-fit-summary", "source-coverage"],
        "brief_required_sections": ["technical-fit-summary", "source-coverage"],
        "required_at_a_glance_labels": ["technical-fit", "primary-risk"],
        "validation_enforced": True,
    },
    "deployment-model-qual": {
        "required_sections": ["deployment-verdict", "source-coverage"],
        "brief_required_sections": ["deployment-verdict", "source-coverage"],
        "required_at_a_glance_labels": ["verdict", "recommended-motion"],
        "validation_enforced": True,
    },
    "poc-plan": {
        "required_sections": ["success-criteria", "source-coverage"],
        "brief_required_sections": ["success-criteria", "source-coverage"],
        "required_at_a_glance_labels": ["poc-proves", "timeline", "success-criteria"],
        "validation_enforced": True,
    },
    "connector-feasibility": {
        "required_sections": ["system-by-system-fit", "source-coverage"],
        "brief_required_sections": ["system-by-system-fit", "source-coverage"],
        "required_at_a_glance_labels": ["feasibility", "recommended-motion"],
        "validation_enforced": True,
    },
    "pov-gsheet": {
        "required_at_a_glance_labels": ["google-sheet-url", "status"],
        "validation_enforced": True,
    },
}

_SKILL_SCHEMAS: dict[str, SkillOutputSchema] = {}
for _arch in CANONICAL_ARCHITECTURE.values():
    if _arch.structured_exception:
        continue
    _overrides = _SKILL_SCHEMA_OVERRIDES.get(_arch.skill, {})
    _kwargs = {
        "skill": _arch.skill,
        "required_sections": _arch.canonical_h2_order,
        "canonical_h2_order": _arch.canonical_h2_order,
        "aliases": _arch.aliases,
        "source_coverage_required": _arch.source_coverage_required,
    }
    _kwargs.update(_overrides)
    _SKILL_SCHEMAS[_arch.skill] = SkillOutputSchema(**_kwargs)


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


def _extract_at_a_glance(text: str, summary_heading: str | None = None) -> dict[str, str]:
    """Find the top summary block (`At a Glance` or a profile-specific name).

    Accepts the profile summary heading (e.g. `Call Snapshot`, `Decision Summary`)
    in addition to `At a Glance` so newer outputs with deliberate summary names
    are still parsed without hard-coding every variant.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    start_idx: int | None = None
    start_level = 3
    headings = ["At a Glance"]
    if summary_heading:
        headings.append(summary_heading)
    pattern = r"^(#{2,3})\s+(?:" + "|".join(re.escape(h) for h in headings) + r")\s*$"
    for i, line in enumerate(lines):
        match = re.match(pattern, line, re.IGNORECASE)
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

    def _set_body(key: str, lines: list[str]) -> None:
        body = "\n".join(lines).strip()
        # If the same heading appears more than once, keep the first non-empty body.
        if key not in sections or not sections[key]:
            sections[key] = body

    for line in text.splitlines():
        fence_match = re.match(r"^(```|~~~)", line)
        if fence_match:
            in_code_fence = not in_code_fence

        if not in_code_fence and line.startswith("## "):
            if current_key is not None:
                _set_body(current_key, current_lines)
            current_key = _normalize_heading(line[3:])
            current_lines = []
            continue

        if current_key is not None:
            current_lines.append(line)
        else:
            # Lead content before the first H2 is ignored as a section; H1/At a
            # Glance are captured separately.
            pass

    if current_key is not None:
        _set_body(current_key, current_lines)

    return sections


def _canonicalize_sections(
    sections: dict[str, str], arch: Any | None
) -> tuple[dict[str, str], set[str]]:
    """Map legacy H2 headings to canonical keys, preserving source order.

    Returns the canonicalized section dict and the set of original keys that
    were legacy-only (used to avoid reclassifying old outputs as corrupt).
    """
    if arch is None:
        return sections, set()

    canonical: dict[str, str] = {}
    legacy_seen: set[str] = set()
    for original_key, body in sections.items():
        canonical_key = original_key
        if original_key in arch.aliases:
            canonical_key = arch.aliases[original_key]
            if original_key not in arch.canonical_h2_order:
                legacy_seen.add(original_key)
        elif original_key not in arch.canonical_h2_order:
            # Unknown headings are kept as-is; they may be expansions that the
            # token-subset resolver later matches.
            pass

        if canonical_key in canonical:
            # If several legacy headings map to the same canonical H2, merge
            # their bodies. This preserves older outputs that split content
            # across now-consolidated sections.
            existing = canonical[canonical_key]
            if body.strip():
                canonical[canonical_key] = existing + "\n\n" + body if existing.strip() else body
        else:
            canonical[canonical_key] = body
    return canonical, legacy_seen


# ---------------------------------------------------------------------------
# Post-call validation helpers
# ---------------------------------------------------------------------------

# Bracket text that is allowed and does not indicate an unfilled template.
_PLACEHOLDER_ALLOWED_BRACKETS = frozenset({"stated", "inferred"})


def _is_allowed_bracket_content(content: str) -> bool:
    """Return True for checkboxes, tags, and callouts rather than placeholders."""
    content = content.strip()
    if content.lower() in _PLACEHOLDER_ALLOWED_BRACKETS:
        return True
    if content.startswith("!"):
        # Markdown callouts such as [!verdict], [!risk], [!info].
        return True
    if content.isdigit():
        # Counts such as ==[3]== or [3].
        return True
    # Checkbox states.
    if re.fullmatch(r"[xX ]?", content):
        return True
    if re.match(r"^(?:stated|inferred)\b", content, re.IGNORECASE):
        return True
    return False


def _mask_markdown_non_placeholders(text: str) -> str:
    """Mask Markdown constructs whose brackets are syntax, not placeholders."""
    masked = text

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    # Fenced blocks are checked first so bracket-like content inside them is
    # never interpreted as document prose.
    masked = re.sub(r"(?ms)^(```|~~~)[^\n]*\n.*?^\1\s*$", blank, masked)
    # Inline code spans, links/images, reference links, and footnote refs all
    # use brackets as Markdown syntax rather than template placeholders.
    masked = re.sub(r"`{1,3}[^`\n]*`{1,3}", blank, masked)
    masked = re.sub(r"!?\[[^\]\n]*\]\([^)\n]*\)", blank, masked)
    masked = re.sub(r"!?\[[^\]\n]*\]\[[^\]\n]*\]", blank, masked)
    masked = re.sub(r"\[\^[^\]\n]+\]", blank, masked)
    return masked


def _find_placeholders(text: str) -> list[str]:
    """Find bracketed text that looks like an unfilled template placeholder."""
    placeholders: list[str] = []
    if not text:
        return placeholders
    masked = _mask_markdown_non_placeholders(text)
    for match in re.finditer(r"\[([^\]\n]+)\]", masked):
        inner = match.group(1)
        if not _is_allowed_bracket_content(inner):
            placeholders.append(text[match.start():match.end()])
    return placeholders


_GONG_CALL_ID_RE = re.compile(r"\b\d{10,}\b")
_FULL_COVERAGE_CLAIM_RE = re.compile(
    r"\b(?:full(?:y)?|complete(?:ly)?|entire(?:ty)?|no truncation|not truncated)\b", re.IGNORECASE
)


def _validate_source_coverage_post_call(body: str) -> list[str]:
    """Source Coverage for post-call must claim a complete read: either concrete
    read/total line counts (a locally-read transcript file), or — since a
    Gong-pulled transcript has no native line count to report — a Gong call ID
    plus an explicit full/complete-read claim."""
    errors: list[str] = []
    if not body or not body.strip():
        errors.append("Source Coverage section is empty.")
        return errors
    match = re.search(r"(\d+)\s*/\s*(\d+)\s*(?:line|lines|ln|lns)", body, re.IGNORECASE)
    if not match:
        if (
            "gong" in body.lower()
            and _GONG_CALL_ID_RE.search(body)
            and _FULL_COVERAGE_CLAIM_RE.search(body)
        ):
            return errors
        errors.append(
            "Source Coverage must report concrete read/total line counts (e.g. '612 / 612 lines') "
            "for a local transcript, or a Gong call ID plus an explicit full/complete-read claim "
            "for a Gong-sourced transcript."
        )
        return errors
    read_count = int(match.group(1))
    total_count = int(match.group(2))
    if total_count <= 0:
        errors.append("Source Coverage total line count must be greater than 0.")
    if read_count < total_count:
        errors.append(f"Source Coverage reports a partial read ({read_count} / {total_count} lines).")
    elif read_count > total_count:
        errors.append(f"Source Coverage read count exceeds total ({read_count} / {total_count} lines).")
    return errors


# ---------------------------------------------------------------------------
# Deterministic transcript-entity triggers for conditional sections
# ---------------------------------------------------------------------------

def _transcript_triggered_conditionals(transcript_text: str | None) -> set[str]:
    """Return the conditional post-call H2 sections required by transcript evidence.

    These rules are deterministic and require no LLM. They scan the transcript for
    entity and intent markers that make a conditional section decision-critical.
    Token/phrase boundaries are used so incidental substrings (e.g. "ae" inside
    "aeroplane" or "api" inside "rapid") do not trigger sections incorrectly.

    The returned keys are canonical H2 keys from `CANONICAL_ARCHITECTURE["post-call"]`.
    """
    triggered: set[str] = set()
    if not transcript_text:
        return triggered

    lowered = transcript_text.lower()

    # Scope & Technical Changes is required when the call discusses connectors,
    # systems, integrations, platforms, or APIs.
    if re.search(
        r"\b(?:connector|connectors|source|sources|destination|destinations|"
        r"integration|integrations|system|systems|platform|platforms|api|apis|"
        r"data source|data sources|data warehouse)\b",
        lowered,
    ):
        triggered.add("scope-and-technical-changes")

    # Technical Notes is required when technical scope is discussed.
    if re.search(
        r"\b(?:technical|architecture|infrastructure|schema|schemas|database|databases|"
        r"cdc|etl|elt|data pipeline|data pipelines|warehouse|data model|data modeling|"
        r"normalization|dbt|sql|query|queries|dataset|engineering|developer|development|"
        r"custom connector|build|building|code|script|scripts)\b",
        lowered,
    ):
        triggered.add("scope-and-technical-changes")

    ae_role = re.search(
        r"\b(?:ae|account executive|sales rep|sales representative|sdr|"
        r"sales development rep|business development rep)\b",
        lowered,
    )
    discovery = re.search(
        r"\b(?:discovery call|discovery meeting|discovery session|intro call|"
        r"initial call|first call|qualification call|qualifying call|qual call|"
        r"meddpicc|metrics|economic buyer|decision criteria|decision process|"
        r"identify pain|champion|competition)\b",
        lowered,
    )
    if ae_role and discovery:
        triggered.add("deal-impact")

    return triggered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_output(
    skill: str,
    text: str,
    reference_freshness_at_generation: list[ReferenceFreshness] | None = None,
    mode: Mode = "full",
    transcript_text: str | None = None,
) -> OutputMetadata:
    """Parse a generated Markdown output and validate it against the skill schema.

    Returns an `OutputMetadata` sidecar with extracted fields, missing required
    sections, a `valid` flag, and a `validation_status`.

    For strict schemas (e.g. `post-call`), every candidate resolves to either
    `"valid"` or `"invalid"`; `"unvalidated"` is only used for non-strict or
    unrecognized skill outputs that lack enough current-format markers.

    `transcript_text` is used by the `post-call` schema to deterministically
    trigger conditional sections (e.g. Scope & Technical Changes). It never
    uses an LLM.
    """
    arch = get_architecture(skill)
    schema = _SKILL_SCHEMAS.get(skill)
    title = _extract_title(text)
    date = _extract_date(text)
    at_a_glance = _extract_at_a_glance(text, arch.top_summary_name if arch else None)
    sections = _extract_sections(text)
    source_heading_keys = set(sections)
    sections, legacy_seen = _canonicalize_sections(sections, arch)
    new_only = (
        set(arch.canonical_h2_order) - set(arch.aliases)
        if arch is not None
        else set()
    )
    is_legacy = bool(legacy_seen) and not bool(source_heading_keys & new_only)

    required: list[str] = list(schema.required_sections) if schema else []
    required_at_a_glance: list[str] = list(schema.required_at_a_glance_labels) if schema else []
    if mode == "brief" and schema:
        if schema.brief_required_sections is not None:
            required = list(schema.brief_required_sections)
        if schema.brief_required_at_a_glance_labels is not None:
            required_at_a_glance = list(schema.brief_required_at_a_glance_labels)

    if schema is not None and not schema.validation_enforced:
        return OutputMetadata(
            skill=skill,
            title=title,
            date=date,
            mode=mode,
            at_a_glance=at_a_glance,
            sections={k: v[:5000] for k, v in sections.items()},
            required_sections=required,
            missing_sections=[],
            validation_errors=[],
            valid=True,
            schema_version=SCHEMA_VERSION,
            validation_status="unvalidated",
            is_legacy=is_legacy,
            reference_freshness_at_generation=reference_freshness_at_generation,
        )

    # Conditional transcript-entity triggers are deterministic and additive.
    triggered_conditionals: set[str] = set()
    if skill == "post-call" and schema and transcript_text is not None:
        triggered_conditionals = _transcript_triggered_conditionals(transcript_text)
        required = required + sorted(
            c for c in triggered_conditionals
            if c not in required
        )

    def _resolve_heading(required_key: str) -> str | None:
        """Return the actual normalized heading key in `sections` that covers the required key.

        A heading matches when the required key's tokens are a subset of the
        found heading's tokens (e.g. "key-takeaways" matches "key-takeaways-and-decisions").
        """
        parts = set(required_key.split("-"))
        for found in sections:
            if parts <= set(found.split("-")):
                return found
        return None

    def _heading_present(required_key: str) -> bool:
        """A required section is present if a normalized H2 heading covers it."""
        return _resolve_heading(required_key) is not None

    def _section_present(required_key: str) -> bool:
        """A required section is present if a heading or At a Glance label covers it."""
        if _heading_present(required_key):
            return True
        parts = set(required_key.split("-"))
        return any(parts <= set(label.split("-")) for label in at_a_glance)

    section_check = _heading_present if (schema and schema.strict_sections) else _section_present
    strict = bool(schema and schema.strict)

    # Unknown skills cannot be validated; return an unvalidated marker.
    if schema is None:
        return OutputMetadata(
            skill=skill,
            title=title,
            date=date,
            mode=mode,
            at_a_glance=at_a_glance,
            sections={k: v[:5000] for k, v in sections.items()},
            required_sections=[],
            missing_sections=[],
            validation_errors=[],
            valid=True,
            schema_version=SCHEMA_VERSION,
            validation_status="unvalidated",
            is_legacy=False,
            reference_freshness_at_generation=reference_freshness_at_generation,
        )

    missing: list[str] = [s for s in required if not section_check(s)]

    # Conditional sections are not required to be present, but if a heading for one
    # appears in the document the runtime has decided to include it and it must have
    # a non-empty body. Transcript-entity triggers are added by `_transcript_triggered_conditionals` and applied above for the `post-call` skill.
    present_conditional: list[str] = []
    if schema and schema.conditional_sections:
        for key in schema.conditional_sections:
            if _resolve_heading(key) is not None:
                present_conditional.append(key)

    # A document must have enough current-format markers for us to confidently
    # say it is incomplete. Legacy outputs may use older headings; without a
    # title and At a Glance block, we treat the result as "unvalidated" rather
    # than "invalid". Missing **Date:** on an otherwise current-format document
    # is then reported as a validation error.
    has_current_markers = bool(title and at_a_glance)

    # Strict schemas must always resolve to valid/invalid, never unvalidated.
    if not has_current_markers and not strict:
        return OutputMetadata(
            skill=skill,
            title=title,
            date=date,
            mode=mode,
            at_a_glance=at_a_glance,
            sections={k: v[:5000] for k, v in sections.items()},
            required_sections=required,
            missing_sections=[],
            validation_errors=[],
            valid=True,
            schema_version=SCHEMA_VERSION,
            validation_status="unvalidated",
            is_legacy=False,
            reference_freshness_at_generation=reference_freshness_at_generation,
        )

    errors: list[str] = []
    if mode not in ("full", "brief"):
        errors.append("Invalid output mode; must be 'full' or 'brief'.")

    if not title:
        errors.append("No H1 title found.")
    if not date:
        errors.append("No **Date:** line found in the title block.")
    if strict and not at_a_glance:
        errors.append("No At a Glance section found.")

    if missing:
        errors.append(f"Missing required sections: {', '.join(missing)}.")

    missing_labels = [label for label in required_at_a_glance if label not in at_a_glance]
    if missing_labels and schema and schema.strict_at_a_glance:
        errors.append(f"Missing At a Glance fields: {', '.join(missing_labels)}.")

    # In strict mode, required and present conditional sections must have non-empty,
    # meaningful bodies. The resolved (possibly expanded) heading is used so an
    # empty "Key Takeaways and Decisions" cannot masquerade as a present section.
    if strict:
        for required_key in required:
            found_key = _resolve_heading(required_key)
            if found_key is not None:
                body = sections[found_key].strip()
                if not body:
                    errors.append(f"Required section '{required_key}' is empty.")
        for conditional_key in present_conditional:
            found_key = _resolve_heading(conditional_key)
            if found_key is not None:
                body = sections[found_key].strip()
                if not body:
                    errors.append(f"Conditional section '{conditional_key}' is empty.")
        if "deal-impact" in triggered_conditionals and not re.search(
            r"^###\s+MEDDPICC\b",
            sections.get("deal-impact", ""),
            re.MULTILINE | re.IGNORECASE,
        ):
            errors.append(
                "Deal Impact must include a '### MEDDPICC' subsection for AE-led discovery."
            )

    section_order = list(sections.keys())
    if "source-coverage" not in sections:
        if schema and schema.source_coverage_required:
            errors.append("Missing Source Coverage section.")
    elif schema and schema.source_coverage_required:
        if not is_legacy and section_order.index("source-coverage") != len(section_order) - 1:
            errors.append("Source Coverage must be the final H2 section.")
        if skill == "post-call":
            errors.extend(_validate_source_coverage_post_call(sections["source-coverage"]))
        elif not sections["source-coverage"].strip():
            errors.append("Source Coverage section is empty.")

    # Strict outputs must not ship with unfilled template placeholders anywhere.
    if schema and schema.forbid_placeholders:
        for source, label in [(title, "title"), (date, "date")]:
            if source:
                placeholders = _find_placeholders(source)
                if placeholders:
                    errors.append(f"Unresolved placeholder(s) in {label}: {', '.join(placeholders)}.")
        for label, value in at_a_glance.items():
            placeholders = _find_placeholders(value)
            if placeholders:
                errors.append(f"Unresolved placeholder(s) in At a Glance '{label}': {', '.join(placeholders)}.")
        for section_key, body in sections.items():
            placeholders = _find_placeholders(body)
            if placeholders:
                errors.append(f"Unresolved placeholder(s) in section '{section_key}': {', '.join(placeholders)}.")

    valid = not errors
    validation_status = "valid" if valid else "invalid"

    return OutputMetadata(
        skill=skill,
        title=title,
        date=date,
        mode=mode,
        at_a_glance=at_a_glance,
        sections={k: v[:5000] for k, v in sections.items()},
        required_sections=required,
        missing_sections=missing,
        validation_errors=errors,
        valid=valid,
        schema_version=SCHEMA_VERSION,
        validation_status=validation_status,
        is_legacy=is_legacy,
        reference_freshness_at_generation=reference_freshness_at_generation,
    )


def write_sidecar(md_path: Path, metadata: OutputMetadata) -> None:
    """Write the metadata sidecar as `<md_path>.json`."""
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    sidecar.write_text(json.dumps(metadata.model_dump(), indent=2), encoding="utf-8")


def read_or_parse_sidecar(md_path: Path, skill: str, mode: Mode | None = None) -> OutputMetadata:
    """Return metadata from the sidecar if fresh, otherwise parse the Markdown and write it."""
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    preserved_reference_freshness: list[ReferenceFreshness] | None = None
    if sidecar.exists():
        try:
            md_mtime = md_path.stat().st_mtime
            sc_mtime = sidecar.stat().st_mtime
            if sc_mtime >= md_mtime:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                if data.get("schema_version") != SCHEMA_VERSION:
                    logger.info("Sidecar schema version %s != %s; reparsing", data.get("schema_version"), SCHEMA_VERSION)
                elif data.get("skill") != skill:
                    logger.info("Sidecar skill %s != %s; reparsing", data.get("skill"), skill)
                else:
                    return OutputMetadata(**data)
                # If we are reparsing, trust the sidecar mode unless the caller overrode it.
                if mode is None:
                    mode = data.get("mode", "full")
                snapshot = data.get("reference_freshness_at_generation")
                if not (
                    isinstance(snapshot, list)
                    and all(isinstance(item, dict) for item in snapshot)
                ):
                    snapshot = data.get("reference_freshness")
                if isinstance(snapshot, list) and all(isinstance(item, dict) for item in snapshot):
                    try:
                        preserved_reference_freshness = [
                            ReferenceFreshness.model_validate(item) for item in snapshot
                        ]
                    except (TypeError, ValueError):
                        preserved_reference_freshness = None
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to read sidecar %s; reparsing", sidecar)

    if mode is None:
        mode = "full"
    text = md_path.read_text(encoding="utf-8")
    metadata = parse_output(skill, text, mode=mode)
    if preserved_reference_freshness is not None:
        metadata.reference_freshness_at_generation = preserved_reference_freshness
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
    "key-takeaways": "Key Takeaways",
    "deal-health-signals": "Deal Health Signals",
    "new-objections-concerns-surfaced": "New Objections / Concerns Surfaced",
    "action-items": "Action Items",
    "next-step": "Next Step",
    "sources-destinations": "Sources & Destinations",
    "technical-notes": "Technical Notes",
    "open-questions-follow-ups": "Open Questions / Follow-ups",
    "attendees": "Attendees",
    "meddpicc-quick-pass": "MEDDPICC Quick Pass",
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
    schema = _SKILL_SCHEMAS.get(skill)
    return bool(schema and schema.validation_enforced)


def list_schemas() -> list[str]:
    return list(_SKILL_SCHEMAS.keys())
