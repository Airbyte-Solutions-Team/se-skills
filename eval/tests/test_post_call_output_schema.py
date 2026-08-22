"""Deterministic validation tests for the post-call output contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import output_schema
import services.output_service as output_service_module
from services.output_service import OutputService


def _load_fixture(repo_root: Path, filename: str) -> str:
    return (repo_root / "eval" / "fixtures" / "outputs" / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "filename, mode",
    [
        pytest.param("post-call-full.md", "full", id="valid-full"),
        pytest.param("post-call-brief.md", "brief", id="valid-brief"),
        pytest.param("post-call-canonical.md", "full", id="valid-canonical"),
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
    text = _load_fixture(repo_root, "post-call-brief.md").replace("## Actions & Next Step", "## Removed")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("actions-and-next-step" in e for e in meta.validation_errors)


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


@pytest.mark.parametrize("placeholder", ["[action]", "[Customer Name]", "[Owner]"])
def test_post_call_rejects_unresolved_placeholder(placeholder: str, repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-canonical.md").replace(
        "The team needs a managed deployment before renewal.",
        f"The team needs {placeholder} before renewal.",
    )
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is False
    assert any(placeholder in e for e in meta.validation_errors)


def test_post_call_markdown_syntax_brackets_are_not_placeholders(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-canonical.md")
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is True
    assert not any("Jump to" in e for e in meta.validation_errors)


def test_post_call_canonical_fixture_keeps_source_coverage_last(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-canonical.md")
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert headings[-1] == "Source Coverage"
    assert headings.index("Coaching Observations") < headings.index("Source Coverage")

    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is True
    assert meta.validation_errors == []


def test_post_call_source_coverage_without_line_counts_is_invalid(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-canonical.md").replace(
        "(612 / 612 lines)", "Transcript was reviewed.",
    )
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is False
    assert any("line counts" in e for e in meta.validation_errors)


def test_ae_led_discovery_requires_meddpicc_h3(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-full.md")
    transcript = "The AE led a discovery call to review metrics and the economic buyer."
    missing = output_schema.parse_output("post-call", text, transcript_text=transcript)
    assert missing.valid is False
    assert any("MEDDPICC" in error for error in missing.validation_errors)

    with_meddpicc = text.replace(
        "## Deal Impact\n",
        "## Deal Impact\n\n### MEDDPICC Changes\n- M🟢 E🟡 D🟢 D🟡 P🟢 I🟢 C🟡 C🟢\n",
    )
    valid = output_schema.parse_output(
        "post-call", with_meddpicc, transcript_text=transcript
    )
    assert valid.valid is True, valid.validation_errors


def test_se_attended_call_does_not_require_meddpicc_h3(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-full.md")
    transcript = "The SE attended a technical discovery call about architecture."
    meta = output_schema.parse_output("post-call", text, transcript_text=transcript)
    assert meta.valid is True, meta.validation_errors


def test_sidecar_schema_version_change_reparses_and_rewrites(tmp_path: Path, repo_root: Path) -> None:
    md_path = tmp_path / "post-call.md"
    md_path.write_text(
        _load_fixture(repo_root, "post-call-canonical.md"),
        encoding="utf-8",
    )
    sidecar = md_path.with_suffix(".md.json")
    sidecar.write_text(
        json.dumps({
            "schema_version": 2,
            "skill": "post-call",
            "valid": True,
            "validation_status": "valid",
        }),
        encoding="utf-8",
    )

    meta = output_schema.read_or_parse_sidecar(md_path, "post-call")

    assert meta.valid is True
    rewritten = json.loads(sidecar.read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == 3
    assert rewritten["validation_status"] == "valid"


def _reference_snapshot() -> list[dict[str, object]]:
    return [
        {
            "source": "registry",
            "label": "Connector registry cache",
            "status": "fresh",
            "date": "2026-08-19",
            "age_days": 1,
            "fresh": True,
            "threshold_days": 7,
            "path": "/workspace/registry.json",
        }
    ]


def _write_v1_sidecar(
    md_path: Path,
    snapshot_key: str,
    snapshot: list[dict[str, object]],
    schema_version: int = 1,
) -> Path:
    sidecar = md_path.with_suffix(md_path.suffix + ".json")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "skill": "post-call",
                "valid": False,
                "validation_status": "invalid",
                snapshot_key: snapshot,
            }
        ),
        encoding="utf-8",
    )
    return sidecar


def test_v1_snapshot_survives_reparse_and_output_service_read(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md_path = tmp_path / "Acme" / "outputs" / "post-call" / "post-call.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_load_fixture(repo_root, "post-call-canonical.md"), encoding="utf-8")
    snapshot = _reference_snapshot()
    sidecar = _write_v1_sidecar(
        md_path,
        "reference_freshness_at_generation",
        snapshot,
        schema_version=2,
    )

    metadata = output_schema.read_or_parse_sidecar(md_path, "post-call")

    assert metadata.valid is True
    assert metadata.validation_status == "valid"
    rewritten = json.loads(sidecar.read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == 3
    assert json.dumps(
        rewritten["reference_freshness_at_generation"], separators=(",", ":")
    ) == json.dumps(snapshot, separators=(",", ":"))

    current = output_schema.ReferenceFreshness.model_validate(snapshot[0])
    changed = output_schema.ReferenceChange(
        source="registry",
        label="Connector registry cache",
        old_date="2026-08-19",
        new_date="2026-08-20",
        old_status="fresh",
        new_status="stale",
    )
    seen: dict[str, object] = {}

    def fake_compute(*args, **kwargs):
        return [current]

    def fake_compare(current_entries, generation_entries):
        seen["generation"] = generation_entries
        return [changed]

    monkeypatch.setattr(output_service_module.reference_freshness, "compute_reference_freshness", fake_compute)
    monkeypatch.setattr(output_service_module.reference_freshness, "compare_to_generation", fake_compare)
    service = OutputService(
        customers_dir=tmp_path,
        workspace=tmp_path,
        repo_root=tmp_path,
        se_config=lambda: {},
        safe_name=lambda value: value,
        slug=lambda value: value.replace(" ", "-").lower(),
    )

    read_metadata = service.read_output_meta("Acme/outputs/post-call/post-call.md")

    assert seen["generation"] == [current]
    assert read_metadata["reference_freshness_at_generation"] == snapshot
    assert read_metadata["reference_changed_since_generation"] == [changed.model_dump()]


def test_legacy_v1_snapshot_migrates_during_reparse(tmp_path: Path, repo_root: Path) -> None:
    md_path = tmp_path / "post-call.md"
    md_path.write_text(_load_fixture(repo_root, "post-call-canonical.md"), encoding="utf-8")
    snapshot = _reference_snapshot()
    sidecar = _write_v1_sidecar(md_path, "reference_freshness", snapshot)

    metadata = output_schema.read_or_parse_sidecar(md_path, "post-call")
    rewritten = json.loads(sidecar.read_text(encoding="utf-8"))

    assert metadata.valid is True
    assert rewritten["schema_version"] == 3
    assert json.dumps(
        rewritten["reference_freshness_at_generation"], separators=(",", ":")
    ) == json.dumps(snapshot, separators=(",", ":"))
    assert "reference_freshness" not in rewritten


def _remove_section(text: str, heading: str) -> str:
    """Remove a section starting with `heading` up to the next H2 or EOF."""
    import re

    pattern = re.compile(rf"^{re.escape(heading)}\b.*?^(?=## |\Z)", re.MULTILINE | re.DOTALL)
    return pattern.sub("", text)


def test_post_call_conditional_sections_absent_legitimately(repo_root: Path) -> None:
    # Remove all conditional sections from the full fixture; the remaining required
    # sections should still validate.
    text = _load_fixture(repo_root, "post-call-full.md")
    for heading in [
        "## Scope & Technical Changes",
        "## Coaching Observations",
    ]:
        text = _remove_section(text, heading)
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is True
    assert meta.validation_status == "valid"


def test_post_call_conditional_section_present_but_empty_is_invalid(repo_root: Path) -> None:
    text = _load_fixture(repo_root, "post-call-full.md").replace(
        "## Scope & Technical Changes",
        "## Scope & Technical Changes\n",
    )
    # Replace the multi-line body up to the next H2 with an empty body.
    text = re.sub(
        r"## Scope & Technical Changes\n.*?^(?=## |\Z)",
        "## Scope & Technical Changes\n\n",
        text,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    meta = output_schema.parse_output("post-call", text, mode="full")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("scope" in e.lower() for e in meta.validation_errors)


def test_post_call_empty_expanded_heading_is_invalid(repo_root: Path) -> None:
    text = _remove_section(_load_fixture(repo_root, "post-call-brief.md"), "## Actions & Next Step")
    text = text.replace("## Source Coverage", "## Actions & Next Step — Final\n\n## Source Coverage")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("actions" in e.lower() for e in meta.validation_errors)


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
    text = _remove_section(_load_fixture(repo_root, "post-call-brief.md"), "## Actions & Next Step")
    text = text.replace("## Source Coverage", "## Actions & Next Step\n\n## Source Coverage")
    meta = output_schema.parse_output("post-call", text, mode="brief")
    assert meta.valid is False
    assert meta.validation_status == "invalid"
    assert any("actions" in e.lower() for e in meta.validation_errors)


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
