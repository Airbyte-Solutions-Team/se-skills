---
name: pov-gsheet
description: >
  Create and pre-fill Airbyte POV (Proof of Value) Success Criteria documents in Google Sheets
  for prospects. Use this skill whenever the user asks to create a POV document, success criteria sheet,
  POV tracker, or anything related to preparing a POV for a prospect. Also triggers when asked to
  "fill out the POV template", "set up POV tracking for [prospect]", "create success criteria for
  [account]", "prep the POV sheet", or any variation involving POV + prospect/account name.
  This skill uses a Google Sheets template (not XLSX) with dropdown fields and pulls data from the
  existing SE workspace — prior skill outputs, workspace transcripts, and (optionally) Salesforce.
---

# POV Google Sheets Creator

Creates pre-filled Airbyte POV Success Criteria Google Sheets for prospects. It gathers context
deterministically from the workspace, builds a structured POV context, then uses Chrome/
computer-use (clipboard + Name Box paste) to copy the configured template, fill the seven tabs,
and move the new sheet into the correct prospect folder in Google Drive.

Before generating anything, read `~/.claude/skills/_se-playbook.md` → **Output Document Format** and
**Source Coverage Transparency**.

## Preflight checks (fail fast)

Stop immediately and report a clear, actionable error if any required dependency or configuration is
missing. Do not open Chrome or copy a template until these checks pass.

1. **Locate the SE config file.** The webapp runs `claude -p` from the workspace root, so start by
   reading `.se-config.yaml` in the current directory. If it is missing, fall back to
   `$SE_WORKSPACE/.se-config.yaml`, then `~/.se-skills/.se-config.yaml`, then `~/airbyte-work/.se-config.yaml`.
2. **Read the `pov_gsheet` block.** It must contain:
   - `template_url` — the Google Sheets template to copy
   - `drive_target_folder_url` — the Drive "Customer" folder where prospect subfolders are created
   - `se_name` — the SE name shown on the Contacts sheet
   - `se_title` — the SE title shown on the Contacts sheet

   If the block or any required key is missing, stop and say:
   > `pov-gsheet` is missing required configuration. Copy `config/se-config.example.yaml` to your workspace root as `.se-config.yaml`, uncomment the `pov_gsheet:` block, and set `template_url`, `drive_target_folder_url`, `se_name`, and `se_title`.
3. **Confirm the account identifier is available.** The webapp passes the selected account and
   opportunity. If invoked outside the webapp, ask for them or infer from the prompt context. The
   account name must match the workspace customer folder name.
4. **Confirm context-gathering is possible.** Run the deterministic context loader:
   ```bash
   REPO_DIR=$(cd "$(dirname "$(readlink -f ~/.claude/skills/pov-gsheet/SKILL.md)")/.." && pwd)
   CTX_FILE="${OUT_DIR:-/tmp}/pov-gsheet-context.json"
   python "$REPO_DIR/webapp/pov_gsheet_context.py" \
     --account "$ACCOUNT" \
     --opportunity "$OPPORTUNITY" \
     --workspace "$(pwd)" \
     --out "$CTX_FILE" --pretty
   ```
   If the loader cannot be found, stop and say:
   > `pov-gsheet` cannot locate `webapp/pov_gsheet_context.py`. Run `install.sh` to symlink the skills repository into `~/.claude/skills/`.
5. **Confirm the run is not `blocked`.** Read `CTX_FILE` and look at `status` (`complete`, `partial`, or
   `blocked`) and `warnings`. If `blocked`, do not create a sheet. Write the local receipt and explain
   what is missing and which upstream skill to run.
6. **Confirm Chrome / computer-use access is available.** The Google Sheets portion of the skill needs
   Chrome running with remote debugging on `localhost:29229` (the webapp default) and either the
   `computer-use` MCP with `clipboardWrite: true` or the optional Node/Playwright runner
   `webapp/scripts/pov-gsheet-runner.mjs` with Playwright installed. If the first browser action fails
   with a missing-tool or permission error, stop and say:
   > `pov-gsheet` needs a signed-in Google account in Chrome, the computer-use MCP with `clipboardWrite`, or a working Playwright + Chrome CDP setup. See `config/se-config.example.yaml` for setup notes.

## Configuration used by this skill

From `.se-config.yaml` (`pov_gsheet` block), assign these values for the rest of the run:

- `template_url` → `TEMPLATE_URL`
- `drive_target_folder_url` → `DRIVE_TARGET_FOLDER_URL`
- `se_name` → `SE_NAME`
- `se_title` → `SE_TITLE`

Use these variables everywhere the original steps referenced the shared template, shared folder, or
SE contact details. Do not use the placeholder URLs or example names below.

## Template

**Source template:** `TEMPLATE_URL`

This template is expected to contain exactly these 7 sheets:

1. **Contacts** — Airbyte team + prospect contacts
2. **POV Milestones** — Timeline tracking (Demo, Discovery, Success Criteria, Environment Setup, POV Execution, Wrap-up)
3. **Business Objectives** — Strategic goals the prospect wants to achieve
4. **POV - Success Criteria** — The core sheet: connectors, use cases, validation criteria, priorities
5. **In-scope Apps** — Source/destination systems with sync methods
6. **Feature Requests** — Product asks surfaced during discovery
7. **Architecture Diagrams** — Placeholder for diagrams

If the copied template is missing any of these tabs or the tabs are renamed, stop before pasting and
report which tabs are missing or unexpected.

## Workflow

### Step 0: Build the structured POV context

Run the context loader once. It is deterministic and produces a single JSON file that the rest of the
run consumes.

```bash
REPO_DIR=$(cd "$(dirname "$(readlink -f ~/.claude/skills/pov-gsheet/SKILL.md)")/.." && pwd)
CTX_FILE="${OUT_DIR:-/tmp}/pov-gsheet-context.json"
python "$REPO_DIR/webapp/pov_gsheet_context.py" \
  --account "$ACCOUNT" \
  --opportunity "$OPPORTUNITY" \
  --workspace "$(pwd)" \
  --out "$CTX_FILE" --pretty
```

Inspect the context JSON. It has these top-level keys:

```yaml
prospect:                # account, opportunity, stage, owner, se_name, se_title, dates
contacts:                # internal (Airbyte) and prospect contact lists
business_objectives:     # list of customer-derived objectives
technical_scope:         # sources, destinations, use_cases, requirements, dependencies
success_criteria:        # list of validation criteria
milestones:              # list of POV milestones with statuses
feature_requests:        # list of product asks
architecture_notes:      # list of notes / placeholders
unknowns:                # list of unresolved items
source_coverage:         # which sources were checked, available, material
status:                  # complete | partial | blocked
warnings:                # human-readable gaps
```

If `status == "blocked"`, do not proceed to Google Sheets. Write the local receipt (see Step 4) and
explain which upstream skill to run (e.g., `biz-qual`, `tech-qual`, `poc-plan`, `connector-feasibility`).

### Step 1: Copy the Template

Open `TEMPLATE_URL` in Chrome and make a copy. The fastest reliable path is the `/copy` URL with the
prospect subfolder as the destination:

```
https://docs.google.com/spreadsheets/d/<TEMPLATE_ID>/copy?copyDestination=<FOLDER_ID>&title=Airbyte%20%7C%7C%20<Prospect>%20-%20POV%20Success%20Criteria
```

If that creates the copy directly in the target folder, confirm the title. If it creates a "Copy of …"
file in My Drive instead, rename it to `Airbyte || [Prospect Name] - POV Success Criteria` and move it
(see Step 3).

If you cannot use the `/copy` URL, open `TEMPLATE_URL` and use **File > Make a copy**.

### Step 2: Fill the Sheets Using the Clipboard Paste Method

Google Sheets editing via browser automation requires a specific technique that works reliably. The
method below avoids validation popup errors and cell misalignment issues.

#### The Reliable Editing Method

**Prerequisites:**

- Request computer-use access with `clipboardWrite: true`.
- Have the Chrome MCP tools available, **or** use the optional Node/Playwright runner:
  ```bash
  node "$REPO_DIR/webapp/scripts/pov-gsheet-runner.mjs" \
    --context "$CTX_FILE" \
    --template-url "$TEMPLATE_URL" \
    --drive-folder-url "$DRIVE_TARGET_FOLDER_URL" \
    --copy-title "Airbyte || $ACCOUNT - POV Success Criteria" \
    --run
  ```
  If Playwright is not installed or the run fails, fall back to the manual clipboard steps below.

**For each cell or range you need to fill:**

1. **Navigate to the target cell** using the Name Box:
   - Use `find` tool to locate the Name Box element (it's a textbox showing the current cell reference, e.g., "A1")
   - Use `form_input` on the Name Box ref to set the cell address (e.g., "A3")
   - Press Enter to navigate to that cell

2. **Write data to clipboard** using `mcp__computer-use__write_clipboard`:
   - For single cells: just the value text
   - For multi-cell ranges: use TSV format (tabs between columns, newlines between rows)
   - Example TSV for a 3-column, 2-row paste:
     ```
     Value1	Value2	Value3
     Value4	Value5	Value6
     ```

3. **Paste with Cmd+V** (or Ctrl+V on Windows/Linux):
   - The data flows into cells starting from the active cell
   - Tabs map to column separators
   - Newlines map to row separators

**Important notes on dropdowns:**

- Google Sheets dropdown cells accept pasted text that matches valid options
- You MUST use the EXACT dropdown values below — any other text will show as "Invalid"
- Known valid dropdown values in this template:
  - **Status** (POV Milestones): "Not Started", "Needs Scheduling", "In Progress", "Completed", "Blocked"
  - **In-scope for POV** (Success Criteria): "Yes", "No"
  - **Priority Level** (Success Criteria, Column D): "Must Have", "Nice to Have", "Out of Scope"
  - **Source or Destination** (In-scope Apps, Column B): "Source", "Destination"
  - **App / System Role - Use Cases** (In-scope Apps, Column C): "Use Case #1", "Use Case #2", "Use Case #3"
  - **Integration Type** (In-scope Apps, Column D): "API", "OAuth", "Other means of integration"
  - **Airbyte Product Area** (Feature Requests): "Connectors", "Platform", "Cloud", "Enterprise"

**CRITICAL — Dropdown value mapping guidance for In-scope Apps:**

- When filling Column C (App / System Role), map each app to its primary use case number:
  - Use Case #1 = Primary/core data pipeline use case (e.g., database replication, warehouse loading)
  - Use Case #2 = Secondary use case (e.g., CRM/SaaS integrations)
  - Use Case #3 = Tertiary/exploratory use case (e.g., custom APIs, homegrown systems)
- When filling Column D (Integration Type), map the connector type:
  - "API" = REST API, SaaS connectors (e.g., Salesforce, HubSpot), custom API sources
  - "OAuth" = OAuth-based authentication connectors
  - "Other means of integration" = Database connectors (CDC, replication), warehouse loads, file transfers, Iceberg, or any non-API method

#### What NOT to do

- Don't try typing directly into cells with Tab to advance — it's unreliable
- Don't use the Google Sheets API (requires OAuth tokens not available in browser context)
- Don't use `navigator.clipboard.writeText()` from JavaScript — it fails with "Document not focused"
- Don't manually click each cell and type — too slow and error-prone

### Step 3: Sheet-by-Sheet Fill Guide

Use the structured context (`$CTX_FILE`) as the single source of truth. Do not invent data.

#### Contacts Sheet

- **Row 3-6 (Airbyte team):** AE name + title, `SE_NAME` (`SE_TITLE`), and any other Airbyte contacts
  from `contacts.internal`.
- **Row 11+ (Prospect contacts):** Names, titles, emails from `contacts.prospect`. Only include contacts
  with a supported name; do not invent emails or roles.

#### POV Milestones Sheet

Use `milestones` from the context. For the default milestone list (Demo, Discovery, Success Criteria,
Environment Setup, POV Execution, Wrap-up):

- Fill dates and statuses for milestones that have occurred
- Set the "Success Criteria" milestone to the current date with "In Progress" status
- Set all genuinely future milestones to "Not Started"
- Do not invent exact dates

#### Business Objectives Sheet

Use `business_objectives` from the context (2–4 concise customer-specific objectives). Each objective
should map to one row. Include the supporting evidence/source where the sheet has a place for it.

#### POV - Success Criteria Sheet

This is the most important sheet. Use `success_criteria` from the context.

Group related criteria under use-case headers when `use_case` is present. For each row include:

- Feature or capability (from `feature_or_capability`)
- Validation method (from `validation_method`)
- Acceptance threshold (from `acceptance_threshold`; if null, label `TBD — confirm with customer`)
- In-scope (from `in_scope`; default "Yes")
- Priority (from `priority`; default "Must Have")
- Notes/dependencies (from `notes` or `evidence`)

If a criterion is not testable as written (e.g., "Validate CDC works well"), rephrase it to a measurable
statement such as "Replicate inserts, updates, and deletes from SQL Server to Snowflake with no
unexplained record loss during the agreed test window."

#### In-scope Apps Sheet

Use `technical_scope.sources`, `technical_scope.destinations`, and `technical_scope.use_cases`.

Columns:

- A = In-scope App / System name
- B = Source or Destination (dropdown)
- C = App / System Role (dropdown: Use Case #1 / #2 / #3)
- D = Integration Type (dropdown)
- E = Notes

Use exact dropdown values. Do not use free-text like "CDC (Debezium)", "Database Replication",
"API (REST)", or "Warehouse Load".

#### Feature Requests Sheet

Use `feature_requests` from the context. Include only supported product gaps or asks.
Columns:

- A = Date
- B = Airbyte Product Area (dropdown)
- C = Task / Description
- D = Priority

#### Architecture Diagrams Sheet

Preserve placeholders or add concise notes describing what diagram is needed. Do not fabricate an
architecture diagram from incomplete context.

### Step 4: Move to Prospect Folder (REQUIRED — every PoV sheet)

Every PoV sheet MUST end up inside a prospect-specific subfolder under the canonical Drive target.

```
Configured customer target folder
└── [Prospect Name]
    └── Airbyte || [Prospect Name] - POV Success Criteria
```

**Canonical target folder URL:** `DRIVE_TARGET_FOLDER_URL`

#### 4.1 Open the target folder in a second Chrome tab

Keep this tab alongside the sheet tab.

#### 4.2 Check for an existing prospect subfolder

Look for a subfolder named exactly after the prospect. If it exists, skip to **4.4**. If it does not
exist, create it.

#### 4.3 Create the prospect subfolder

1. Click `+ New` > `New folder`.
2. Name it exactly as the prospect appears in the workspace.
3. Click `Create`.
4. Open the new folder and copy its URL.

#### 4.4 Move the sheet into the prospect subfolder

1. Switch to the sheet tab.
2. Open the Move dialog (folder-with-arrow icon next to the title).
3. Paste the new subfolder URL (or the existing one) into the search field.
4. Select the folder and click `Move`.
5. If prompted `Change ownership to a shared drive?`, confirm.

#### 4.5 Cross-shared-drive constraint

Moving directly from one Shared Drive to another is blocked. The template copy must be in **My Drive**
when you run Step 4. If the copy landed in another shared drive, make a new copy into the prospect
subfolder (the file ID will change) and delete the original.

#### 4.6 Verify

Refresh the Drive tab. Confirm:

- The prospect subfolder exists under the customer folder.
- The sheet is inside that subfolder.
- The sheet's title reads `Airbyte || [Prospect Name] - POV Success Criteria`.

Do not report success while the file remains in My Drive or another temporary location.

### Step 5: Write a Local Receipt

The external Google Sheet is the primary artifact, but the SE workflow also needs a local record that
the run happened, what inputs were available, and what still needs to be filled.

**Where to save:**

- If the invocation provided an `{out_dir}` path (the webapp does: `{out_dir}/pov-gsheet/`), save it there.
- If no `out_dir` is provided, save under `customers/<Account>/outputs/pov-gsheet/` (or
  `customers/<Account>/opportunities/<Opportunity>/outputs/pov-gsheet/` if an opportunity was given).
- Create the `pov-gsheet/` subfolder if it does not exist.

**Filename:** `pov-gsheet-YYYY-MM-DD-<Prospect-Name>.md` (use `v2`, `v3`, etc. for same-day reruns).

**Receipt content (follow `~/.claude/skills/_se-playbook.md` → Output Document Format):**

```markdown
# <Account> — POV Google Sheet: <Status>
**Date:** <Month DD, YYYY> · **Sheet URL:** <url> · **Status:** <Status>

### At a Glance
- **Google Sheet URL:** <final verified URL>
- **Prospect:** <prospect name>
- **Status:** <created | created with gaps | blocked>
- **SE:** SE_NAME (SE_TITLE)
- **Drive Folder:** <final verified folder URL>

## Receipt
- **Prospect data sources checked:** [list each source from `source_coverage` with available: true/false]
- **Inputs used:** [what was actually pre-filled from the sources]
- **Unresolved fields:** [what could not be filled and why]
- **Drive move completed:** Yes / No (with subfolder path)
- **Sheet title:** `Airbyte || [Prospect Name] - POV Success Criteria`

## Source Coverage
[Mirror the `source_coverage` list from the structured POV context. For each entry show source name, available yes/no, freshness/path, whether it was material, and any note.]
```

If the run was `blocked` or `partial`, set `Status` accordingly and explain the missing evidence in
`Unresolved fields`. Do not claim the sheet was created unless Drive placement was verified.

## Scope boundaries

Do not:

- Import external dependency skills or their personal database files
- Use personal `~/Documents/...` paths or any Ryan-specific configuration
- Call external note-taking or call-transcript MCP tools that are not configured in this environment
- Invent prospect contacts, emails, or roles
- Invent customer requirements from generic Airbyte knowledge
- Overwrite an existing POV sheet automatically
- Replace `poc-plan` or other qualification skills
- Add a general Google Sheets framework

## Data Source Reference

This skill uses repository-native sources only:

- **Workspace account/opportunity metadata** — folder structure, `.sfdc-name`, `.se-config.yaml`
- **Prior skill outputs** — `biz-qual`, `tech-qual`, `poc-plan`, `deal-assessment`, `connector-feasibility`, `post-call`, `account-refresher`, etc.
- **Workspace transcripts** — files in `customers/_transcripts/`
- **Salesforce** — optional, via the `sf` CLI if `salesforce.enabled: true`

The deterministic loader is `webapp/pov_gsheet_context.py`. It builds the structured POV context and
records source coverage. It does not require external dependency skills, note-taking tools, or personal
databases.

## Changelog

- 2026-07-15 — Ported from external dependency skill to repository-native context loader; removed external databases and personal-path references.
- 2026-07-15 — Added `.se-config.yaml` `pov_gsheet` block, preflight checks, and structured POV context output.
