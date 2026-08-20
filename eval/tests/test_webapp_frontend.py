"""Focused correctness checks for the no-build local webapp frontend."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_app_js_parses_and_normalizes_output_meta(repo_root: Path) -> None:
    """The frontend has no JS test harness, so verify syntax and shared routing."""
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    subprocess.run(["node", "--check", str(repo_root / "webapp" / "static" / "app.js")], check=True)

    assert "function normalizeOutputMeta(o)" in app_js
    assert "normalizeOutputMeta(o)]" in app_js
    assert "const meta = rawMeta ? normalizeOutputMeta(rawMeta) : null;" in app_js
    assert app_js.count("normalizeOutputMeta(") >= 4


def test_doc_status_helper_behaves_truthfully_across_validation_states(repo_root: Path) -> None:
    """Exercise the pure status helper through Node rather than source matching."""
    helper = repo_root / "webapp" / "static" / "doc_status.js"
    script = f"""
const docStatus = require({str(helper)!r});
const cases = {{
  invalid: docStatus({{validation_status: "invalid", validation_errors: ["Missing Source Coverage"], reference_sources_tracked: false}}),
  unsupported: docStatus({{validation_status: "unvalidated", validation_supported: false, reference_sources_tracked: false}}),
  legacy: docStatus({{validation_status: "unvalidated", validation_supported: true, reference_sources_tracked: false}}),
  valid: docStatus({{validation_status: "valid", valid: true, reference_sources_tracked: false}}),
  trackedLegacy: docStatus({{validation_status: "unvalidated", validation_supported: true, reference_sources_tracked: true, reference_freshness_at_generation: null}}),
  untrackedLegacy: docStatus({{validation_status: "unvalidated", validation_supported: true, reference_sources_tracked: false, reference_freshness_at_generation: null}}),
  stale: docStatus({{validation_status: "valid", valid: true, reference_sources_tracked: true, reference_freshness_at_generation: [{{label: "Platform", fresh: false}}]}})
}};
console.log(JSON.stringify(cases));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    cases = json.loads(result.stdout)

    assert cases["invalid"]["severity"] == "error"
    assert cases["invalid"]["issues"][0]["text"] == (
        "Automatic checks found: Missing Source Coverage"
    )
    assert "Missing Source Coverage" in cases["invalid"]["issues"][0]["text"]
    assert cases["unsupported"]["severity"] == "ok"
    assert cases["unsupported"]["issues"][0]["text"] == (
        "Automatic structure checks are not defined for this output type."
    )
    assert cases["legacy"]["severity"] == "info"
    assert cases["legacy"]["label"] == "Checks unavailable"
    assert cases["legacy"]["issues"][0]["text"] == (
        "Automatic structure check unavailable for this older output format. "
        "Regenerate it to use current automatic checks."
    )
    assert cases["valid"]["severity"] == "ok"
    assert cases["trackedLegacy"]["severity"] == "warn"
    assert any(issue["text"] == (
        "Reference snapshot unavailable for this output — product-reference claims "
        "may predate current reference data."
    ) for issue in cases["trackedLegacy"]["issues"])
    assert cases["untrackedLegacy"]["severity"] == "info"
    assert not any("Reference snapshot" in issue["text"] for issue in cases["untrackedLegacy"]["issues"])
    assert cases["stale"]["severity"] == "warn"
    assert "Reference data was stale/missing when generated" in cases["stale"]["issues"][0]["text"]


def test_doc_status_escape_html_handles_generated_validation_text(repo_root: Path) -> None:
    """Generated validation details must stay data inside output-list tooltips."""
    helper = repo_root / "webapp" / "static" / "doc_status.js"
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    script = f"""
const docStatus = require({str(helper)!r});
const raw = '\\"><img src=x onerror=alert(1)>&amp; \\'quoted\\' </span>';
const result = docStatus({{validation_status: "invalid", validation_errors: [raw]}});
console.log(JSON.stringify({{issue: result.issues[0].text, escaped: docStatus.escapeHtml(result.issues[0].text)}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert "<" not in data["escaped"]
    assert ">" not in data["escaped"]
    assert '"' not in data["escaped"]
    assert "&lt;img" in data["escaped"]
    assert "&quot;" in data["escaped"]
    assert "&amp;" in data["escaped"]
    assert "&#39;" in data["escaped"]
    assert "&lt;/span&gt;" in data["escaped"]
    assert "window.docStatus.escapeHtml(warnTitle)" in app_js
    assert 'title="${warnTitle}' not in app_js
    assert app_js.count("safeWarnTitle") >= 4
