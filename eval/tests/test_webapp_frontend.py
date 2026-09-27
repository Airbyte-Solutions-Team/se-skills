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


def test_command_center_risk_preview_and_review_rendering(repo_root: Path) -> None:
    """Exercise the no-build renderers with synthetic risk data and check responsive rules."""
    command_center = repo_root / "webapp" / "static" / "command_center.js"
    subprocess.run(["node", "--check", str(command_center)], check=True)
    script = f"""
const fs = require("fs");
const src = fs.readFileSync({json.dumps(str(command_center))}, "utf8");
const body = {{innerHTML: "", querySelector: () => null, querySelectorAll: () => []}};
global.document = {{getElementById: () => body}};
global.view = {{innerHTML: ""}};
global.location = {{hash: "#/command-center/portfolio"}};
global.setCrumbs = () => {{}};
global.emptyBox = (item) => item.title;
global.esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const link = "#/opp/Acme/synthetic-opportunity/synthetic-opportunity/risk/security-review";
const risksLink = "#/opp/Acme/synthetic-opportunity/synthetic-opportunity/risks";
const risk = {{key: "security-review", title: "Security <review>", reason: "Approval is pending <script>", severity: "high", status: "potential", evidence_label: "1 transcript citation(s) · 00:00:05", overview_revision: 2, overview_created_at: "2026-09-26T12:00:00Z", last_updated_at: null, link}};
const critical = {{...risk, key: "critical", title: "Critical synthetic risk", severity: "critical", link: link.replace("security-review", "critical")}};
const hidden = {{...risk, key: "hidden", title: "Hidden third risk", link: link.replace("security-review", "hidden")}};
const hiddenToo = {{...risk, key: "hidden-two", title: "Hidden fourth risk", link: link.replace("security-review", "hidden-two")}};
const row = {{account: "Acme", opportunity_slug: "synthetic-opportunity", opportunity_name: "Synthetic Opportunity", opportunity_link: "#/opp/Acme/synthetic-opportunity/synthetic-opportunity", risks_link: risksLink, overview: {{status: "current"}}, action_counts: {{open: 0, blocked: 0, proposed: 0, completed: 0, overdue: 0}}, next_step: {{state: "unknown", value: null}}, waiting_on: [], confirmed_blockers: [], risks: [critical, risk, hidden, hiddenToo], evaluation: {{supported: false}}, freshness: {{state: "processed_latest_import", label: "Current through latest manual import", source_counts: {{total: 1}}, connector: {{mode: "manual_import", health: "not_monitored"}}}}, attention: [], latest_change_at: null}};
const portfolio = {{opportunities: [row], accounts: ["Acme"], total: 1, offset: 0, limit: 25, next_offset: null}};
const today = {{attention: [{{kind: "risk_review", title: risk.title, reason: risk.reason, account: row.account, opportunity_slug: row.opportunity_slug, opportunity_link: row.opportunity_link, next_step: "Review the risk", link, when: risk.overview_created_at, risk, freshness: {{label: row.freshness.label}}}}], counts_by_kind: {{risk_review: 1}}, recent_changes: [], opportunity_count: 1, total: 1, offset: 0, limit: 25, next_offset: null}};
global.api = async (path) => path.includes("/portfolio") ? portfolio : today;
eval(src + "\\nglobalThis.ccPagePortfolio = ccPagePortfolio; globalThis.ccPageToday = ccPageToday;");
(async () => {{
  await ccPagePortfolio(new URLSearchParams());
  const portfolioHtml = body.innerHTML;
  await ccPageToday(new URLSearchParams());
  const todayHtml = body.innerHTML;
  console.log(JSON.stringify({{
    portfolio: portfolioHtml.includes("Risks</dt>") && portfolioHtml.includes("4 potential risks") && portfolioHtml.includes("View all in Overview") && portfolioHtml.includes(risksLink) && portfolioHtml.includes("Showing 2 most severe") && portfolioHtml.includes("critical · potential") && portfolioHtml.includes("high · potential") && portfolioHtml.includes(link) && portfolioHtml.includes("Approval is pending &lt;script&gt;"),
    compact: !portfolioHtml.includes("Hidden third risk") && !portfolioHtml.includes("Hidden fourth risk"),
    today: todayHtml.includes("Potential risk to review") && todayHtml.includes("Freshness: Current through latest manual import") && todayHtml.includes("1 transcript citation(s)") && todayHtml.includes(link),
    escaped: !portfolioHtml.includes("<script>") && !todayHtml.includes("<script>") && portfolioHtml.includes("Security &lt;review&gt;"),
    noRedRisk: !todayHtml.includes("cc-att--error") && !portfolioHtml.includes("cc-badge--error")
  }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert all(json.loads(result.stdout).values()), result.stdout
    css = (repo_root / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".cc-cards { grid-template-columns: 1fr; }" in css
    assert ".cc-risk-context { align-items: flex-start; }" in css
    assert ".cc-risk-list { list-style: none;" in css
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="overview-risk-${esc(risk.key)}"' in app_js
    assert 'id="overview-risks"' in app_js
    assert "No risks are recorded in this Overview." in app_js
    assert "riskDetail.scrollIntoView" in app_js
    scroll_script = f"""
const fs = require("fs");
const src = fs.readFileSync({json.dumps(str(repo_root / 'webapp' / 'static' / 'app.js'))}, "utf8");
const start = src.indexOf("function scrollToOverviewRisk(");
const end = src.indexOf("async function pageOpportunity", start);
const seen = [];
global.document = {{getElementById: (id) => ({{scrollIntoView: () => seen.push(id)}})}};
eval(src.slice(start, end));
scrollToOverviewRisk("#/opp/Acme/synthetic-opportunity/synthetic-opportunity/risk/security-review");
scrollToOverviewRisk("#/opp/Acme/synthetic-opportunity/synthetic-opportunity/risks");
scrollToOverviewRisk("#/opp/Acme/synthetic-opportunity/synthetic-opportunity");
console.log(JSON.stringify(seen));
"""
    scrolled = subprocess.run(["node", "-e", scroll_script], check=True, capture_output=True, text=True)
    assert json.loads(scrolled.stdout) == ["overview-risk-security-review", "overview-risks"]


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

    # The correction preview is structured exactly like the saved-document
    # article: the collapsible-section styling is scoped to `.md-body`, so a
    # bare `.doc-sheet` pane would show every collapsed section's summary line
    # on top of its own body. Its sections are wired for expand/collapse too.
    assert 'id="review-preview-pane" class="md-body review-preview hidden"' in app_js
    assert '<div class="doc-sheet">${doc.sheetHtml}</div>`' in app_js
    assert "_wireReviewCollapsibles(pane)" in app_js
    assert app_js.count("function _wireReviewCollapsibles(root)") == 1

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


def test_hosted_export_controls_follow_the_approved_current_version(repo_root: Path) -> None:
    """Export is offered only for the approved current version.

    The server is authoritative, but a button that looks available while reading a
    historical version invites the reviewer to believe they exported what is on
    screen. Both buttons, both tooltips, and the status line therefore share one
    `canExport` condition, and the request body stays limited to a format and an
    idempotency key.
    """
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    subprocess.run(["node", "--check", str(repo_root / "webapp" / "static" / "app.js")], check=True)

    assert "const canExport = approved && isCurrent;" in app_js
    assert app_js.count("${canExport ?") == 6
    assert '${approved ? "" : " disabled"}' not in app_js
    assert "exportBlockedWhy" in app_js

    export_block = app_js.split("async function _exportHostedOutput(format, button)")[1].split("\n}\n")[0]
    assert 'JSON.stringify({ format, request_id: _newRequestId() })' in export_block
    for forbidden in ("org_id", "version_id", "storage_path", "created_by"):
        assert forbidden not in export_block, forbidden

    # Bounded loading and failure states, and the filename comes from the server.
    assert 'button.textContent = "Exporting…";' in export_block
    assert 'r.headers.get("content-disposition")' in export_block
    assert "URL.revokeObjectURL" in export_block
    assert "button.disabled = false;" in export_block


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

def test_local_opportunity_workspace_renders_truthful_shell_and_output_history(
    repo_root: Path,
) -> None:
    """Exercise the DOM-free workspace renderer with synthetic aggregate data."""
    app_js_path = repo_root / "webapp" / "static" / "app.js"
    script = f"""
const fs = require("fs");
const src = fs.readFileSync({str(app_js_path)!r}, "utf8");
const start = src.indexOf("function workspaceOutputItems");
const end = src.indexOf("// ---- Page: opportunity workspace", start);
if (start < 0 || end < 0) throw new Error("workspace helper block missing");
global.window = {{
  docStatus: Object.assign(
    (meta) => ({{severity: meta.validation_status === "invalid" ? "error" : "ok", label: "Valid", issues: []}}),
    {{escapeHtml: (value) => value}}
  )
}};
function normalizeOutputMeta(value) {{ return value; }}
function esc(value) {{ return String(value || "").replace(/[&<>"]/g, ""); }}
function prettySkill(value) {{ return value; }}
function conciseOutputName(filename) {{ return filename; }}
function downloadMenuHtml() {{ return ""; }}
function emptyBox({{title, body, actions}}) {{ return title + body + (actions || ""); }}
eval(src.slice(start, end));
const output = {{
  skill: "tech-qual",
  filename: "tech-qual-current.md",
  path: "Acme/opportunities/acme-expansion/outputs/tech-qual/tech-qual-current.md",
  ext: "md",
  mtime: 2,
  modified: "2026-09-15 12:00",
  validation_status: "valid",
  validation_supported: true,
  review_supported: true,
  review_status: "approved"
}};
const older = {{...output, filename: "tech-qual-old.md", path: "old.md", mtime: 1, modified: "2026-09-10 12:00"}};
const accountOutput = {{...output, skill: "account-refresher", filename: "account.md", path: "account.md"}};
const workspace = {{
  account: {{name: "Acme", owner_id: "gary", owner_name: "Gary Yang"}},
  opportunity: {{
    name: "Server Opportunity",
    slug: "acme-expansion",
    stage: "Tech Eval",
    stage_num: "S3",
    amount: 75000,
    close_date: "2026-10-31",
    type: "New Business",
    ae: "Alex",
    metadata_complete: true
  }},
  outputs: {{
    opportunity: {{
      total: 2,
      groups: [{{skill: "tech-qual", latest: output, generation_count: 2, history_count: 1, generations: [output, older]}}]
    }},
    account: {{
      total: 1,
      groups: [{{skill: "account-refresher", latest: accountOutput, generation_count: 1, history_count: 0, generations: [accountOutput]}}]
    }}
  }}
}};
const html = renderOpportunityWorkspace(workspace);
console.log(JSON.stringify({{
  title: html.includes("Server Opportunity"),
  stage: html.includes("S3 · Tech Eval"),
  techEval: html.includes("Tech Eval / POV Readiness"),
  meddpicc: html.includes("MEDDPICC"),
  outputs: html.includes("Opportunity Outputs"),
  history: html.includes("View 1 earlier generation"),
  statuses: html.includes("Validation: Valid") && html.includes("Review: Approved"),
  accountScope: html.includes("Account-level outputs") && html.includes("are not applied to this opportunity"),
  generate: html.includes('id="invoke-btn">Generate</button>'),
  noFakeUpdateButton: !html.includes('id="update-overview"')
}}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(result.stdout)
    assert all(rendered.values()), rendered


def test_local_opportunity_page_uses_workspace_endpoint_and_server_identity(
    repo_root: Path,
) -> None:
    """The route label is fallback-only; normal rendering uses aggregate data."""
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    subprocess.run(
        ["node", "--check", str(repo_root / "webapp" / "static" / "app.js")],
        check=True,
    )

    page = app_js.split("async function pageOpportunity(account, slug, routeOppName)")[1]
    page = page.split("// Build a single, scannable Document status bar")[0]

    assert "/opportunities/${encodeURIComponent(slug)}/workspace" in page
    assert "account = workspace.account.name;" in page
    assert "slug = workspace.opportunity.slug;" in page
    assert "const oppName = workspace.opportunity.name;" in page
    assert "outputContext = { account, slug, oppName" in page
    assert "renderWorkspaceOutputGroups(workspace.outputs.opportunity)" in page
    assert 'id="invoke-btn">⚡ Invoke Skill</button>' not in page


def test_opportunity_workspace_renders_created_state_and_create_failure(repo_root: Path) -> None:
    """Typed canonical state renders separately from outputs; failure stays retryable."""
    app_js_path = repo_root / "webapp" / "static" / "app.js"
    script = f"""
const fs = require("fs");
const src = fs.readFileSync({str(app_js_path)!r}, "utf8");
const start = src.indexOf("function workspaceOutputItems");
const end = src.indexOf("// ---- Page: opportunity workspace", start);
global.window = {{docStatus: Object.assign(() => ({{severity: "ok", label: "Valid", issues: []}}), {{escapeHtml: (v) => v}})}};
function normalizeOutputMeta(v) {{ return v; }}
function esc(v) {{ return String(v || "").replace(/[&<>\"]/g, ""); }}
function prettySkill(v) {{ return v; }}
function conciseOutputName(v) {{ return v; }}
function downloadMenuHtml() {{ return ""; }}
function emptyBox({{title, body, actions}}) {{ return title + body + (actions || ""); }}
eval(src.slice(start, end));
const ref = {{source_type: "transcript", source_id: "tr_synthetic", locator: "00:01:00"}};
const claim = (value) => ({{knowledge_state: "known", value, confidence: "high", evidence_refs: [ref]}});
const current = {{
  revision: 2,
  created_at: "2026-09-18T12:00:00Z",
  change_set: {{parent_revision: 1, child_revision: 2, metadata_changed: false, brief: [{{key: "current_status", change_type: "changed", fields: ["value"]}}], business_case: [{{key: "current_state", change_type: "changed", fields: ["knowledge"]}}], meddpicc: [{{key: "metrics", change_type: "changed", fields: ["knowledge"]}}], health_indicators: [], risks: [], recommended_actions: [], missing_information: [], evidence_sources: [{{source_type: "transcript", source_id: "tr_synthetic", change_type: "added"}}]}},
  evidence_manifest: [{{source_type: "opportunity_metadata", source_id: "opportunity-metadata-v1", display_name: "Opportunity metadata", sha256: "a".repeat(64)}}, {{source_type: "transcript", source_id: "tr_synthetic", display_name: "Synthetic transcript", sha256: "b".repeat(64)}}],
  state: {{
    brief: {{customer_objective: claim("Centralize data"), why_airbyte: claim("Reliable movement"), current_status: claim("Planning"), path_to_decision: claim("Validate then approve"), immediate_priority: claim("Confirm connectors")}},
    health_indicators: [{{key: "technical_fit", status: "strong", reason: "Supported path", evidence_refs: [ref]}}],
    risks: [{{title: "Security review", description: "Approval is pending", severity: "high", classification: "critical_blocker", evidence_refs: [ref]}}],
    recommended_actions: [{{action: "Engage security", goal: "Clear review", status: "not_started", evidence_refs: [ref]}}],
    missing_information: [{{description: "Decision authority is unknown", category: "qualification"}}]
  }}
}};
const base = {{account: {{name: "Acme"}}, opportunity: {{name: "Synthetic Opportunity", slug: "synthetic-opportunity"}}, outputs: {{opportunity: {{total: 0, groups: []}}, account: {{total: 0, groups: []}}}}, eligible_evidence: {{count: 1}}}};
const freshness = {{metadata_changed: false, new_source_count: 1, changed_source_count: 0, update_active: false, last_successful_update: {{created_at: current.created_at}}}};
const created = renderOpportunityWorkspace({{...base, canonical_state: {{status: "current", available: true, current, freshness}}}});
const failed = renderOpportunityWorkspace({{...base, canonical_state: {{status: "not_created", available: false, create_job: {{status: "error", error_message: "Safe failure"}}}}}});
console.log(JSON.stringify({{
  brief: created.includes("Centralize data") && created.includes("Confirm connectors"),
  health: created.includes("Health indicators") && created.includes("Strong"),
  risks: created.includes("Security review") && created.includes("Engage security"),
  provenance: created.includes("Evidence and missing information") && created.includes("Synthetic transcript"),
  evidenceButton: created.includes("data-evidence-refs"),
  separate: created.includes("Generate remains a separate artifact workflow"),
  update: created.includes('id="update-overview-btn"') && created.includes("What Changed") && created.includes("Revision 1 → Revision 2") && created.includes("Business Case") && created.includes("MEDDPICC") && created.includes("Stakeholders"),
  stakeholderLegacy: created.includes("0 established") && created.includes("Not established from authorized evidence") && !created.includes("No structured stakeholder map is available yet"),
  retry: failed.includes("Retry Create overview") && failed.includes("Safe failure")
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = json.loads(result.stdout)
    assert all(rendered.values()), rendered


def test_overview_frameworks_are_collapsed_complete_and_escaped(repo_root: Path) -> None:
    app_js_path = repo_root / "webapp" / "static" / "app.js"
    script = f"""
const fs = require("fs");
const src = fs.readFileSync({json.dumps(str(app_js_path))}, "utf8");
const start = src.indexOf("const overviewLabel");
const end = src.indexOf("function renderOverviewBrief", start);
if (start < 0 || end < 0) throw new Error("framework renderer block missing");
const esc = (value) => String(value == null ? "" : value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
eval(src.slice(start, end) + "\\nglobalThis.renderBusinessCase = renderBusinessCase; globalThis.renderMeddpicc = renderMeddpicc; globalThis.renderStakeholders = renderStakeholders;");
const ref = {{source_type: "transcript", source_id: "tr_synthetic", locator: "00:01:00"}};
const knowledge = (state, value, refs = []) => ({{knowledge_state: state, value, confidence: "medium", confirmation: refs.length ? "evidence_backed" : "inferred", evidence_refs: refs}});
const area = (state, value, missing, refs = [], points = []) => ({{knowledge: knowledge(state, value, refs), missing_information: missing, points}});
const keys = ["metrics", "economic_buyer", "decision_criteria", "decision_process", "paper_process", "identify_pain", "champion", "competition"];
const current = {{state: {{
  business_case: {{
    current_state: area("known", "Manual work <script>alert(1)</script>", [], [ref], ["Manual export takes 6 hours weekly <script>x</script>"]),
    future_state: area("partial", "Automated movement", ["Target SLA <img src=x>"]),
    negative_consequences: area("conflicting", "Conflicting cost estimates", ["Validated cost"]),
    positive_business_outcomes: area("unknown", null, ["Quantified outcome"]),
  }},
  meddpicc: {{dimensions: keys.map((key, index) => ({{
    key,
    knowledge: knowledge(index < 3 ? "known" : index < 5 ? "partial" : "unknown", index < 5 ? key + " detail" : null, index === 0 ? [ref] : []),
    missing_information: index < 3 ? [] : ["Missing " + key],
    suggested_discovery: ["Ask about " + key + " </details><script>x</script>"],
  }}))}},
  stakeholders: {{
    stakeholders: [{{key: "casey-champion", name: "Casey <script>x</script>", title_or_role: "Data lead <img src=x>", category: "champion", influence: "high", engagement: "active", stance: "supportive", blocker_status: "not_a_blocker", blocker_reason: null, recommended_next_step: "Confirm process </details><script>x</script>", evidence_refs: [ref], missing_information: ["Authority <svg onload=x>"]}}, {{key: "sam-security", name: "Sam", title_or_role: null, category: "security_approver", influence: "medium", engagement: "limited", stance: "skeptical", blocker_status: "active_blocker", blocker_reason: "Security requirements unknown", recommended_next_step: null, evidence_refs: [ref], missing_information: []}}],
    missing_key_roles: ["economic_buyer", "technical_decision_maker"],
    missing_information: ["Procurement contact <iframe>"],
  }},
}}}};
const business = renderBusinessCase(current);
const meddpicc = renderMeddpicc(current);
const stakeholders = renderStakeholders(current);
const html = business + meddpicc + stakeholders;
console.log(JSON.stringify({{
  topCollapsed: !/<details[^>]+id="overview-(business-case|meddpicc|stakeholders)"[^>]+open/.test(html),
  flatDimensions: !/<details[^>]*data-meddpicc-dimension/.test(meddpicc),
  eightDimensions: (meddpicc.match(/data-meddpicc-dimension=/g) || []).length === 8,
  gapCallout: meddpicc.includes('class="overview-meddpicc-gap-label">GAP<'),
  pointsRendered: business.includes("overview-framework-points") && business.includes("Manual export takes 6 hours weekly"),
  noEmptyPointsList: !business.includes('<ul class="overview-framework-points"></ul>'),
  businessSummary: business.includes("1 established · 1 partial · 1 conflicting · 1 unknown"),
  meddpiccSummary: meddpicc.includes("3 established · 2 partial · 3 unknown"),
  sections: ["Known information", "Missing information", "Evidence", "Suggested discovery"].every((label) => meddpicc.includes(label)),
  evidence: html.includes("data-evidence-refs=") && html.includes("1 evidence reference"),
  stakeholderCount: (stakeholders.match(/data-stakeholder-key=/g) || []).length === 2,
  stakeholderSummary: stakeholders.includes("2 established · Missing Economic Buyer, Technical Decision Maker · 1 blocker"),
  stakeholderFields: ["Influence", "Engagement", "Stance", "Blocker information", "Recommended next engagement step", "Missing information", "Evidence"].every((label) => stakeholders.includes(label)),
  stakeholderDisclosure: stakeholders.startsWith('<details class="workspace-disclosure') && stakeholders.includes("<summary>") && !stakeholders.includes("<details open"),
  noPlaceholder: !html.includes("No structured stakeholder map is available yet"),
  escaped: !html.includes("<script>") && !html.includes("<img") && !html.includes("<svg") && !html.includes("<iframe") && html.includes("&lt;script&gt;") && html.includes("&lt;img src=x&gt;"),
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = json.loads(result.stdout)
    assert all(rendered.values()), rendered

    css = (repo_root / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".overview-stakeholder { min-width: 0" in css
    assert "overflow-wrap: anywhere" in css
    assert ".overview-stakeholder-grid { grid-template-columns: minmax(0, 1fr); }" in css
    assert ".overview-stakeholder-signals { grid-template-columns: minmax(0, 1fr); }" in css


def test_create_overview_frontend_uses_opaque_selection_and_dedicated_routes(repo_root: Path) -> None:
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    create = app_js.split("async function openCreateOverviewModal")[1].split("async function openUpdateOverviewModal")[0]
    assert "/overview/evidence" in create
    assert "/overview/create" in create
    assert "transcript_ids: selected" in create
    assert "input type=\"checkbox\"" in create
    assert "path" not in create
    page = app_js.split("async function pageOpportunity(account, slug, routeOppName)")[1]
    assert "/overview/jobs/${encodeURIComponent(jobId)}" in page
    assert "showOverviewEvidenceRefs" in page


def test_update_overview_frontend_requires_selection_and_loads_history(repo_root: Path) -> None:
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    update = app_js.split("async function openUpdateOverviewModal")[1].split("function showHistoricalOverview")[0]
    assert "/overview/freshness" in update
    assert "/overview/update" in update
    assert "base_version_id: freshness.base_version_id" in update
    assert "transcript_ids: selected" in update
    assert "!freshness.metadata_changed && !boxes.some" in update
    assert "Previously used · inherited without resending" in update
    assert "Generated outputs and local filesystem paths are excluded" in update
    history = app_js.split("async function loadOverviewHistory")[1].split("// ---- Page: opportunity workspace")[0]
    assert "/overview/history" in history
    assert "data-history-revision" in history
    assert "showHistoricalOverview" in history
    assert "<details" in app_js.split("function renderRevisionHistory")[1].split("function renderCreateOverviewState")[0]


def test_tech_eval_tracker_is_collapsed_grouped_and_escapes_user_fields(repo_root: Path) -> None:
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    script = f"""
const fs = require("fs");
const src = fs.readFileSync({json.dumps(str(repo_root / 'webapp' / 'static' / 'app.js'))}, "utf8");
const start = src.indexOf("const techEvalStatuses");
const end = src.indexOf("function renderOpportunityWorkspace", start);
if (start < 0 || end < 0) throw new Error("Tech Eval renderer missing");
const esc = (value) => String(value == null ? "" : value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const overviewLabel = (value) => String(value || "unknown").replaceAll("_", " ").replace(/\\b\\w/g, (c) => c.toUpperCase());
eval(src.slice(start, end) + "\\nglobalThis.renderTechEvalTracker = renderTechEvalTracker;");
const items = [
  {{id:"gate",phase:"plan",type:"gate",label:"Gate <script>alert(1)</script>",status:"blocked",owner:"<img src=x>",note:"</textarea><script>x</script>",last_updated:"2026-09-18T12:00:00Z"}},
  {{id:"required",phase:"prepare",type:"required",label:"Required",status:"done",owner:null,note:null,last_updated:null}},
  {{id:"recommended",phase:"execute",type:"recommended",label:"Recommended",status:"in_progress",owner:null,note:null,last_updated:null}},
  {{id:"optional",phase:"validate",type:"optional",label:"Optional",status:"not_applicable",owner:null,note:null,last_updated:null}},
];
const tracker = {{items, summary:{{current_phase:"plan",overall_state:"blocked",overall_label:"Blocked",completed:1,total_applicable:3,blocking_gates:1,remaining:"Blocked gate: Gate <script>.",phases:[
  {{phase:"plan",completed:0,total_applicable:1,blocking_gates:1}},{{phase:"prepare",completed:1,total_applicable:1,blocking_gates:0}},{{phase:"execute",completed:0,total_applicable:1,blocking_gates:0}},{{phase:"validate",completed:0,total_applicable:0,blocking_gates:0}},{{phase:"close",completed:0,total_applicable:0,blocking_gates:0}}
]}}}};
const html = renderTechEvalTracker(tracker);
console.log(JSON.stringify({{
  collapsed: !/<details[^>]+open/.test(html),
  phases: ["Plan", "Prepare", "Execute", "Validate", "Close"].every((phase) => html.includes(">" + phase + "</h3>")),
  types: ["Gate", "Required", "Recommended", "Optional"].every((type) => html.includes(">" + type + "</span>")),
  controls: html.includes("data-tech-eval-status") && html.includes("data-tech-eval-owner") && html.includes("data-tech-eval-note"),
  escaped: !html.includes("<script>") && !html.includes("<img") && html.includes("&lt;script&gt;") && html.includes("&lt;img src=x&gt;"),
  manual: html.includes("Saved as a manual change, not model evidence."),
  summary: html.includes("1/3 applicable items complete") && html.includes("1 blocking gate")
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    rendered = json.loads(result.stdout)
    assert all(rendered.values()), rendered

    page = app_js.split("async function pageOpportunity(account, slug, routeOppName)")[1]
    assert "renderTechEvalTracker(workspace.tech_eval)" in app_js
    assert "/tech-eval/items/${encodeURIComponent(item.dataset.techEvalItem)}" in app_js
    assert "wireTechEval();" in page
    assert "No structured readiness plan exists yet." not in app_js

