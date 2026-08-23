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


def test_profile_summary_is_promoted_with_profile_label(repo_root: Path) -> None:
    """The shared reader module recognizes current profiles and keeps their label."""
    reader_js = repo_root / "webapp" / "static" / "reader.js"
    script = f"""
const reader = require({str(reader_js)!r});
console.log(JSON.stringify({{
  current: reader.summaryHeadingLabelText("Call Snapshot"),
  legacy: reader.summaryHeadingLabelText("At a Glance"),
  unrelated: reader.summaryHeadingLabelText("Deal Thesis")
}}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    labels = json.loads(result.stdout)
    assert labels == {
        "current": "Call Snapshot",
        "legacy": "At a Glance",
        "unrelated": "",
    }

    reader_text = reader_js.read_text(encoding="utf-8")
    assert "summaryHeadingLabel(leadSection)" in reader_text
    assert 'exec-card-eyebrow">${escapeHtml(leadSummaryLabel)}' in reader_text


def test_profile_summary_is_removed_from_sidebar_in_reader_round_trip(repo_root: Path) -> None:
    """The TOC keeps body order while excluding promoted and unpromoted summaries."""
    reader_js = repo_root / "webapp" / "static" / "reader.js"
    script = f"""
const {{ summaryHeadingLabelText, filterSummaryTocEntries }} = require({str(reader_js)!r});
const toc = [
  {{level: 3, text: "Call Snapshot", id: "call-snapshot"}},
  {{level: 2, text: "Key Takeaways", id: "key-takeaways"}},
  {{level: 3, text: "Attendees", id: "attendees"}},
  {{level: 2, text: "Deal Impact", id: "deal-impact"}},
  {{level: 3, text: "At a Glance", id: "at-a-glance"}}
];
if (summaryHeadingLabelText(toc[0].text) !== "Call Snapshot") {{
  throw new Error("profile summary was not promoted with its profile label");
}}
console.log(JSON.stringify(filterSummaryTocEntries(toc)));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    entries = json.loads(result.stdout)
    assert entries == [
        {"level": 2, "text": "Key Takeaways", "id": "key-takeaways"},
        {"level": 3, "text": "Attendees", "id": "attendees"},
        {"level": 2, "text": "Deal Impact", "id": "deal-impact"},
    ]

    reader_text = reader_js.read_text(encoding="utf-8")
    assert "const tocEntries = filterSummaryTocEntries(toc);" in reader_text


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


def test_hosted_review_ui_escapes_review_text_and_reuses_reader_pipeline(repo_root: Path) -> None:
    """The hosted review surface renders review text as escaped plain text.

    Comment bodies, change summaries, and reviewer emails are user-authored, so
    they must only ever reach the DOM through `esc()`. Version content and the
    correction preview must go through the server renderer + `reader.js`, never
    through a second in-browser Markdown path.
    """
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    subprocess.run(["node", "--check", str(repo_root / "webapp" / "static" / "app.js")], check=True)

    assert "async function pageHostedOutputReview(accountId, outputId)" in app_js
    assert 'if (parts[4] === "outputs" && parts[5])' in app_js

    # User-authored review text is escaped everywhere it is rendered.
    assert 'class="review-activity-body">${esc(entry.comment)}' in app_js
    assert "${esc(v.change_summary)}" in app_js
    assert "${esc(who)}" in app_js
    assert "${esc(r.draft != null ? r.draft : r.markdown)}" in app_js

    # Version content and the preview reuse the shared sanitized pipeline.
    assert app_js.count("window.seReader.buildReaderDocument(addMdClasses(") == 2
    assert "/versions/${encodeURIComponent(ref)}/content" in app_js
    assert "/preview`" in app_js

    # The browser never supplies identity, Storage paths, or provenance.
    review_block = app_js.split("// ---- Hosted output review (Slice 6A)")[1]
    for forbidden in (
        "org_id",
        "user_id",
        "created_by:",
        "content_storage_path",
        "validation_status:",
        "sidecar",
        "storage_path",
    ):
        assert forbidden not in review_block, forbidden

    # Corrections and approvals target an exact version and carry an
    # idempotency key so a retry cannot duplicate a version or an approval.
    assert "base_version_id: baseRef || null" in review_block
    assert "r.requestId = r.requestId || _newRequestId();" in review_block
    assert "target_version_id: currentRef === HOSTED_REVIEW_GENERATED_REF ? null : currentRef," in review_block

    # A stale-base conflict keeps the unsaved draft.
    assert "_refreshHostedReviewState({ keepDraft: true })" in review_block


def test_api_error_detail_is_readable_for_structured_validation_failures(repo_root: Path) -> None:
    """A structured 422 detail becomes readable text instead of `[object Object]`.

    The hosted correction endpoint returns `{message, validation_errors}`, so the
    shared `api()` helper must flatten object and list details.
    """
    app_js_path = repo_root / "webapp" / "static" / "app.js"
    script = f"""
const src = require("fs").readFileSync({str(app_js_path)!r}, "utf8");
const start = src.indexOf("function errorDetailText");
const end = src.indexOf("const api = async");
eval(src.slice(start, end));
console.log(JSON.stringify({{
  text: errorDetailText({{detail: "plain"}}),
  structured: errorDetailText({{detail: {{message: "Correction does not satisfy the output contract", validation_errors: ["Missing section: Source Coverage"]}}}}),
  list: errorDetailText({{detail: [{{msg: "field required"}}]}}),
  empty: errorDetailText({{}})
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert data["text"] == "plain"
    assert data["structured"].splitlines() == [
        "Correction does not satisfy the output contract",
        "Missing section: Source Coverage",
    ]
    assert data["list"] == "field required"
    assert data["empty"] == ""
