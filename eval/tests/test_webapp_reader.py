"""Behavioral tests for the shared reader presentation layer and Output Gallery.

`webapp/static/reader.js` owns every presentation-only transformation applied to
the sanitized HTML returned by `/api/output/render`, and the gallery renders
committed synthetic fixtures through that exact path. The pure classifiers are
exercised through Node; the DOM behavior is exercised by running the real module
inside headless Chrome over real server-rendered fixture HTML, so no browser
test toolchain is introduced.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.output_service import OutputService
from webapp import app as app_module

GALLERY_FIXTURES = (
    "prep-call",
    "post-call",
    "biz-qual",
    "tech-qual",
    "deployment-qual",
    "connector-feasibility",
    "deal-assessment",
    "poc-plan",
    "roi-business-case",
    "mutual-close-plan",
    "account-refresher",
    "next-move",
    "edge-cases-long",
    "edge-cases-short",
)


def _reader_path(repo_root: Path) -> Path:
    return repo_root / "webapp" / "static" / "reader.js"


def _run_node(script: str) -> dict:
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _fixture_md(repo_root: Path, name: str) -> str:
    return (repo_root / "eval" / "fixtures" / "gallery" / f"{name}.md").read_text(encoding="utf-8")


def _build_document(repo_root: Path, markdown_text: str, tmp_path: Path) -> dict:
    """Run buildReaderDocument on server-rendered HTML inside headless Chrome."""
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        pytest.skip("no Chrome binary available for DOM behavior tests")
    server_html = OutputService.render_markdown(markdown_text)
    harness = tmp_path / "harness.html"
    harness.write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"></head><body><pre id=\"out\"></pre>"
        f"<script src=\"file://{_reader_path(repo_root)}\"></script>"
        "<script>\n"
        f"const serverHtml = {json.dumps(server_html)};\n"
        "const doc = window.seReader.buildReaderDocument(serverHtml, { title: 'Fixture' });\n"
        "document.getElementById('out').textContent = JSON.stringify(doc);\n"
        "</script></body></html>",
        encoding="utf-8",
    )
    dom = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--allow-file-access-from-files",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--virtual-time-budget=5000",
            "--dump-dom",
            f"file://{harness}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    start = dom.index('<pre id="out">') + len('<pre id="out">')
    end = dom.index("</pre>", start)
    payload = dom[start:end]
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")):
        payload = payload.replace(entity, char)
    return json.loads(payload)


# ── Pure classifiers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["System", "Connector", "Exists", "Availability", "Use-case fit", "Confidence", "Top risk"], "system-records"),
        (["Source system", "Connector", "Exists", "GA status", "Fit"], "system-records"),
        (["System", "Connector"], "generic"),
        (["Workstream", "Owner", "Start", "End", "Dependency", "Notes"], "generic"),
        (["System", "Notes", "Comment", "Detail"], "generic"),
        ([], "generic"),
    ],
    ids=["canonical", "variant-headers", "too-narrow", "wide-generic", "system-but-unrelated", "empty"],
)
def test_only_a_confident_connector_table_shape_is_reshaped(repo_root: Path, headers, expected) -> None:
    data = _run_node(
        f"const r = require({str(_reader_path(repo_root))!r});"
        f"console.log(JSON.stringify({{shape: r.classifyTableShape({json.dumps(headers)})}}));"
    )
    assert data["shape"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Priya Raman — VP Data Platform, economic buyer", {"name": "Priya Raman", "detail": "VP Data Platform, economic buyer"}),
        ("Bo Lin: Procurement Lead", {"name": "Bo Lin", "detail": "Procurement Lead"}),
        ("We agreed to move the security review earlier. It is now booked.", None),
        ("the team owns this — and nobody else does", None),
    ],
    ids=["dash", "colon", "prose", "lowercase-lead"],
)
def test_person_rows_are_recognized_only_when_the_shape_is_a_person(repo_root: Path, text, expected) -> None:
    data = _run_node(
        f"const r = require({str(_reader_path(repo_root))!r});"
        f"console.log(JSON.stringify({{parsed: r.parsePersonEntry({json.dumps(text)})}}));"
    )
    parsed = data["parsed"]
    if expected is None:
        assert parsed is None
    else:
        assert parsed["name"] == expected["name"]
        assert parsed["detail"].startswith(expected["detail"][:12])


@pytest.mark.parametrize(
    ("text", "action", "labels"),
    [
        ("Book the review · Owner: Dana · Due: 2026-09-09", "Book the review", ["Owner", "Due"]),
        ("Send the questionnaire · Owner: Devin · Status: not started", "Send the questionnaire", ["Owner", "Status"]),
        ("Book the review with the platform team", None, []),
        ("Two clauses · both are plain prose", None, []),
    ],
    ids=["owner-due", "owner-status", "no-metadata", "no-labels"],
)
def test_action_metadata_is_split_only_when_labeled(repo_root: Path, text, action, labels) -> None:
    data = _run_node(
        f"const r = require({str(_reader_path(repo_root))!r});"
        f"console.log(JSON.stringify({{split: r.splitActionMeta({json.dumps(text)})}}));"
    )
    split = data["split"]
    if action is None:
        assert split is None
    else:
        assert split["action"] == action
        assert [m["label"] for m in split["meta"]] == labels


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Source Coverage", "evidence"),
        ("Who's Who", "people"),
        ("Loss Risks", "risk"),
        ("Action Items", "action"),
        ("Open Questions / Follow-Ups", "questions"),
        ("Why Airbyte", "thesis"),
        ("Scope & Technical Changes", "plain"),
    ],
    ids=["evidence", "people", "risk", "action", "questions", "thesis", "plain"],
)
def test_section_roles_come_from_headings(repo_root: Path, title, role) -> None:
    data = _run_node(
        f"const r = require({str(_reader_path(repo_root))!r});"
        f"console.log(JSON.stringify({{role: r.sectionRole({json.dumps(title)})}}));"
    )
    assert data["role"] == role


def test_summary_promotion_and_sidebar_exclusion_share_one_predicate(repo_root: Path) -> None:
    """A profile summary must never be both promoted and listed in the sidebar."""
    data = _run_node(
        f"""
const r = require({str(_reader_path(repo_root))!r});
const toc = r.PROFILE_SUMMARY_NAMES.map((name, i) => ({{level: 3, text: name, id: "s" + i}}))
  .concat([{{level: 2, text: "Key Takeaways", id: "kt"}}, {{level: 3, text: "At a Glance", id: "ag"}}]);
console.log(JSON.stringify({{
  promoted: r.PROFILE_SUMMARY_NAMES.map((n) => r.summaryHeadingLabelText(n)),
  remaining: r.filterSummaryTocEntries(toc).map((t) => t.text),
}}));
"""
    )
    assert all(data["promoted"])
    assert data["remaining"] == ["Key Takeaways"]


# ── DOM behavior through the real module ─────────────────────────────────────

def test_connector_table_becomes_vertical_records_and_wide_table_scrolls(repo_root: Path, tmp_path: Path) -> None:
    connector = _build_document(repo_root, _fixture_md(repo_root, "connector-feasibility"), tmp_path)["sheetHtml"]
    assert connector.count('class="sys-record"') >= 5
    assert 'class="sys-field-k"' in connector
    # Every cell of the recognized table survives the reshape.
    assert "source-postgres" in connector

    generic = _build_document(repo_root, _fixture_md(repo_root, "edge-cases-long"), tmp_path)["sheetHtml"]
    assert 'class="md-table-wrap"' in generic
    assert 'class="sys-record"' not in generic


def test_ordinary_sections_are_not_all_promoted_to_surfaces(repo_root: Path, tmp_path: Path) -> None:
    doc = _build_document(repo_root, _fixture_md(repo_root, "post-call"), tmp_path)
    sheet = doc["sheetHtml"]
    plain = sheet.count("doc-section--role-plain")
    surfaced = sheet.count("doc-section--role-evidence") + sheet.count("doc-section--blocker") \
        + sheet.count("doc-section--win") + sheet.count("doc-section--risk") + sheet.count("is-glance")
    assert plain >= surfaced


def test_source_coverage_stays_last_and_visible(repo_root: Path, tmp_path: Path) -> None:
    doc = _build_document(repo_root, _fixture_md(repo_root, "deal-assessment"), tmp_path)
    sheet = doc["sheetHtml"]
    assert sheet.rindex("Source Coverage") > sheet.rindex("Recommended Actions")
    assert "_transcripts/Northwind-08.14.26.txt" in sheet
    assert "display: none" not in sheet
    labels = [t["text"] for t in doc["tocEntries"]]
    assert labels[-1] == "Source Coverage"


def test_sidebar_order_matches_markdown_heading_order(repo_root: Path, tmp_path: Path) -> None:
    markdown_text = _fixture_md(repo_root, "poc-plan")
    doc = _build_document(repo_root, markdown_text, tmp_path)
    md_headings = [
        line.lstrip("#").strip()
        for line in markdown_text.splitlines()
        if line.startswith("## ") or line.startswith("### ")
    ]
    sidebar = [t["text"] for t in doc["tocEntries"]]
    assert sidebar == [h for h in md_headings if h in sidebar]


def test_callouts_keep_their_semantics_and_feed_the_risk_strip(repo_root: Path, tmp_path: Path) -> None:
    md = (
        "# Doc\n\n### Recommendation\n- **Verdict:** 🟢 go\n\n## Loss Risks\n\n"
        "> [!blocker]\n> Security review is not booked.\n\n"
        "> [!risk]\n> The champion is the only advocate.\n\n"
        "> [!info]\n> Procurement freezes in the last week of the quarter.\n\n"
        "## Source Coverage\n\n- one source\n"
    )
    doc = _build_document(repo_root, md, tmp_path)
    assert "callout-blocker" in doc["sheetHtml"]
    assert "callout-risk" in doc["sheetHtml"]
    assert "callout-info" in doc["sheetHtml"]
    strip = doc["riskStripHtml"]
    assert "Security review is not booked." in strip
    assert "champion is the only advocate" in strip
    assert "Procurement freezes" not in strip
    assert strip.index("blocker") < strip.index("risk</span>")


def test_people_and_action_transformations_preserve_every_value(repo_root: Path, tmp_path: Path) -> None:
    md = (
        "# Doc\n\n## Who's Who\n\n"
        "- **Priya Raman** — VP Data Platform, owns the budget line\n"
        "- **Marcus Feld** — Staff Data Engineer, ran the pilot\n\n"
        "## Action Items\n\n"
        "- Book the architecture review · Owner: Dana Okafor · Due: 2026-09-09\n"
        "- Send the questionnaire · Owner: Devin Ortiz · Status: not started\n"
    )
    doc = _build_document(repo_root, md, tmp_path)
    sheet = doc["sheetHtml"]
    for value in (
        "Priya Raman", "VP Data Platform, owns the budget line",
        "Marcus Feld", "ran the pilot",
        "Book the architecture review", "Dana Okafor", "2026-09-09",
        "Send the questionnaire", "Devin Ortiz", "not started",
    ):
        assert value in sheet
    assert sheet.count('class="person"') == 2
    assert sheet.count('class="action-chip"') == 4


def test_hostile_generated_content_stays_inert_through_the_reader(repo_root: Path, tmp_path: Path) -> None:
    md = (
        "# <img src=x onerror=alert(1)>\n\n"
        "### Recommendation\n- **Verdict:** <script>alert(2)</script> go\n\n"
        "## Who's Who\n\n- **<script>alert(3)</script>** — <iframe src=\"javascript:alert(4)\"></iframe>\n\n"
        "## Action Items\n\n- Do it <svg onload=alert(5)> · Owner: <b onmouseover=alert(6)>x</b>\n\n"
        "## Source Coverage\n\n- `file.txt` — 1 / 1 lines\n"
    )
    doc = _build_document(repo_root, md, tmp_path)
    blob = json.dumps(doc)
    assert "<script" not in blob
    assert "<iframe" not in blob
    assert "onerror=" not in blob
    assert "onload=" not in blob
    assert "onmouseover=" not in blob
    assert "javascript:" not in blob


@pytest.mark.parametrize("name", GALLERY_FIXTURES)
def test_every_gallery_fixture_renders_through_the_reader(repo_root: Path, tmp_path: Path, name: str) -> None:
    doc = _build_document(repo_root, _fixture_md(repo_root, name), tmp_path)
    assert doc["docTitle"]
    assert doc["sheetHtml"].strip()
    assert doc["tocHtml"].strip()
    assert "<h1" not in doc["sheetHtml"]


# ── Gallery route + wiring ───────────────────────────────────────────────────

def test_gallery_serves_only_allowlisted_committed_fixtures() -> None:
    with TestClient(app_module.app) as client:
        listing = client.get("/api/gallery/fixtures")
        assert listing.status_code == 200
        names = [f["name"] for f in listing.json()["fixtures"]]
        assert set(GALLERY_FIXTURES).issubset(set(names))

        ok = client.get("/api/gallery/fixture", params={"name": "deal-assessment"})
        assert ok.status_code == 200
        assert ok.text.startswith("# Deal Assessment")

        for hostile in ("../../../etc/passwd", "deal-assessment.md", "..", "outputs/secret", "DEAL"):
            assert client.get("/api/gallery/fixture", params={"name": hostile}).status_code == 404


def test_gallery_uses_the_production_rendering_path(repo_root: Path) -> None:
    gallery_js = (repo_root / "webapp" / "static" / "gallery.js").read_text(encoding="utf-8")
    subprocess.run(["node", "--check", str(repo_root / "webapp" / "static" / "gallery.js")], check=True)
    assert '"/api/output/render"' in gallery_js
    assert "window.seReader.buildReaderDocument(" in gallery_js
    # No second renderer: the gallery must not parse Markdown itself.
    assert "marked" not in gallery_js and "markdown(" not in gallery_js

    gallery_html = (repo_root / "webapp" / "static" / "gallery.html").read_text(encoding="utf-8")
    assert gallery_html.index("/reader.js") < gallery_html.index("/gallery.js")
    assert "/style.css" in gallery_html


def test_reader_module_is_loaded_before_app_js(repo_root: Path) -> None:
    index_html = (repo_root / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert index_html.index("/reader.js?v=") < index_html.index("/app.js?v=")
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.seReader.buildReaderDocument(" in app_js


def test_narrow_layout_rules_exist_for_every_new_component(repo_root: Path) -> None:
    """Narrow widths must stack the components that would otherwise clip."""
    css = (repo_root / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 700px)"):]
    for selector in (".sys-fields", ".person", ".kv-grid", ".tile-grid"):
        assert selector in narrow
    assert "overflow-x: auto" in css
