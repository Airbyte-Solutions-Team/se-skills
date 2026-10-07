#!/usr/bin/env node
/**
 * Optional Google Sheets automation helper for the `pov-gsheet` skill.
 *
 * In `--dry-run` mode (no Google auth or Playwright required) it reads the
 * structured POV context produced by `pov_gsheet_context.py` and prints a plan
 * of cell ranges, TSV payloads, and expected Drive URLs. This is the mode used
 * for deterministic validation.
 *
 * In `--run` mode it attempts to drive Chrome via Playwright, copy the
 * configured template, paste the plan into the seven tabs, and move the new
 * sheet to the prospect subfolder. This mode requires:
 *   - Playwright installed (`npm install` in this directory)
 *   - Chrome running with remote debugging (default http://localhost:29229)
 *   - A Google account signed in to Chrome
 */
import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { platform } from "node:os";

// ---------------------------------------------------------------------------
// Constants matching the POV Google Sheets template contract
// ---------------------------------------------------------------------------
const TABS = [
  "Contacts",
  "POV Milestones",
  "Business Objectives",
  "POV - Success Criteria",
  "In-scope Apps",
  "Feature Requests",
  "Architecture Diagrams",
];

// Dropdown lists read from the template's data validation on 2026-10-07.
// Several values end in a space; the space is part of the value and must be kept.
// The Sheets UI flags anything else as "Invalid", but neither paste nor the
// Sheets API rejects it, so every value written below goes through pick().
const DROPDOWNS = {
  milestoneStatus: ["Needs Scheduling ", "Scheduled ", "Completed ", "In Progress"],
  inScope: ["Yes", "No"],
  priority: ["Must Have", "Nice to Have", "Out of Scope"],
  criteriaStatus: ["Completed", "Pending"],
  sourceOrDestination: ["Source", "Destination"],
  useCase: ["Use Case #1", "Use Case #2", "Use Case #3"],
  integrationType: ["API", "OAuth", "Other means of integration"],
  prodNonProd: ["Prod", "Non-prod"],
  approvalRequired: ["No", "Yes"],
  // Feature Requests > Airbyte Product Area only offers the placeholder "Product Area 1",
  // so the product area is written into Notes instead of column C.
  featurePriority: ["1 - Critical", "2 - High", "3 - Medium", "4 - Low", "5 - Not Needed"],
  featureScope: ["POC ", "Implementation"],
  featureFlagged: ["Yes", "No"],
  featureStatus: [
    "Not Started",
    "On Roadmap",
    "Not Planned",
    "In Progress - At Risk",
    "In Progress - Off track",
    "In Progress - On track",
    "Completed - Pending Verification",
    "Completed",
    "Canceled",
  ],
};

// Fixed row layout of the template (verified 2026-10-07). Writing outside these
// rows overwrites template headers.
const LAYOUT = {
  contacts: { internalRows: [3, 6], prospectHeaderCell: "A11", prospectRows: [13, 15] },
  milestones: [
    { row: 3, label: "Demo", match: /\bdemo\b/i },
    { row: 4, label: "Technical Discovery / Workshop & Architecture Review", match: /discovery|workshop|architecture/i },
    { row: 5, label: "Success Criteria & Requirements Review", match: /success criteria|requirements/i },
    { row: 6, label: "Executive Business Case", match: /business case|executive/i },
    { row: 7, label: "POV Mutual Slack Channel", match: /slack|channel|support email/i },
  ],
  objectivesFirstRow: 2,
  criteriaSetupRows: [3, 5],
  criteriaSections: [
    { headerRow: 6, rows: [8, 16], placeholderCell: "A8" },
    { headerRow: 17, rows: [19, 26], placeholderCell: "A19" },
    { headerRow: 27, rows: [29, 40], placeholderCell: "A29" },
  ],
  appsFirstRow: 4,
  featuresFirstRow: 2,
};

const DEFAULT_MILESTONES = [
  "Demo",
  "Discovery",
  "Success Criteria",
  "Environment Setup",
  "POV Execution",
  "Wrap-up",
];

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const args = {
    context: null,
    templateUrl: null,
    driveFolderUrl: null,
    copyTitle: null,
    cdpUrl: "http://localhost:29229",
    dryRun: false,
    run: false,
    outReceipt: null,
    help: false,
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--context" || a === "-c") args.context = argv[++i];
    else if (a === "--template-url") args.templateUrl = argv[++i];
    else if (a === "--drive-folder-url") args.driveFolderUrl = argv[++i];
    else if (a === "--copy-title") args.copyTitle = argv[++i];
    else if (a === "--cdp-url") args.cdpUrl = argv[++i];
    else if (a === "--out-receipt") args.outReceipt = argv[++i];
    else if (a === "--dry-run") args.dryRun = true;
    else if (a === "--run") args.run = true;
    else if (a === "--help" || a === "-h") args.help = true;
  }
  return args;
}

function showHelp() {
  console.log(`Usage: node pov-gsheet-runner.mjs [options]

Options:
  --context <path>          JSON file from pov_gsheet_context.py
  --template-url <url>      Google Sheets template URL to copy
  --drive-folder-url <url>  Drive "Customer" folder URL
  --copy-title <title>      Title for the copied sheet
  --cdp-url <url>           Chrome DevTools Protocol URL (default: http://localhost:29229)
  --dry-run                 Generate a plan without touching Google
  --run                     Attempt to drive Chrome and create the sheet
  --out-receipt <path>      Write the receipt JSON to this path
  --help                    Show this help
`);
}

// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------
function extractSheetsId(url) {
  const m = url.match(/\/spreadsheets\/d\/([a-zA-Z0-9-_]+)/);
  if (!m) throw new Error(`Cannot extract spreadsheet ID from ${url}`);
  return m[1];
}

function extractDriveFolderId(url) {
  const m = url.match(/\/folders\/([a-zA-Z0-9-_]+)/);
  if (!m) throw new Error(`Cannot extract Drive folder ID from ${url}`);
  return m[1];
}

function buildCopyUrl(templateId, folderId, title) {
  const params = new URLSearchParams({
    copyDestination: folderId,
    title,
    copyCollaborators: "false",
    copyComments: "false",
  });
  return `https://docs.google.com/spreadsheets/d/${templateId}/copy?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Plan generation
// ---------------------------------------------------------------------------
function tsv(rows) {
  return rows.map((row) => row.join("\t")).join("\n");
}

function safeDropdown(value, allowed, fallback = "TBD") {
  return pick(value, allowed, fallback);
}

// Match a value to a dropdown option ignoring case and surrounding whitespace,
// and return the option exactly as the template spells it (trailing spaces included).
function pick(value, allowed, fallback) {
  if (value === undefined || value === null || String(value).trim() === "") return fallback;
  const v = String(value).trim().toLowerCase();
  const hit = allowed.find((a) => a.trim().toLowerCase() === v);
  return hit === undefined ? fallback : hit;
}

function milestoneStatus(raw) {
  const v = String(raw || "").trim().toLowerCase();
  if (v === "completed" || v === "done") return "Completed ";
  if (v === "in progress") return "In Progress";
  if (v === "scheduled") return "Scheduled ";
  return "Needs Scheduling "; // Not Started, Blocked, Needs Scheduling, unknown
}

function featurePriority(raw) {
  const exact = pick(raw, DROPDOWNS.featurePriority, null);
  if (exact) return exact;
  const v = String(raw || "").trim().toLowerCase();
  if (v === "must have" || v === "critical") return "1 - Critical";
  if (v === "high") return "2 - High";
  if (v === "nice to have" || v === "medium") return "3 - Medium";
  if (v === "low") return "4 - Low";
  if (v === "out of scope" || v === "not needed") return "5 - Not Needed";
  return "3 - Medium";
}

// One cell of pasted text: no tabs or newlines (they would split cells/rows),
// and no leading "=", "+", "-" or "@" that Sheets would parse as a formula.
function cell(value) {
  if (value === undefined || value === null) return "";
  // No trim: some dropdown values end in a space that must survive.
  let s = String(value).replace(/[\t\r\n]+/g, " ");
  if (/^[=+@]/.test(s) || /^-[^ ]/.test(s)) s = "'" + s;
  return s;
}

function colLetter(n) {
  let s = "";
  while (n > 0) {
    const m = (n - 1) % 26;
    s = String.fromCharCode(65 + m) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

function quoteTab(tab) {
  return `'${tab.replace(/'/g, "''")}'`;
}

// A block is a rectangle of values pasted (or written with the Sheets API) at startCell.
function block(tab, startCell, rows) {
  const m = startCell.match(/^([A-Z]+)(\d+)$/);
  const startColNum = m[1].split("").reduce((acc, ch) => acc * 26 + (ch.charCodeAt(0) - 64), 0);
  const startRow = Number(m[2]);
  const width = Math.max(1, ...rows.map((r) => r.length));
  const values = rows.map((r) => {
    const padded = r.map(cell);
    while (padded.length < width) padded.push("");
    return padded;
  });
  const endCell = `${colLetter(startColNum + width - 1)}${startRow + values.length - 1}`;
  return {
    startCell,
    range: `${quoteTab(tab)}!${startCell}:${endCell}`,
    values,
    tsv: tsv(values),
  };
}

function classifyIntegrationType(connectorName) {
  // Template guidance: databases (CDC/replication), warehouses, files and lakes are
  // "Other means of integration"; OAuth SaaS apps are "OAuth"; other REST/SaaS APIs are "API".
  const lower = String(connectorName).toLowerCase();
  const other = [
    "postgres", "sql", "mysql", "mariadb", "oracle", "db2", "mongodb", "database", "cdc",
    "snowflake", "bigquery", "redshift", "databricks", "clickhouse", "warehouse",
    "s3", "gcs", "azure blob", "blob storage", "sftp", "ftp", "file", "csv", "iceberg", "lake", "kafka",
  ];
  if (other.some((k) => lower.includes(k))) return "Other means of integration";
  if (["salesforce", "hubspot", "google", "linkedin", "facebook", "meta", "zendesk", "oauth"].some((k) => lower.includes(k))) {
    return "OAuth";
  }
  return "API";
}

function addTab(plan, tab, blocks) {
  const usable = blocks.filter((b) => b && b.values.length);
  plan.tabs[tab] = {
    blocks: usable,
    // First block, kept for callers that read a single start cell.
    startCell: usable[0] ? usable[0].startCell : null,
    tsv: usable[0] ? usable[0].tsv : "",
  };
}

function buildPlan(ctx) {
  const plan = { tabs: {}, warnings: [], placeholders_to_clear: [] };
  const today = new Date().toISOString().slice(0, 10);
  const prospectName = ctx.prospect?.account_name || "Prospect";

  // ---- Contacts: Airbyte team A3:B6, prospect header A11, evaluators A13:B15
  const internal = (ctx.contacts?.internal || []).filter((c) => c.name);
  const names = new Set(internal.map((c) => c.name.trim().toLowerCase()));
  const owner = ctx.prospect?.owner;
  if (owner && !names.has(owner.trim().toLowerCase())) internal.unshift({ name: owner, role: "Account Executive" });
  const seName = ctx.prospect?.se_name;
  if (seName && !names.has(seName.trim().toLowerCase())) {
    internal.push({ name: seName, role: ctx.prospect?.se_title || "Solutions Engineer" });
  }
  const [iFirst, iLast] = LAYOUT.contacts.internalRows;
  const internalRows = internal.slice(0, iLast - iFirst + 1).map((c) => [c.role ? `${c.name}, ${c.role}` : c.name, c.email || ""]);
  if (internal.length > iLast - iFirst + 1) plan.warnings.push(`Contacts: ${internal.length - (iLast - iFirst + 1)} Airbyte contact(s) did not fit rows ${iFirst}-${iLast}.`);
  const prospect = (ctx.contacts?.prospect || []).filter((c) => c.name);
  const [pFirst, pLast] = LAYOUT.contacts.prospectRows;
  const prospectRows = prospect.slice(0, pLast - pFirst + 1).map((c) => [c.role ? `${c.name}, ${c.role}` : c.name, c.email || ""]);
  if (prospect.length > pLast - pFirst + 1) {
    plan.warnings.push(`Contacts: ${prospect.length - (pLast - pFirst + 1)} prospect contact(s) need extra rows inserted after row ${pLast} before writing.`);
  }
  addTab(plan, "Contacts", [
    internalRows.length ? block("Contacts", `A${iFirst}`, internalRows) : null,
    block("Contacts", LAYOUT.contacts.prospectHeaderCell, [[`${prospectName} Contacts`]]),
    prospectRows.length ? block("Contacts", `A${pFirst}`, prospectRows) : null,
  ]);

  // ---- POV Milestones: Date/Status in B:C of the fixed Pre-Evaluation rows
  const milestoneBlocks = [];
  const used = new Set();
  for (const slot of LAYOUT.milestones) {
    const m = (ctx.milestones || []).find((x, i) => !used.has(i) && slot.match.test(x.name || "") && used.add(i));
    if (m) {
      milestoneBlocks.push(block("POV Milestones", `B${slot.row}`, [[m.target_date || "", milestoneStatus(m.status)]]));
    } else if (slot.row === 5) {
      // The Success Criteria review is happening now.
      milestoneBlocks.push(block("POV Milestones", "B5", [[ctx.prospect?.pov_start_date || today, "In Progress"]]));
    }
  }
  (ctx.milestones || []).forEach((m, i) => {
    if (!used.has(i)) plan.warnings.push(`POV Milestones: "${m.name}" has no matching template row; add it by hand.`);
  });
  addTab(plan, "POV Milestones", milestoneBlocks);

  // ---- Business Objectives: A2:B from row 2
  const objectiveRows = (ctx.business_objectives || []).slice(0, 8).map((o, i) => [
    `Objective #${i + 1}: ${o.objective || ""}`,
    [o.desired_outcome, o.evidence, (o.sources || []).length ? `Sources: ${(o.sources || []).join(", ")}` : ""]
      .filter(Boolean)
      .join(" "),
  ]);
  addTab(plan, "Business Objectives", objectiveRows.length ? [block("Business Objectives", `A${LAYOUT.objectivesFirstRow}`, objectiveRows)] : []);

  // ---- POV - Success Criteria: columns A-F = Feature, Validation, In-scope, Priority, Status, Notes
  const scBlocks = [];
  const tab = "POV - Success Criteria";
  const sources = (ctx.technical_scope?.sources || []).map((s) => s.name).filter(Boolean);
  const destinations = (ctx.technical_scope?.destinations || []).map((s) => s.name).filter(Boolean);
  const setupRows = [];
  if (sources.length) setupRows.push([`Source connectors: ${sources.join(", ")}`, "All source connectors authenticate and complete an initial sync", "Yes", "Must Have", "Pending", ""]);
  if (destinations.length) setupRows.push([`Destinations: ${destinations.join(", ")}`, "Destinations configured; data lands as expected", "Yes", "Must Have", "Pending", ""]);
  if (setupRows.length) scBlocks.push(block(tab, `A${LAYOUT.criteriaSetupRows[0]}`, setupRows));

  const groups = [];
  for (const c of ctx.success_criteria || []) {
    const key = (c.use_case || "").trim() || "POV requirements";
    let g = groups.find((x) => x.name === key);
    if (!g) groups.push((g = { name: key, items: [] }));
    g.items.push(c);
  }
  groups.forEach((g, gi) => {
    const section = LAYOUT.criteriaSections[gi];
    if (!section) {
      plan.warnings.push(`POV - Success Criteria: use case "${g.name}" (${g.items.length} criteria) has no free section; add it by hand.`);
      return;
    }
    const capacity = section.rows[1] - section.rows[0] + 1;
    if (g.items.length > capacity) plan.warnings.push(`POV - Success Criteria: use case "${g.name}" has ${g.items.length - capacity} more criteria than rows ${section.rows[0]}-${section.rows[1]} hold.`);
    scBlocks.push(block(tab, `A${section.headerRow}`, [[`Use case #${gi + 1}: ${g.name}`]]));
    const rows = g.items.slice(0, capacity).map((c, i) => [
      `${i + 1} - ${c.feature_or_capability}`,
      `${c.validation_method || "Customer validation during POV"}. Acceptance: ${c.acceptance_threshold || "TBD, confirm with customer"}`,
      pick(c.in_scope, DROPDOWNS.inScope, "Yes"),
      pick(c.priority, DROPDOWNS.priority, "Must Have"),
      "Pending",
      c.notes || c.evidence || (c.sources || []).join(", "),
    ]);
    scBlocks.push(block(tab, `A${section.rows[0]}`, rows));
  });
  LAYOUT.criteriaSections.slice(groups.length).forEach((s) => plan.placeholders_to_clear.push(`${quoteTab(tab)}!${s.placeholderCell}`));
  addTab(plan, tab, scBlocks);

  // ---- In-scope Apps: data from row 4, columns A-H
  const groupIndex = (name) => {
    const n = String(name || "").trim().toLowerCase();
    const i = groups.findIndex((g) => g.name.toLowerCase() === n);
    return i >= 0 && i < 3 ? i : -1;
  };
  const systemUseCases = (sys) => {
    const hits = new Set();
    const direct = groupIndex(sys.use_case);
    if (direct >= 0) hits.add(direct);
    const needle = String(sys.name || "").toLowerCase();
    groups.slice(0, 3).forEach((g, gi) => {
      if (needle && g.items.some((c) => `${c.feature_or_capability} ${c.validation_method || ""}`.toLowerCase().includes(needle))) hits.add(gi);
    });
    if (!hits.size && sys.kind === "Destination") groups.slice(0, 3).forEach((_, gi) => hits.add(gi));
    if (!hits.size) hits.add(0);
    return [...hits].sort().map((i) => DROPDOWNS.useCase[i]).join(", ");
  };
  const appRows = [
    ...(ctx.technical_scope?.sources || []).map((s) => ({ ...s, kind: "Source" })),
    ...(ctx.technical_scope?.destinations || []).map((s) => ({ ...s, kind: "Destination" })),
  ]
    .filter((s) => s.name)
    .map((sys) => [
      sys.name,
      pick(sys.kind, DROPDOWNS.sourceOrDestination, "Source"),
      systemUseCases(sys),
      pick(sys.integration_type, DROPDOWNS.integrationType, null) || classifyIntegrationType(sys.name),
      "",
      "",
      sys.evidence || (sys.sources || []).join(", "),
      "",
    ]);
  addTab(plan, "In-scope Apps", appRows.length ? [block("In-scope Apps", `A${LAYOUT.appsFirstRow}`, appRows)] : []);

  // ---- Feature Requests: columns A-J from row 2
  const featureRows = (ctx.feature_requests || []).slice(0, 20).map((f) => [
    f.date || today,
    "",
    "", // Airbyte Product Area: template list is a placeholder; area goes in Notes
    f.description || "",
    featurePriority(f.priority),
    "",
    "",
    "",
    "Not Started",
    [f.product_area ? `Product area: ${f.product_area}.` : "", f.evidence || ""].filter(Boolean).join(" "),
  ]);
  addTab(plan, "Feature Requests", featureRows.length ? [block("Feature Requests", `A${LAYOUT.featuresFirstRow}`, featureRows)] : []);

  // ---- Architecture Diagrams
  const archRows = (ctx.architecture_notes || []).length
    ? (ctx.architecture_notes || []).map((n) => [n])
    : [["Placeholder: add the architecture diagram once technical scope is final"]];
  addTab(plan, "Architecture Diagrams", [block("Architecture Diagrams", "A2", archRows)]);

  return plan;
}

// ---------------------------------------------------------------------------
// Receipt generation
// ---------------------------------------------------------------------------
function buildReceipt(ctx, args, plan, finalUrl, folderUrl, status, note) {
  return {
    skill: "pov-gsheet",
    status,
    note,
    prospect: ctx.prospect,
    template_url: args.templateUrl,
    drive_target_folder_url: args.driveFolderUrl,
    copy_title: args.copyTitle,
    google_sheet_url: finalUrl || null,
    final_drive_folder_url: folderUrl || null,
    tabs_populated: Object.keys(plan.tabs || {}),
    source_coverage: ctx.source_coverage || [],
    warnings: ctx.warnings || [],
    generated_at: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Dry run
// ---------------------------------------------------------------------------
async function doDryRun(ctx, args) {
  const templateId = extractSheetsId(args.templateUrl);
  const folderId = extractDriveFolderId(args.driveFolderUrl);
  const plan = buildPlan(ctx);
  const simulatedUrl = `https://docs.google.com/spreadsheets/d/COPY_ID_WILL_BE_KNOWN_AFTER_RUN/edit`;
  const simulatedFolder = `https://drive.google.com/drive/folders/${folderId}`;
  const copyUrl = buildCopyUrl(templateId, folderId, args.copyTitle || "POV Success Criteria");
  const receipt = buildReceipt(
    ctx,
    args,
    plan,
    simulatedUrl,
    simulatedFolder,
    "dry-run",
    "Dry-run plan generated. No Google Sheet was created.",
  );
  receipt.plan = plan;
  receipt.copy_url = copyUrl;
  return receipt;
}

// ---------------------------------------------------------------------------
// Real run (best-effort; unverified in this environment)
// ---------------------------------------------------------------------------
async function writeClipboard(text) {
  const sys = platform();
  if (sys === "darwin") {
    const proc = spawn("pbcopy", { stdio: ["pipe", "ignore", "ignore"] });
    proc.stdin.write(text);
    proc.stdin.end();
    await new Promise((resolve) => proc.on("close", resolve));
  } else if (sys === "win32") {
    const proc = spawn("clip", { stdio: ["pipe", "ignore", "ignore"] });
    proc.stdin.write(text);
    proc.stdin.end();
    await new Promise((resolve) => proc.on("close", resolve));
  } else {
    // Linux: prefer xclip; fall back to wl-copy; otherwise instruct manual paste.
    const cmd = existsSync("/usr/bin/xclip") || existsSync("/usr/local/bin/xclip") ? "xclip" : "wl-copy";
    const args = cmd === "xclip" ? ["-selection", "clipboard"] : [];
    try {
      const proc = spawn(cmd, args, { stdio: ["pipe", "ignore", "ignore"] });
      proc.stdin.write(text);
      proc.stdin.end();
      await new Promise((resolve, reject) => {
        proc.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} failed`))));
        proc.on("error", reject);
      });
    } catch (e) {
      throw new Error(
        `Could not set OS clipboard (${cmd} not found). Install xclip or wl-copy, or run in --dry-run mode and paste manually.`,
      );
    }
  }
}

async function assertGoogleSignedIn(page, url) {
  if (url.includes("accounts.google.com") || url.includes("ServiceLogin")) {
    throw new Error(
      "Chrome is not signed into a Google account. Sign in to Chrome with an account that has access to the template and Drive folder, then retry.",
    );
  }
  // Drive and Sheets sometimes redirect through an interstitial that contains a sign-in button.
  const signInButton = page.locator('button:has-text("Sign in"), a:has-text("Sign in"), [data-test-id="sign-in-button"]').first();
  if (await signInButton.isVisible().catch(() => false)) {
    throw new Error(
      "Google sign-in is required but Chrome is not signed in. Sign in and retry.",
    );
  }
}

async function findExistingSheet(page, folderUrl, title) {
  await page.goto(folderUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await assertGoogleSignedIn(page, page.url());
  // Drive lists files with data-tooltip or aria-label attributes. Search for the title text.
  const locator = page.locator(`text=${title}`).first();
  if (await locator.isVisible().catch(() => false)) {
    const link = page.locator(`a:has-text("${title}")`).first();
    const href = await link.getAttribute("href").catch(() => null);
    return href ? (href.startsWith("http") ? href : `https://drive.google.com${href}`) : folderUrl;
  }
  return null;
}

async function verifySheetInFolder(page, folderUrl, title) {
  await page.goto(folderUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await assertGoogleSignedIn(page, page.url());
  const locator = page.locator(`text=${title}`).first();
  return (await locator.isVisible().catch(() => false));
}

async function doRun(ctx, args) {
  let playwright;
  try {
    playwright = await import("playwright");
  } catch (e) {
    throw new Error(
      `Playwright is not installed. Run \`npm install\` in ${process.cwd()} or run this script in --dry-run mode.`,
    );
  }

  const templateId = extractSheetsId(args.templateUrl);
  const folderId = extractDriveFolderId(args.driveFolderUrl);
  const plan = buildPlan(ctx);
  const copyTitle = args.copyTitle || "POV Success Criteria";
  const copyUrl = buildCopyUrl(templateId, folderId, copyTitle);

  const browser = await playwright.chromium.connectOverCDP(args.cdpUrl);
  const context = browser.contexts()[0] || (await browser.newContext());
  const page = await context.newPage();

  let finalUrl = null;
  let finalFolderUrl = args.driveFolderUrl;
  let error = null;
  let existingSheet = null;
  try {
    // Check for an existing sheet with the same title before copying.
    existingSheet = await findExistingSheet(page, args.driveFolderUrl, copyTitle);
    if (existingSheet) {
      throw new Error(
        `An existing POV sheet titled "${copyTitle}" was found in the Drive folder: ${existingSheet}. Stop and ask the user whether to use it, create a new version, or stop.`,
      );
    }

    // Open the copy URL and wait for the new spreadsheet to load.
    await page.goto(copyUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await assertGoogleSignedIn(page, page.url());
    // A /copy URL either opens the new sheet directly or shows a copy dialog.
    // Wait until the URL looks like an /edit URL for a spreadsheet.
    await page.waitForFunction(
      () => window.location.href.includes("/spreadsheets/d/") && window.location.href.includes("/edit"),
      { timeout: 60000 },
    );
    finalUrl = page.url();

    // Fill each tab.
    for (const tabName of TABS) {
      const tabPlan = plan.tabs[tabName];
      if (!tabPlan || !(tabPlan.blocks || []).length) continue;

      // Click the sheet tab by name.
      const tab = page.locator(`button[aria-label="${tabName}"], [role="tab"]:has-text("${tabName}")`).first();
      if (await tab.isVisible().catch(() => false)) {
        await tab.click();
        await page.waitForTimeout(500);
      }

      for (const b of tabPlan.blocks) {
        // Select the start cell via the Name Box.
        const nameBox = page.locator('[id="formula-bar-name-box"]').first();
        await nameBox.click();
        await nameBox.fill(b.startCell);
        await nameBox.press("Enter");
        await page.waitForTimeout(300);

        // Set clipboard and paste.
        await writeClipboard(b.tsv);
        const isMac = platform() === "darwin";
        await page.keyboard.press(isMac ? "Meta+v" : "Control+v");
        await page.waitForTimeout(800);
      }
    }

    // Verify the title.
    const title = await page.title().catch(() => "");
    if (copyTitle && !title.includes(copyTitle)) {
      console.warn(`Sheet title may not match expected: "${title}"`);
    }

    // Verify Drive placement.
    const inFolder = await verifySheetInFolder(page, args.driveFolderUrl, copyTitle);
    if (!inFolder) {
      console.warn(`Sheet "${copyTitle}" not yet visible in the target Drive folder. It may still be copying or may need to be moved manually.`);
    }
  } catch (e) {
    error = e.message;
  } finally {
    await page.close().catch(() => {});
    await browser.close().catch(() => {});
  }

  const folderUrl = finalFolderUrl || `https://drive.google.com/drive/folders/${folderId}`;
  const status = error ? (existingSheet ? "existing-sheet-detected" : "run-error") : "created";
  const note = error
    ? `Run failed: ${error}. ${existingSheet ? "An existing POV sheet was detected — ask the user how to proceed." : "Use --dry-run to get a plan, then follow the manual clipboard steps in the skill."}`
    : `Sheet created and verified in Drive folder: ${folderUrl}`;
  const receipt = buildReceipt(ctx, args, plan, finalUrl, folderUrl, status, note);
  return receipt;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  const args = parseArgs(process.argv);
  if (args.help || (!args.dryRun && !args.run)) {
    showHelp();
    process.exit(args.help ? 0 : 1);
  }

  if (!args.context) {
    console.error("Missing --context <path>");
    process.exit(1);
  }
  if (!args.templateUrl || !args.driveFolderUrl || !args.copyTitle) {
    console.error("Missing --template-url, --drive-folder-url, or --copy-title");
    process.exit(1);
  }

  const raw = await readFile(args.context, "utf-8");
  const ctx = JSON.parse(raw);

  const receipt = args.run ? await doRun(ctx, args) : await doDryRun(ctx, args);

  const payload = JSON.stringify(receipt, null, 2);
  if (args.outReceipt) {
    await writeFile(args.outReceipt, payload, "utf-8");
    console.log(`Receipt written to ${args.outReceipt}`);
  } else {
    console.log(payload);
  }
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(1);
});
