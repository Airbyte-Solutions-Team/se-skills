"""Machine-readable tests for UX-010 canonical content architecture."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import output_schema
from architecture import CANONICAL_ARCHITECTURE, _SOURCE_COVERAGE


def _report_skills() -> list:
    return [
        arch
        for arch in CANONICAL_ARCHITECTURE.values()
        if not arch.structured_exception and arch.canonical_h2_order
    ]


@pytest.mark.parametrize("arch", _report_skills(), ids=lambda a: a.skill)
def test_source_coverage_is_final_h2(arch) -> None:
    """Source Coverage must be the last canonical H2 for every report skill."""
    if _SOURCE_COVERAGE in arch.canonical_h2_order:
        assert arch.canonical_h2_order[-1] == _SOURCE_COVERAGE, (
            f"{arch.skill}: Source Coverage must be the final H2"
        )


def test_structured_exceptions_are_explicit() -> None:
    """Specialized artifacts must not inherit normal report architecture."""
    for skill in ("follow-up-email", "full-qual"):
        arch = CANONICAL_ARCHITECTURE.get(skill)
        assert arch and arch.structured_exception
        assert not arch.canonical_h2_order


def test_pov_gsheet_is_lightweight_report() -> None:
    """pov-gsheet is a lightweight schema with a receipt and source coverage."""
    arch = CANONICAL_ARCHITECTURE["pov-gsheet"]
    assert arch.canonical_h2_order == ["receipt", _SOURCE_COVERAGE]
    assert arch.source_coverage_required is True


@pytest.mark.parametrize("arch", CANONICAL_ARCHITECTURE.values(), ids=lambda a: a.skill)
def test_canonical_h2_order_is_unique(arch) -> None:
    """No duplicate H2s in a canonical architecture declaration."""
    assert len(arch.canonical_h2_order) == len(set(arch.canonical_h2_order))


@pytest.mark.parametrize("arch", CANONICAL_ARCHITECTURE.values(), ids=lambda a: a.skill)
def test_legacy_aliases_map_to_canonical_h2s(arch) -> None:
    """Every alias resolves to a canonical H2 that exists in the architecture."""
    for alias, canonical in arch.aliases.items():
        assert canonical in arch.canonical_h2_order, (
            f"{arch.skill}: alias '{alias}' -> '{canonical}' is not a canonical H2"
        )


def test_output_schema_required_sections_ends_with_source_coverage() -> None:
    """SkillOutputSchema required_sections end with Source Coverage where applicable."""
    for skill in output_schema.list_schemas():
        schema = output_schema._SKILL_SCHEMAS[skill]
        if schema.source_coverage_required and schema.required_sections:
            assert schema.required_sections[-1] == _SOURCE_COVERAGE, (
                f"{skill}: schema required sections must end with source-coverage"
            )


_PRODUCER_SKILLS = (
    "prep-call",
    "post-call",
    "biz-qual",
    "tech-qual",
    "deployment-model-qual",
    "connector-feasibility",
    "deal-assessment",
    "poc-plan",
    "roi-business-case",
    "mutual-close-plan",
    "account-refresher",
    "next-move",
    "internal-prep",
    "coverage-handoff",
    "objection-handler",
)


def _marked_template(repo_root: Path, skill: str) -> str:
    text = (repo_root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    matches = re.findall(
        r"<!-- output-template:start -->(.*?)<!-- output-template:end -->",
        text,
        flags=re.DOTALL,
    )
    assert len(matches) == 1, f"{skill}: expected one marked output template"
    return matches[0]


@pytest.mark.parametrize("skill", _PRODUCER_SKILLS)
def test_producer_templates_follow_canonical_architecture(
    skill: str, repo_root: Path
) -> None:
    """Marked producer templates are machine-checkable against the registry."""
    arch = CANONICAL_ARCHITECTURE[skill]
    template = _marked_template(repo_root, skill)
    headings = re.findall(r"^## (.+)$", template, flags=re.MULTILINE)
    keys = [arch.canonical_key(heading) for heading in headings]
    assert keys == arch.canonical_h2_order, skill
    assert all(key is not None for key in keys)

    jump_to = re.search(r"^\*\*Jump to:\*\* (.+)$", template, flags=re.MULTILINE)
    assert jump_to, f"{skill}: missing canonical Jump to line"
    anchors = re.findall(r"\]\(#([^)]+)\)", jump_to.group(1))
    assert anchors == arch.canonical_h2_order, skill

    for parent, children in arch.h3_groups.items():
        if parent == arch.top_summary_name.lower().replace(" ", "-"):
            continue
        parent_match = re.search(
            rf"^## {re.escape(parent.replace('-', ' ').title())}\s*$",
            template,
            flags=re.MULTILINE,
        )
        assert parent_match, f"{skill}: missing H2 parent {parent}"
        next_h2 = re.search(r"^## ", template[parent_match.end() :], flags=re.MULTILINE)
        body = template[parent_match.end() : parent_match.end() + next_h2.start()] if next_h2 else template[parent_match.end() :]
        child_keys = {
            _normalize_heading(match)
            for match in re.findall(r"^### (.+)$", body, flags=re.MULTILINE)
        }
        assert set(children) <= child_keys, f"{skill}: H3s not nested under {parent}"


def _normalize_heading(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def test_playbook_architecture_table_matches_registry(repo_root: Path) -> None:
    """The playbook's architecture table stays synchronized with the registry."""
    text = (repo_root / "skills" / "_se-playbook.md").read_text(encoding="utf-8")
    start = text.index("| Skill | Canonical H2 order | Notes |")
    end = text.index("\n\nLegacy headings", start)
    rows = re.findall(
        r"^\| ([^|]+) \| ([^|]+) \|",
        text[start:end],
        flags=re.MULTILINE,
    )[1:]
    assert [skill.strip() for skill, _ in rows] == list(CANONICAL_ARCHITECTURE)
    for skill, order in rows:
        arch = CANONICAL_ARCHITECTURE[skill.strip()]
        if arch.canonical_h2_order:
            expected = " → ".join(
                " ".join(word.title() for word in key.split("-"))
                for key in arch.canonical_h2_order
            )
            assert order.strip() == expected


def _load_output_fixture(repo_root: Path, filename: str) -> str:
    return (repo_root / "eval" / "fixtures" / "outputs" / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "skill, filename, mode",
    [
        pytest.param("post-call", "post-call-full.md", "full", id="post-call-full"),
        pytest.param("post-call", "post-call-canonical.md", "full", id="post-call-canonical"),
        pytest.param("post-call", "post-call-brief.md", "brief", id="post-call-brief"),
        pytest.param("biz-qual", "next-move-biz-qual.md", None, id="biz-qual-next-move"),
        pytest.param("tech-qual", "next-move-tech-qual.md", None, id="tech-qual-next-move"),
        pytest.param("deployment-model-qual", "next-move-deployment-qual.md", None, id="deployment-qual-next-move"),
        pytest.param("connector-feasibility", "next-move-connector-feasibility.md", None, id="connector-feasibility-next-move"),
        pytest.param("biz-qual", "hourly-biz-qual.md", None, id="biz-qual-hourly"),
        pytest.param("deployment-model-qual", "hourly-deployment-qual.md", None, id="deployment-qual-hourly"),
        pytest.param("connector-feasibility", "hourly-connector-feasibility.md", None, id="connector-feasibility-hourly"),
    ],
)
def test_canonical_fixtures_have_source_coverage_last(
    skill: str, filename: str, mode: str | None, repo_root: Path
) -> None:
    """Every representative synthetic fixture ends with Source Coverage and parses validly."""
    text = _load_output_fixture(repo_root, filename)
    kwargs = {"mode": mode} if mode else {}
    meta = output_schema.parse_output(skill, text, **kwargs)
    assert meta.valid, f"{filename}: {meta.validation_errors}"
    assert meta.validation_status == "valid"
    if meta.sections:
        assert list(meta.sections.keys())[-1] == _SOURCE_COVERAGE


def test_legacy_biz_qual_output_is_not_falsely_marked_corrupt() -> None:
    """Old headings that alias to canonical H2s keep older outputs openable and valid."""
    text = """# Biz Qual

**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Overall:** qualified · **Recommended Motion:** run tech-qual

## MEDDPICC Scorecard
ok

## No Gap Without a Close Path
ok

## Movement Since Last Qualification
ok

## Deal Risks
ok

## Recommended Next Actions
ok

## Source Coverage
- transcript
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.valid is True
    assert meta.validation_status == "valid"
    assert list(meta.sections.keys())[-1] == _SOURCE_COVERAGE
    assert meta.is_legacy is True


def test_legacy_source_coverage_not_last_is_allowed_for_open_review() -> None:
    """Legacy outputs with Source Coverage not last are openable, not falsely invalid."""
    text = """# Biz Qual

**Date:** 2026-07-01 · **Skill:** biz-qual

## At a Glance
- **Overall:** qualified · **Recommended Motion:** run tech-qual

## MEDDPICC Scorecard
ok

## No Gap Without a Close Path
ok

## Movement Since Last Qualification
ok

## Deal Risks
ok

## Source Coverage
- transcript

## Recommended Next Actions
- run tech-qual
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.valid is True
    assert meta.is_legacy is True
    # Source Coverage is not the final heading, but the legacy flag prevents a false final-H2 error.
    section_order = list(meta.sections.keys())
    assert _SOURCE_COVERAGE in section_order
    assert section_order.index(_SOURCE_COVERAGE) != len(section_order) - 1


def test_mixed_legacy_and_canonical_headings_enforce_source_coverage_order() -> None:
    """Mixed-generation documents are current drift, not historical legacy."""
    text = """# Biz Qual

**Date:** 2026-07-01

### Decision Summary
- **Overall:** qualified · **Recommended Motion:** run tech-qual

## Qualification Narrative
ok

## Source Coverage
- transcript

## Recommended Next Actions
run tech-qual
"""
    meta = output_schema.parse_output("biz-qual", text)
    assert meta.is_legacy is False
    assert meta.valid is False
    assert any("Source Coverage must be the final H2" in error for error in meta.validation_errors)


@pytest.mark.parametrize(
    "skill",
    (
        "biz-qual",
        "tech-qual",
        "deployment-model-qual",
        "poc-plan",
        "connector-feasibility",
        "pov-gsheet",
        "post-call",
    ),
)
@pytest.mark.parametrize("mode", ("full", "brief"))
def test_enforced_skills_use_minimal_required_sections(skill: str, mode: str) -> None:
    """Each enforced profile accepts a minimal synthetic full or brief document."""
    schema = output_schema._SKILL_SCHEMAS[skill]
    assert schema.validation_enforced is True
    required = (
        schema.brief_required_sections
        if mode == "brief" and schema.brief_required_sections is not None
        else schema.required_sections
    )
    labels = (
        schema.brief_required_at_a_glance_labels
        if mode == "brief" and schema.brief_required_at_a_glance_labels is not None
        else schema.required_at_a_glance_labels
    )
    summary_heading = CANONICAL_ARCHITECTURE[skill].top_summary_name
    lines = [f"# {skill}", "", "**Date:** July 1, 2026", "", f"### {summary_heading}"]
    lines.extend(f"- **{label.replace('-', ' ').title()}:** confirmed" for label in labels)
    for section in required:
        heading = " ".join(word.title() for word in section.split("-"))
        body = "612 / 612 lines." if section == "source-coverage" and skill == "post-call" else "Synthetic evidence."
        lines.extend(["", f"## {heading}", body])
    text = "\n".join(lines)
    meta = output_schema.parse_output(skill, text, mode=mode)
    assert meta.valid is True, meta.validation_errors

    missing = required[0]
    missing_text = re.sub(
        rf"^## {re.escape(' '.join(word.title() for word in missing.split('-')))}\n(?:Synthetic evidence\.|612 / 612 lines\.)\n?",
        "",
        text,
        flags=re.MULTILINE,
    )
    missing_text = re.sub(
        rf"^- \*\*{re.escape(missing.replace('-', ' ').title())}:\*\* confirmed\n?",
        "",
        missing_text,
        flags=re.MULTILINE,
    )
    invalid = output_schema.parse_output(skill, missing_text, mode=mode)
    assert invalid.valid is False
    assert missing in invalid.missing_sections


def test_non_enforced_schema_is_unvalidated() -> None:
    """Schema support does not imply automatic validation support."""
    assert output_schema.skill_has_schema("prep-call") is False
    meta = output_schema.parse_output(
        "prep-call",
        "# Prep Call\n\n## Account Context\nFacts\n## Source Coverage\nTranscript",
    )
    assert meta.validation_status == "unvalidated"
    assert meta.valid is True


def test_app_js_sidebar_uses_source_order_not_intent_groups(repo_root: Path) -> None:
    """Sidebar no longer groups by intent; it follows the Markdown source order."""
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "tocGroup(" not in app_js
    assert "TOC_GROUP_ORDER" not in app_js
    # Flat source-order links for H2/H3.
    assert 'class="doc-toc-link lvl${t.level}"' in app_js
    assert "tocEntries" in app_js
