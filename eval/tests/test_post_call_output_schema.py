"""Deterministic validation tests for the post-call output contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import output_schema


def _load_fixture(repo_root: Path, filename: str) -> str:
    return (repo_root / "eval" / "fixtures" / "outputs" / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "filename, mode",
    [
        pytest.param("post-call-full.md", "full", id="valid-full"),
        pytest.param("post-call-brief.md", "brief", id="valid-brief"),
    ],
)
def test_post_call_valid_fixtures(filename: str, mode: str, repo_root: Path) -> None:
    text = _load_fixture(repo_root, filename)
    meta = output_schema.parse_output("post-call", text, mode=mode)
    assert meta.valid is True
    assert meta.validation_status == "valid"
    assert meta.title
    assert meta.date
    assert meta.missing_sections == []
    assert not meta.validation_errors


def test_post_call_missing_source_coverage(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("## Source Coverage", "## Removed")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("Source Coverage" in e for e in meta.validation_errors)


def test_post_call_partial_transcript_coverage(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("(612 / 612 lines)", "(300 / 612 lines)")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("partial read" in e.lower() for e in meta.validation_errors)


def test_post_call_missing_critical_section(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("## Action Items", "## Removed")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("action-items" in e for e in meta.validation_errors)


def test_post_call_unresolved_template_placeholders(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("Acme", "[Customer Name]")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("[Customer Name]" in e for e in meta.validation_errors)


def test_post_call_action_item_owner_placeholder(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("**SE**", "**[Owner]**")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("[Owner]" in e for e in meta.validation_errors)


def _remove_section(text: str, heading: str) -> str:
    """Remove a section starting with `heading` up to the next H2 or EOF."""
    import re

    pattern = re.compile(rf"^{re.escape(heading)}\b.*?^(?=## |\Z)", re.MULTILINE | re.DOTALL)
    return pattern.sub("", text)


def test_post_call_conditional_sections_absent_legitimately(repo_root: Path) -> None:
    # Remove all conditional sections from the full fixture; the remaining required
    # sections (including attendees and coaching observations) should still validate.
    text = _load_fixture(repo_root, "post-call-full.md")
    for heading in [
        "## Sources & Destinations",
        "## Technical Notes",
        "## MEDDPICC Quick Pass",
        "## Open Questions / Follow-ups",
    ]:
        text = _remove_section(text, heading)
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is True
    assert meta.validation_status == "valid"


def test_post_call_conditional_section_present_but_empty_is_invalid(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-full.md").replace(
        "## Technical Notes\n- **Volume / scale / frequency:** 10M rows/day [stated]\n- **Deployment / infra / security:** VPC residency required",
        "## Technical Notes",
    )
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("technical-notes" in e.lower() for e in meta.validation_errors)


def test_post_call_empty_expanded_heading_is_invalid(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace(
        "## Action Items",
        "## Action Items and Decisions",
    ).replace(
        "- [ ] **SE** — Schedule technical deep-dive with security lead by June 14\n- [ ] **Champion** — Introduce SE to the security reviewer",
        "",
    )
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("action-items" in e.lower() for e in meta.validation_errors)


def test_post_call_malformed_sidecar_skill_mismatch(tmp_path: Path, repo_root: Path) -> None:
    md_path = tmp_path / "post-call.md"
    text = _load_fixture(repo_root, "post-call-full.md")
    md_path.write_text(text, encoding="utf-8")
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    sidecar.write_text(json.dumps({"schema_version": output_schema.SCHEMA_VERSION, "skill": "biz-qual"}), encoding="utf-8")
    meta = output_schema.read_or_parse_sidecar(md_path, "post-call")
    assert meta.skill == "post-call"
    assert meta.valid is True
    assert meta.validation_status == "valid"


def test_post_call_empty_text_is_invalid() -> None:
    meta = output_schema.parse_output("post-call", "", mode="full")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("H1 title" in e or "At a Glance" in e for e in meta.validation_errors)


def test_post_call_missing_h1_title(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("# Call Summary: Acme", "## Call Summary: Acme")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("H1 title" in e for e in meta.validation_errors)


def test_post_call_missing_at_a_glance(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("### At a Glance", "### Removed")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("At a Glance" in e for e in meta.validation_errors)


def test_post_call_empty_required_section(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace(
        "## Action Items\n- [ ] **SE** — Schedule technical deep-dive with security lead by June 14\n- [ ] **Champion** — Introduce SE to the security reviewer\n",
        "## Action Items\n",
    )
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("action-items" in e.lower() for e in meta.validation_errors)


def test_post_call_zero_source_coverage(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md").replace("(612 / 612 lines)", "(0 / 0 lines)")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("greater than 0" in e.lower() for e in meta.validation_errors)


def test_post_call_invalid_mode(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-brief.md")
    meta = output_schema.parse_output("post-call", text, mode="verbose")  # type: ignore[arg-type]
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("mode" in e.lower() for e in meta.validation_errors)


def test_legacy_non_strict_skill_returns_unvalidated_when_markers_missing() -> None:
    # Legacy outputs without current-format markers should not be marked invalid.
    text = "# Old Style Output\n\nSome prose without At a Glance or source coverage."
    meta = output_schema.parse_output("biz-qual", text, mode="full")
    assert meta.valid is True
    assert meta.validation_status == "unvalidated"
