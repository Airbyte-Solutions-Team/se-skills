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
import os
import re
import shutil
import subprocess
import sys
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
        # A skipped browser test is not browser validation: in CI this must fail
        # loudly rather than let a green run stand in for reader DOM coverage.
        if os.environ.get("CI"):
            pytest.fail("no Chrome/Chromium binary on the CI runner; reader DOM coverage cannot silently skip")
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
        # Reordered but equivalent: the contract is the header set, not its order.
        (["System", "Top risk", "Confidence", "Availability", "Connector"], "system-records"),
        # Extra columns are fine as long as the contract is still recognizable.
        (["System", "Connector", "Exists", "Availability", "Owner", "Ticket"], "system-records"),
        # Partial: only one contract field present.
        (["System", "Connector", "Owner", "Ticket", "Notes"], "generic"),
        # Malformed: blank header cells must not be counted as contract fields.
        (["System", "", "", "", ""], "generic"),
        # Right fields, wrong first column: not a system-by-system table.
        (["Workstream", "Connector", "Exists", "Availability", "Confidence"], "generic"),
    ],
    ids=[
        "canonical", "variant-headers", "too-narrow", "wide-generic", "system-but-unrelated", "empty",
        "reordered-equivalent", "extra-columns", "partial-contract", "malformed-blank", "wrong-first-column",
    ],
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
    assert sheet.count('class="action-owner"') == 2
    assert sheet.count('class="action-due"') == 2
    assert sheet.count('class="action-status"') == 2
    # 2 actions × 3 columns each, minus the 4 populated fields above = 2 placeholders
    assert sheet.count('class="action-empty"') == 2


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


def test_prep_call_gallery_fixture_carries_the_canonical_prep_architecture(repo_root: Path) -> None:
    """The Prep Call preview must exercise Prep Call, not another skill's shape."""
    from architecture import get_architecture

    arch = get_architecture("prep-call")
    assert arch is not None
    markdown_text = _fixture_md(repo_root, "prep-call")
    h2s = [line[3:].strip() for line in markdown_text.splitlines() if line.startswith("## ")]
    h3s = [line[4:].strip() for line in markdown_text.splitlines() if line.startswith("### ")]

    assert [arch.canonical_key(h) for h in h2s] == arch.canonical_h2_order
    assert h3s[0] == arch.top_summary_name
    # The Discovery Plan groups this review depends on are actually present.
    discovery = arch.h3_groups["discovery-plan"]
    keys = [re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-") for h in h3s]
    assert set(discovery) <= set(keys)


def test_must_ask_is_the_primary_discovery_group_and_siblings_are_not(
    repo_root: Path, tmp_path: Path
) -> None:
    sheet = _build_document(repo_root, _fixture_md(repo_root, "prep-call"), tmp_path)["sheetHtml"]
    assert sheet.count('class="qa-group qa-group--primary"') == 1
    assert sheet.count("qa-group--supporting") == 2
    primary = sheet[sheet.index("qa-group--primary"):sheet.index("qa-group--supporting")]
    assert "Must-Ask Questions" in primary
    for sibling in ("Implication-Depth Questions", "Persona-Specific Questions"):
        assert sibling not in primary


@pytest.mark.parametrize(
    ("markdown_text", "grouped"),
    [
        pytest.param(
            "# Doc\n\n## Discovery Plan\n\n### Must-Ask Questions\n\n- **Q1?**\n\n"
            "### Persona-Specific Questions\n\n- **Q2?**\n",
            True,
            id="discovery_h2_with_must_ask_sibling",
        ),
        pytest.param(
            "# Doc\n\n## Discovery Plan\n\n### Implication-Depth Questions\n\n- **Q1?**\n\n"
            "### Persona-Specific Questions\n\n- **Q2?**\n",
            False,
            id="no_must_ask_group_present",
        ),
        pytest.param(
            "# Doc\n\n## Discovery Plan\n\n### Must-Ask Questions\n\n- **Q1?**\n",
            False,
            id="single_group_has_nothing_to_rank",
        ),
        pytest.param(
            "# Doc\n\n## Objections & Open Questions\n\n### Must-Ask Questions\n\n- **Q1?**\n\n"
            "### Other Questions\n\n- **Q2?**\n",
            False,
            id="must_ask_outside_a_discovery_section",
        ),
    ],
)
def test_discovery_grouping_requires_the_canonical_structure(
    repo_root: Path, tmp_path: Path, markdown_text: str, grouped: bool
) -> None:
    sheet = _build_document(repo_root, markdown_text, tmp_path)["sheetHtml"]
    assert ("qa-group" in sheet) is grouped
    assert "Q1?" in sheet


def test_discovery_grouping_preserves_markdown_order_and_content(
    repo_root: Path, tmp_path: Path
) -> None:
    markdown_text = _fixture_md(repo_root, "prep-call")
    doc = _build_document(repo_root, markdown_text, tmp_path)
    sheet = doc["sheetHtml"]
    groups = ["Must-Ask Questions", "Implication-Depth Questions", "Persona-Specific Questions"]
    assert [g for g in groups if g in sheet] == groups
    assert sorted(groups, key=sheet.index) == groups
    assert [t["text"] for t in doc["tocEntries"] if t["text"] in groups] == groups
    for value in (
        "who gets paged",
        "is CDC off because of the version",
        "$95K/yr",
        "what is the next thing your team would be judged on",
        "where the data plane runs",
    ):
        assert value in sheet


def test_unrelated_wide_table_next_to_a_connector_table_stays_a_table(
    repo_root: Path, tmp_path: Path
) -> None:
    """Recognition is per-table: one reshape must not capture its neighbours."""
    md = (
        "# Doc\n\n## System-by-System Feasibility\n\n"
        "| System | Connector | Exists | Availability | Use-case fit | Confidence | Top risk |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| Postgres | source-postgres | yes | GA | strong | high | CDC slot limits |\n\n"
        "### Timeline\n\n"
        "| Workstream | Owner | Start | End | Dependency | Notes |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| Security review | Dana | 09-02 | 09-09 | none | booked |\n\n"
        "## Source Coverage\n\n- one source\n"
    )
    sheet = _build_document(repo_root, md, tmp_path)["sheetHtml"]
    assert sheet.count('class="sys-record"') == 1
    assert 'class="md-table-wrap"' in sheet
    for value in ("Security review", "Dana", "09-09", "booked", "CDC slot limits", "source-postgres"):
        assert value in sheet


# ── Extraction parity: the reader contract that existed before UX-011 ────────

def test_extraction_preserves_the_pre_existing_reader_contract(
    repo_root: Path, tmp_path: Path
) -> None:
    """Behavior that `openOutput()` owned before the module split still holds."""
    markdown_text = (
        repo_root / "eval" / "fixtures" / "outputs" / "post-call-canonical.md"
    ).read_text(encoding="utf-8")
    doc = _build_document(repo_root, markdown_text, tmp_path)

    # Document title is extracted from the H1 and removed from the body.
    first_h1 = next(
        line[2:].strip() for line in markdown_text.splitlines() if line.startswith("# ")
    )
    assert doc["docTitle"] == first_h1
    assert "<h1" not in doc["sheetHtml"]

    # Profile summary promoted into the lead surface (tile card when the summary
    # exposes tileable labels, otherwise the glance panel), and kept out of the sidebar.
    assert "exec-card" in doc["sheetHtml"] or "is-glance" in doc["sheetHtml"]
    sidebar_labels = [t["text"] for t in doc["tocEntries"]]
    summary_names = _run_node(
        f"const r = require({str(_reader_path(repo_root))!r});"
        "console.log(JSON.stringify({names: r.PROFILE_SUMMARY_NAMES}));"
    )["names"]
    assert not set(sidebar_labels) & set(summary_names + ["At a Glance"])

    # Source order preserved for H2/H3, Source Coverage last, collapsible sections
    # and the risk strip all still produced by the same call.
    md_headings = [
        line.lstrip("#").strip()
        for line in markdown_text.splitlines()
        if line.startswith("## ") or line.startswith("### ")
    ]
    assert sidebar_labels == [h for h in md_headings if h in sidebar_labels]
    assert sidebar_labels[-1] == "Source Coverage"
    assert "doc-toc-link" in doc["tocHtml"]
    assert "sec-body" in doc["sheetHtml"] and "sec-summary" in doc["sheetHtml"]
    assert isinstance(doc["riskStripHtml"], str)


def test_reader_helpers_are_defined_once(repo_root: Path) -> None:
    """No duplicated summary predicate or renderer logic left behind in app.js."""
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    reader_js = (repo_root / "webapp" / "static" / "reader.js").read_text(encoding="utf-8")
    for symbol in (
        "PROFILE_SUMMARY_NAMES",
        "function isSummaryHeadingText",
        "function filterSummaryTocEntries",
        "function classifyTableShape",
        "function buildReaderDocument",
    ):
        assert symbol in reader_js
        assert symbol not in app_js


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


def test_hosted_mode_exposes_no_gallery_route(repo_root: Path) -> None:
    """The gallery is developer tooling on the local side of the trust boundary."""
    env = {
        **os.environ,
        "HOSTED_MODE": "1",
        "PYTHONPATH": f"{repo_root}{os.pathsep}{repo_root / 'webapp'}",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key",
        "HOSTED_JWT_ALGORITHM": "HS256",
        "HOSTED_JWT_SECRET": "super-secret-32-byte-test-jwt-key!",
        "SUPABASE_JWT_SECRET": "super-secret-32-byte-test-storage-jwt-key!",
        "HOSTED_CONTEXT_SECRET": "test-context-secret-32-bytes!!",
    }
    probe = (
        "import json, webapp.app as a;"
        "print(json.dumps([getattr(r, 'path', '') for r in a.app.routes]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert paths, "hosted app registered no routes; the probe is not proving anything"
    assert not [p for p in paths if "gallery" in p]


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
    # Discovery groups differ by weight only, and must never force overflow.
    assert ".qa-group--primary" in css and ".qa-group--supporting" in css
    assert ".md-body .qa-group { min-width: 0; }" in css


def _chrome_bin() -> str:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        if os.environ.get("CI"):
            pytest.fail("no Chrome/Chromium binary on the CI runner; gallery viewport coverage cannot silently skip")
        pytest.skip("no Chrome binary available for DOM behavior tests")
    return chrome


def test_gallery_width_selection_is_a_real_viewport_for_production_media_queries(
    repo_root: Path, tmp_path: Path
) -> None:
    """The 420px selection must make the production @media rules actually fire.

    The gallery renders into an iframe, so narrowing it narrows a real viewport:
    `.kv-grid` stacks inside the frame while the wide outer page keeps two
    columns, using only the production stylesheet (no gallery-only rules).
    """
    chrome = _chrome_bin()
    css = (repo_root / "webapp" / "static" / "style.css").as_posix()
    body = '<div class="md-body"><div class="kv-grid"><div class="kv"><div class="kv-k">Key</div><div class="kv-v">Value</div></div></div></div>'
    harness = tmp_path / "viewport.html"
    frame_html = (
        '<!doctype html><html data-theme="dark"><head><meta charset="utf-8">'
        f'<link rel="stylesheet" href="file://{css}"></head><body><main>{body}</main></body></html>'
    )
    harness.write_text(
        '<!doctype html><html data-theme="dark"><head><meta charset="utf-8">'
        f'<link rel="stylesheet" href="file://{css}"></head><body>'
        f'<pre id="out"></pre><main>{body}</main>'
        '<iframe id="frame" class="gallery-frame" style="width:420px"></iframe>'
        "<script>\n"
        f"const frameHtml = {json.dumps(frame_html)};\n"
        "const f = document.getElementById('frame');\n"
        "const d = f.contentDocument;\n"
        "d.open(); d.write(frameHtml); d.close();\n"
        "function cols(doc) { return getComputedStyle(doc.querySelector('.kv-grid')).gridTemplateColumns; }\n"
        "setTimeout(() => {\n"
        "  document.getElementById('out').textContent = JSON.stringify({\n"
        "    frameWidth: f.contentWindow.innerWidth,\n"
        "    frameCols: cols(d),\n"
        "    outerCols: cols(document),\n"
        "    outerWidth: innerWidth,\n"
        "  });\n"
        "}, 500);\n"
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
            "--window-size=1400,900",
            "--virtual-time-budget=5000",
            "--dump-dom",
            f"file://{harness}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    payload = dom[dom.index('<pre id="out">') + len('<pre id="out">'):]
    payload = payload[: payload.index("</pre>")]
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")):
        payload = payload.replace(entity, char)
    data = json.loads(payload)

    # The frame's own viewport is the selected width (minus its 1px preview border).
    assert 400 < data["frameWidth"] <= 420, data
    assert data["outerWidth"] > 700, data
    # Inside the frame the narrow breakpoint applies: one column.
    assert len(data["frameCols"].split()) == 1, data
    # The outer page is wide, so the same markup keeps the label/value columns.
    assert len(data["outerCols"].split()) == 2, data


def test_gallery_has_no_gallery_specific_responsive_rules(repo_root: Path) -> None:
    """Truthfulness: the frame must inherit production breakpoints, not copies."""
    css = (repo_root / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".gallery-doc.width-narrow" not in css and ".gallery-doc.width-tablet" not in css
    for block in re.findall(r"@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", css):
        assert ".gallery" not in block

    gallery_js = (repo_root / "webapp" / "static" / "gallery.js").read_text(encoding="utf-8")
    assert 'href="/style.css"' in gallery_js
    assert 'narrow: "420px"' in gallery_js and 'tablet: "780px"' in gallery_js


def test_gallery_frame_height_shrinks_when_the_document_gets_shorter(
    repo_root: Path, tmp_path: Path
) -> None:
    """Collapsing a section must not leave dead space inside the preview frame.

    `documentElement.scrollHeight` inside an iframe is floored at the frame's own
    viewport height, so measuring it makes the frame grow-only. The production
    `syncFrameHeight` is loaded here and driven against a shrinking document.
    """
    chrome = _chrome_bin()
    gallery_js = (repo_root / "webapp" / "static" / "gallery.js").as_posix()
    harness = tmp_path / "frameheight.html"
    harness.write_text(
        '<!doctype html><html><head><meta charset="utf-8"></head><body>'
        '<pre id="out"></pre><main id="view"></main>'
        '<div id="gallery-error" class="hidden"></div><div id="gallery-picker"></div>'
        '<div id="gallery-doc"></div>'
        '<select id="gallery-width"><option value="full">full</option>'
        '<option value="narrow">narrow</option></select>'
        "<script>window.fetch = () => Promise.resolve({ ok: true, "
        "json: () => Promise.resolve({ fixtures: [] }), text: () => Promise.resolve('') });"
        "window.seReader = {};</script>"
        f'<script src="file://{gallery_js}"></script>'
        "<script>\n"
        "const frame = document.createElement('iframe');\n"
        "frame.className = 'gallery-frame';\n"
        "frame.style.width = '780px';\n"
        "document.getElementById('gallery-doc').appendChild(frame);\n"
        "const d = frame.contentDocument;\n"
        "d.open();\n"
        "d.write('<!doctype html><body style=\"margin:0\"><div id=\"tall\" style=\"height:3000px\"></div></body>');\n"
        "d.close();\n"
        "syncFrameHeight(frame);\n"
        "const tall = frame.offsetHeight;\n"
        "d.getElementById('tall').style.height = '400px';\n"
        "syncFrameHeight(frame);\n"
        "const short = frame.offsetHeight;\n"
        "document.getElementById('out').textContent = JSON.stringify({ tall, short });\n"
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
            "--window-size=1400,900",
            "--virtual-time-budget=5000",
            "--dump-dom",
            f"file://{harness}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    payload = dom[dom.index('<pre id="out">') + len('<pre id="out">'):]
    payload = payload[: payload.index("</pre>")]
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")):
        payload = payload.replace(entity, char)
    data = json.loads(payload)

    assert 2990 <= data["tall"] <= 3010, data
    # The frame must follow the content back down instead of ratcheting upward.
    assert 390 <= data["short"] <= 420, data
