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

const DROPDOWNS = {
  milestoneStatus: ["Not Started", "Needs Scheduling", "In Progress", "Completed", "Blocked"],
  inScope: ["Yes", "No"],
  priority: ["Must Have", "Nice to Have", "Out of Scope"],
  sourceOrDestination: ["Source", "Destination"],
  useCase: ["Use Case #1", "Use Case #2", "Use Case #3"],
  integrationType: ["API", "OAuth", "Other means of integration"],
  productArea: ["Connectors", "Platform", "Cloud", "Enterprise"],
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
  if (!value) return fallback;
  const v = String(value).trim();
  return allowed.includes(v) ? v : fallback;
}

function classifyIntegrationType(connectorName) {
  const lower = String(connectorName).toLowerCase();
  if (lower.includes("postgres") || lower.includes("sql") || lower.includes("mysql") || lower.includes("mongodb") || lower.includes("database") || lower.includes("cdc")) {
    return "Other means of integration";
  }
  if (lower.includes("salesforce") || lower.includes("hubspot") || lower.includes("oauth")) {
    return "OAuth";
  }
  if (lower.includes("s3") || lower.includes("gcs") || lower.includes("azure") || lower.includes("api")) {
    return "API";
  }
  return "API";
}

function buildPlan(ctx) {
  const plan = { tabs: {} };

  // Contacts
  const contactsRows = [];
  const internal = (ctx.contacts?.internal || []).filter((c) => c.name);
  const prospect = (ctx.contacts?.prospect || []).filter((c) => c.name);
  // Airbyte team starting at A3
  contactsRows.push(["Airbyte team"]); // header-ish row
  for (const c of internal) {
    contactsRows.push([c.name, c.role || "", c.email || "", c.side || "Airbyte"]);
  }
  // blank separator
  contactsRows.push([]);
  contactsRows.push(["Prospect contacts"]);
  for (const c of prospect) {
    contactsRows.push([c.name, c.role || "", c.email || "", c.side || "Customer"]);
  }
  plan.tabs.Contacts = {
    startCell: "A3",
    tsv: tsv(contactsRows),
  };

  // POV Milestones
  const milestoneRows = [];
  const provided = new Map((ctx.milestones || []).map((m) => [m.name, m]));
  for (const name of DEFAULT_MILESTONES) {
    const m = provided.get(name) || {};
    const status = safeDropdown(m.status || "Not Started", DROPDOWNS.milestoneStatus, "Not Started");
    const date = m.target_date || "";
    const notes = m.evidence || "";
    milestoneRows.push([name, date, status, notes]);
  }
  plan.tabs["POV Milestones"] = { startCell: "A2", tsv: tsv(milestoneRows) };

  // Business Objectives
  const objectiveRows = (ctx.business_objectives || []).slice(0, 8).map((o) => [
    o.objective || "",
    o.desired_outcome || "",
    (o.sources || []).join(", "),
  ]);
  plan.tabs["Business Objectives"] = { startCell: "A2", tsv: tsv(objectiveRows) };

  // POV - Success Criteria
  const criteriaRows = (ctx.success_criteria || []).slice(0, 20).map((c) => [
    c.use_case || "",
    c.feature_or_capability || "",
    c.validation_method || "Customer validation during POV",
    c.acceptance_threshold || "TBD — confirm with customer",
    safeDropdown(c.in_scope, DROPDOWNS.inScope, "Yes"),
    safeDropdown(c.priority, DROPDOWNS.priority, "Must Have"),
    c.notes || c.evidence || "",
  ]);
  plan.tabs["POV - Success Criteria"] = { startCell: "A2", tsv: tsv(criteriaRows) };

  // In-scope Apps
  const appRows = [];
  let useCaseIndex = 0;
  const allSystems = [
    ...(ctx.technical_scope?.sources || []).map((s) => ({ ...s, kind: "Source" })),
    ...(ctx.technical_scope?.destinations || []).map((s) => ({ ...s, kind: "Destination" })),
  ];
  for (const sys of allSystems) {
    const useCase = DROPDOWNS.useCase[Math.min(useCaseIndex, 2)] || "Use Case #1";
    useCaseIndex++;
    appRows.push([
      sys.name || "",
      safeDropdown(sys.kind || sys.side, DROPDOWNS.sourceOrDestination, "Source"),
      useCase,
      classifyIntegrationType(sys.name || ""),
      (sys.sources || []).join(", "),
    ]);
  }
  // If no systems but use cases exist, still create rows
  for (const uc of ctx.technical_scope?.use_cases || []) {
    appRows.push([uc.name || "", "Source", "Use Case #1", "API", (uc.sources || []).join(", ")]);
  }
  plan.tabs["In-scope Apps"] = { startCell: "A2", tsv: tsv(appRows) };

  // Feature Requests
  const featureRows = (ctx.feature_requests || []).slice(0, 10).map((f) => [
    f.date || "",
    safeDropdown(f.product_area, DROPDOWNS.productArea, "Connectors"),
    f.description || "",
    safeDropdown(f.priority, DROPDOWNS.priority, "Must Have"),
  ]);
  plan.tabs["Feature Requests"] = { startCell: "A2", tsv: tsv(featureRows) };

  // Architecture Diagrams
  const archRows = (ctx.architecture_notes || []).length
    ? (ctx.architecture_notes || []).map((n) => [n])
    : [["Placeholder — add architecture diagram once technical scope is finalized"]];
  plan.tabs["Architecture Diagrams"] = { startCell: "A2", tsv: tsv(archRows) };

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
  const copyUrl = buildCopyUrl(templateId, folderId, args.copyTitle || "POV Success Criteria");

  const browser = await playwright.chromium.connectOverCDP(args.cdpUrl);
  const context = browser.contexts()[0] || (await browser.newContext());
  const page = await context.newPage();

  let finalUrl = null;
  let error = null;
  try {
    // Open the copy URL and wait for the new spreadsheet to load.
    await page.goto(copyUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
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
      if (!tabPlan || !tabPlan.tsv) continue;

      // Click the sheet tab by name.
      const tab = page.locator(`button[aria-label="${tabName}"], [role="tab"]:has-text("${tabName}")`).first();
      if (await tab.isVisible().catch(() => false)) {
        await tab.click();
        await page.waitForTimeout(500);
      }

      // Select the start cell via the Name Box.
      const nameBox = page.locator('[id="formula-bar-name-box"]').first();
      await nameBox.click();
      await nameBox.fill(tabPlan.startCell);
      await nameBox.press("Enter");
      await page.waitForTimeout(300);

      // Set clipboard and paste.
      await writeClipboard(tabPlan.tsv);
      const isMac = platform() === "darwin";
      await page.keyboard.press(isMac ? "Meta+v" : "Control+v");
      await page.waitForTimeout(800);
    }

    // Verify the title.
    const title = await page.title().catch(() => "");
    if (args.copyTitle && !title.includes(args.copyTitle)) {
      console.warn(`Sheet title may not match expected: "${title}"`);
    }
  } catch (e) {
    error = e.message;
  } finally {
    await page.close().catch(() => {});
    await browser.close().catch(() => {});
  }

  const folderUrl = `https://drive.google.com/drive/folders/${folderId}`;
  const status = error ? "run-error" : "created";
  const note = error
    ? `Run failed: ${error}. Use --dry-run to get a plan, then follow the manual clipboard steps in the skill.`
    : "Sheet created. Verify Drive placement before reporting success.";
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
